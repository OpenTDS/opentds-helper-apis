// ============================================================================
// mail - the catcher (pop/reset/clear, waiting, grace, plus-addressing), the
// sender (plain, generated, saved and received attachments, injected bytes,
// JSON and text POST bodies, allow-list refusals) and the attachment store
// (save, get, download/view redirects, text, find, compare, passwords,
// delete, S3 upload).
// ============================================================================

load.initialize(async function () { startScript(); });

const FROM = () => `regression@${STATE.mailDomain}`;

function send(tx, query, extra) {
  return call(tx, { path: '/mail/send', query: { from: FROM(), ...query }, ...(extra || {}) }, (r, b) => {
    eq(b.result, 'sent', 'result'); isStr(b.messageId, 'messageId'); between(b.sentTime, nowSec() - 300, nowSec() + 300, 'sentTime');
  }).json;
}

// Pop until a message arrives (up to 3 x 20 s): mail delivery takes seconds.
function popMessage(tx, address, checks) {
  return step(tx, () => {
    for (let i = 0; i < 3; i++) {
      const r = http({ path: '/mail/pop', query: { address, wait: 20 } }); STATE.current = r;
      eq(r.status, 200, 'status'); eq(r.json.address, address, 'address echoed');
      if (!r.json.exhausted) { if (checks) checks(r.json); return r.json; }
    }
    fail(`no email arrived at ${address} within 60 s`);
  });
}

function multipart(fields, fileName, fileContent, contentType) {
  const boundary = '----thdevweb' + load.utils.randomString(16, { letters: true, digits: true });
  let s = '';
  for (const [k, v] of Object.entries(fields)) s += `--${boundary}\r\nContent-Disposition: form-data; name="${k}"\r\n\r\n${v}\r\n`;
  s += `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${fileName}"\r\nContent-Type: ${contentType}\r\n\r\n` +
       `${fileContent}\r\n--${boundary}--\r\n`;
  return { body: s, contentType: `multipart/form-data; boundary=${boundary}` };
}

load.action('inbox', async function () {
  const address = mailAddress('inbox');
  call('service_index', { path: '/mail' }, (r, b) => ['pop', 'send', 'attachments/find', 'attachments/compare'].forEach((a) => ok(b.actions.includes(a), a)));
  call('clear_new_inbox', { path: '/mail/clear', query: { address } }, (r, b) => eq([b.address, b.deleted], [address, 0], 'nothing to delete'));
  call('pop_empty', { path: '/mail/pop', query: { address } }, (r, b) => eq([b.exhausted, b.body], [true, null], 'empty inbox'));
  call('pop_missing_address_400', { path: '/mail/pop', expect: 400 }, (r) => isError(r, 400, 'address'));

  const code = load.utils.randomString(6, { letters: false, digits: true });
  const subject = `Verify ${uid('s')}`;
  send('send_plain', { to: address, subject, body: `Hello,\nyour code is ${code}.\nThanks` }, null);
  const m = popMessage('pop_wait', address, (b) => {
    eq([b.subject, b.from, b.serial, b.remaining, b.repeat, b.bodyTruncated], [subject, FROM(), 1, 0, false, false], 'message fields');
    has(b.body, `your code is ${code}`, 'body'); eq(b.attachments, [], 'no attachments'); isNum(b.receivedTime, 'receivedTime');
    between(b.ageSeconds, 0, 120, 'ageSeconds');
  });
  ok((m.body.match(/code is (\d{6})/) || [])[1] === code, 'code extracted with a regex');
  call('pop_consumed', { path: '/mail/pop', query: { address, grace: 0 } }, (r, b) => eq(b.exhausted, true, 'consumed'));
  call('pop_grace_repeat', { path: '/mail/pop', query: { address, grace: 60 } }, (r, b) => {
    eq([b.exhausted, b.repeat, b.serial], [false, true, 1], 'the last message offered again, flagged repeat');
  });
  call('reset', { path: '/mail/reset', query: { address } }, (r, b) => eq(b.reset, true, 'reset'));
  call('pop_after_reset', { path: '/mail/pop', query: { address: address.toUpperCase() } }, (r, b) => {
    eq([b.serial, b.subject, b.repeat], [1, subject, false], 'same message again (address case-insensitive)');
  });
  call('clear', { path: '/mail/clear', query: { address } }, (r, b) => eq(b.deleted, 1, 'deleted 1'));
  call('pop_after_clear', { path: '/mail/pop', query: { address, grace: 0 } }, (r, b) => eq(b.exhausted, true, 'empty after clear'));

  // Plus-addressing: the + survives as %2B.
  const plus = address.replace('@', '+tag@');
  send('send_plus_address', { to: plus, subject: 'plus', body: 'plus addressing' });
  popMessage('pop_plus_address', plus, (b) => eq(b.subject, 'plus', 'delivered to the + address'));

  // A POSTed text body is the message.
  const text = `Line one & two\nLine 3: 100% sure +plus\n${'x'.repeat(2000)}`;
  send('send_post_text', { to: address, subject: 'post-text' }, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: text });
  popMessage('pop_post_text', address, (b) => { has(b.body, 'Line one & two', 'body'); has(b.body, '100% sure +plus', 'special characters'); });

  // Refusals: from outside the mail domain, to an address that is not allowed.
  call('send_from_outside_403', { path: '/mail/send', query: { from: 'someone@example.com', to: address, subject: 'x', body: 'x' }, expect: 403 },
    (r, b) => { isError(r, 403); eq(b.allowedFrom, `*@${STATE.mailDomain}`, 'allowedFrom'); });
  call('send_to_outside_403', { path: '/mail/send', query: { from: FROM(), to: 'someone@example.com', subject: 'x', body: 'x' }, expect: 403 },
    (r, b) => { isError(r, 403); eq(b.mailDomain, STATE.mailDomain, 'mailDomain'); ok(Array.isArray(b.allowedDomains), 'allowedDomains'); });
  call('send_missing_subject_400', { path: '/mail/send', query: { from: FROM(), to: address, body: 'x' }, expect: 400 }, (r) => isError(r, 400, 'subject'));
  call('send_two_attachment_sources_400', { path: '/mail/send',
    query: { from: FROM(), to: address, subject: 'x', body: 'x', attachmentkb: 1, key: POOLS.invoice(STATE.ns) }, expect: 400 }, (r) => isError(r, 400));
  call('clear_end', { path: '/mail/clear', query: { address } });
  call('clear_plus', { path: '/mail/clear', query: { address: plus } });
});

load.action('emailed attachments', async function () {
  const address = mailAddress('att');
  // A generated attachment with injected bytes at the start and end.
  send('send_generated', { to: address, subject: 'generated', body: 'see attached', attachmentkb: 3, filename: 'dummy.bin',
    prepend64: b64('HEAD-MARK'), append64: b64('TAIL-MARK') });
  const gen = popMessage('pop_generated', address, (b) => {
    eq(b.attachments.length, 1, 'one attachment'); eq(b.attachments[0].filename, 'dummy.bin', 'filename');
    ok(b.attachments[0].bytes >= 3072, 'about 3 KB'); isStr(b.attachments[0].ref, 'ref');
  }).attachments[0];
  call('get_received_data', { path: '/mail/attachments/get', query: { ref: gen.ref } }, (r, b) => {
    eq([b.source, b.filename, b.bytes, b.ref], ['received', 'dummy.bin', gen.bytes, gen.ref], 'received file');
    const bytes = fromB64(b.data);
    ok(bytes.startsWith('HEAD-MARK'), 'prepend64 at the start'); ok(bytes.endsWith('TAIL-MARK'), 'append64 at the end');
  });
  call('get_received_url', { path: '/mail/attachments/get', query: { ref: gen.ref, url: 'true' } }, (r, b) => {
    ok(/^https?:\/\//.test(b.url), 'presigned url'); isNum(b.expiresInSeconds, 'expiresInSeconds'); ok(b.data === undefined, 'no data');
  });
  call('get_download_302', { path: '/mail/attachments/get', query: { ref: gen.ref, download: 'true' }, noRedirect: true, expect: 302 },
    (r) => ok(/^https?:\/\//.test(header(r, 'location') || ''), 'Location header'));
  call('get_view_302', { path: '/mail/attachments/get', query: { ref: gen.ref, view: 'true' }, noRedirect: true, expect: 302 },
    (r) => ok(/^https?:\/\//.test(header(r, 'location') || ''), 'Location header'));
  call('get_unknown_ref_404', { path: '/mail/attachments/get', query: { ref: 'zzzzzzzzzz' }, expect: 404 }, (r) => isError(r, 404));

  // Promote the received file into the store under a new name.
  const kept = uid('kept');
  call('save_promote_ref', { path: '/mail/attachments/save', query: { key: kept, ref: gen.ref, filename: 'promoted.bin' } }, (r, b) => {
    eq([b.result, b.key, b.filename, b.bytes], ['saved', kept, 'promoted.bin', gen.bytes], 'promoted');
  });

  // Send the invoice the init script saved; check it arrived intact.
  const inv = POOLS.invoice(STATE.ns);
  send('send_saved_key', { to: address, subject: 'invoice', body: 'your invoice', key: inv });
  const got = popMessage('pop_saved_key', address, (b) => eq(b.attachments[0].filename, 'regression-invoice.pdf', 'filename from the store')).attachments[0];
  call('compare_received_vs_saved', { path: '/mail/attachments/compare', query: { ref: got.ref, otherkey: inv } }, (r, b) => {
    eq([b.same, b.sameBytes, b.sameText], [true, true, true], 'the very same file'); eq(b.first.source, 'received', 'first'); eq(b.second.source, 'saved', 'second');
    has(b.verdict, 'identical', 'verdict');
  });
  call('find_in_received', { path: '/mail/attachments/find', query: { ref: got.ref, left: 'Total including GST' } },
    (r, b) => eq(b.values, ['$123.45'], 'value next to the label'));

  // POST JSON: several attachments, from the store and from a received ref.
  send('send_post_json', { to: address, subject: 'two files' }, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body: 'Two attachments.', attachments: [{ key: inv }, { ref: gen.ref, filename: 'second.bin' }] }) });
  popMessage('pop_post_json', address, (b) => {
    eq(b.body.trim(), 'Two attachments.', 'body'); eq(b.attachments.map((a) => a.filename), ['regression-invoice.pdf', 'second.bin'], 'both files');
  });
  // Re-send a received file by ref.
  send('send_received_ref', { to: address, subject: 'forward', body: 'forwarded', ref: got.ref, filename: 'forwarded.pdf' });
  popMessage('pop_received_ref', address, (b) => eq(b.attachments[0].filename, 'forwarded.pdf', 'renamed'));

  call('delete_promoted', { path: '/mail/attachments/delete', query: { key: kept } });
  call('clear', { path: '/mail/clear', query: { address } });
});

load.action('attachment store', async function () {
  const key = uid('pdf');
  // Save as base64 text, and as raw bytes; the two must be identical.
  const t = call('save_base64', { method: 'POST', path: '/mail/attachments/save', query: { key, filename: 'invoice.pdf' },
    headers: { 'Content-Type': 'text/plain' }, body: FIXTURES.invoicePdf }, (r, b) => {
    eq([b.result, b.contentType, b.bodyFormat, b.source], ['saved', 'application/pdf', 'base64', 'upload'], 'saved'); ok(/^[0-9a-f]{64}$/.test(b.sha256), 'sha256');
  }).json;
  const raw = uid('raw');
  call('save_binary', { method: 'POST', path: '/mail/attachments/save', query: { key: raw, filename: 'invoice.pdf' },
    headers: { 'Content-Type': 'application/octet-stream' }, body: Buffer.from(FIXTURES.invoicePdf, 'base64') }, (r, b) => {
    eq(b.bodyFormat, 'binary', 'raw bytes'); eq([b.sha256, b.bytes], [t.sha256, t.bytes], 'same bytes as the base64 upload');
  });
  call('save_wrong_type_400', { method: 'POST', path: '/mail/attachments/save', query: { key: raw + 'x', filename: 'x.pdf' },
    headers: { 'Content-Type': 'application/pdf' }, body: Buffer.from(FIXTURES.invoicePdf, 'base64'), expect: 400 }, (r, b) => {
    isError(r, 400); has(JSON.stringify(b), 'octet-stream', 'says which header to use');
  });
  const nf = uid('nofilename');
  call('save_without_filename', { method: 'POST', path: '/mail/attachments/save', query: { key: nf },
    headers: { 'Content-Type': 'text/plain' }, body: b64('plain text file') }, (r, b) => {
    eq([b.filename, b.contentType], [nf, 'application/octet-stream'], 'named after the key'); isStr(b.hint, 'hint');
  });
  call('get_saved_data', { path: '/mail/attachments/get', query: { key } }, (r, b) => {
    eq([b.source, b.filename, b.bytes, b.contentType], ['saved', 'invoice.pdf', t.bytes, 'application/pdf'], 'saved file');
    eq(b.data, FIXTURES.invoicePdf, 'content round-trips');
  });

  // Text extraction.
  call('text_json', { path: '/mail/attachments/text', query: { key } }, (r, b) => {
    eq(b.document.pages, 2, 'two pages'); eq(b.pages.length, 2, 'pages array'); has(b.pages[0].text, 'Invoice number\tTH-REG-0001', 'label TAB value');
    eq(b.metadata.Title, 'Regression Invoice', 'Title'); eq(b.metadata.InvoiceNumber, 'TH-REG-0001', 'custom property'); eq(b.truncated, false, 'truncated');
  });
  call('text_page_2', { path: '/mail/attachments/text', query: { key, page: 2 } }, (r, b) => { eq(b.pages.length, 1, 'one page'); has(b.pages[0].text, 'PAGE2-OK', 'page 2'); });
  call('text_plain', { path: '/mail/attachments/text', query: { key, format: 'text' } }, (r) => {
    eq(r.contentType, 'text/plain', 'plain text'); has(r.body, 'PAGE1-OK', 'page 1'); has(r.body, 'PAGE2-OK', 'page 2');
  });
  call('text_docx', { path: '/mail/attachments/text', query: { key: POOLS.note(STATE.ns) } }, (r, b) => has(b.pages[0].text, 'DOCX-OK', 'Word text'));

  // Find: literal (flexible whitespace), regex, boundaries, metadata, case.
  call('find_text', { path: '/mail/attachments/find', query: { key, text: 'Invoice number TH-REG-0001' } }, (r, b) => {
    eq(b.count, 1, 'space matches the TAB'); eq(b.results[0].page, 1, 'page');
  });
  call('find_text64', { path: '/mail/attachments/find', query: { key, text64: b64('Total including GST $123.45') } }, (r, b) => eq(b.count, 1, 'text64'));
  call('find_exact_no_match', { path: '/mail/attachments/find', query: { key, text: 'Invoice number TH-REG-0001', exact: 'true' } },
    (r, b) => eq(b.count, 0, 'exact: a space is not a TAB'));
  call('find_regex', { path: '/mail/attachments/find', query: { key, regex: 'ORD-\\d{6}' } }, (r, b) => eq(b.values, ['ORD-778899'], 'regex'));
  call('find_regex64', { path: '/mail/attachments/find', query: { key, regex64: b64('\\$[0-9]+\\.[0-9]{2}') } },
    (r, b) => eq(b.values, ['$112.23', '$123.45'], 'both amounts'));
  call('find_left', { path: '/mail/attachments/find', query: { key, left: 'Order number' } }, (r, b) => eq(b.values, ['ORD-778899'], 'to end of cell'));
  call('find_left_right', { path: '/mail/attachments/find', query: { key, left: 'Customer', right: 'Customer' } }, (r, b) => eq(b.values, ['Regression'], 'between'));
  call('find_metadata', { path: '/mail/attachments/find', query: { key, in: 'metadata', left: 'InvoiceNumber' } }, (r, b) => eq(b.values, ['TH-REG-0001'], 'metadata'));
  call('find_case_miss_hint', { path: '/mail/attachments/find', query: { key, text: 'marker page1-ok' } }, (r, b) => {
    eq([b.count, b.caseInsensitiveCount], [0, 1], 'case hint'); isStr(b.hint, 'hint');
  });
  call('find_ignorecase', { path: '/mail/attachments/find', query: { key, text: 'marker page1-ok', ignorecase: 'true' } }, (r, b) => eq(b.count, 1, 'ignorecase'));
  call('find_two_search_types_400', { path: '/mail/attachments/find', query: { key, text: 'a', regex: 'b' }, expect: 400 }, (r) => isError(r, 400));
  call('find_no_search_400', { path: '/mail/attachments/find', query: { key }, expect: 400 }, (r) => isError(r, 400));

  // Password-protected PDF.
  const locked = POOLS.invoiceLocked(STATE.ns), pw = SEED.lockedPassword;
  call('locked_text_no_password_422', { path: '/mail/attachments/text', query: { key: locked }, expect: 422 },
    (r, b) => eq([b.passwordRequired, b.encrypted], [true, true], 'password required'));
  call('locked_text_wrong_password_422', { path: '/mail/attachments/text', query: { key: locked, password: 'wrong' }, expect: 422 },
    (r, b) => eq(b.passwordIncorrect, true, 'password incorrect'));
  call('locked_text_password', { path: '/mail/attachments/text', query: { key: locked, password: pw } }, (r, b) => {
    eq(b.document.encrypted, true, 'encrypted'); has(b.pages[0].text, 'TH-REG-0001', 'text');
  });
  call('locked_text_password64', { path: '/mail/attachments/text', query: { key: locked, password64: b64(pw) } }, (r, b) => has(b.pages[0].text, 'TH-REG-0001', 'text'));
  call('locked_text_owner_password', { path: '/mail/attachments/text', query: { key: locked, password: 'Owner-1357' } }, (r, b) => has(b.pages[0].text, 'TH-REG-0001', 'owner password'));
  call('locked_find_password', { path: '/mail/attachments/find', query: { key: locked, left: 'Total including GST', password: pw } }, (r, b) => eq(b.values, ['$123.45'], 'find'));
  call('locked_get_unlocked', { path: '/mail/attachments/get', query: { key: locked, password: pw } }, (r, b) => {
    eq([b.unlocked, b.encrypted], [true, true], 'served unlocked'); isNum(b.originalBytes, 'originalBytes'); ok(fromB64(b.data).startsWith('%PDF'), 'a PDF');
  });
  call('locked_view_302', { path: '/mail/attachments/get', query: { key: locked, password: pw, view: 'true' }, noRedirect: true, expect: 302 },
    (r) => ok(!!header(r, 'location'), 'Location'));
  call('locked_get_wrong_password_422', { path: '/mail/attachments/get', query: { key: locked, password: 'wrong' }, expect: 422 },
    (r, b) => eq(b.passwordIncorrect, true, 'incorrect'));
  call('plain_pdf_password_not_unlocked', { path: '/mail/attachments/get', query: { key, password: 'x' } }, (r, b) => eq(b.unlocked, false, 'not encrypted'));
  call('password_on_non_pdf_400', { path: '/mail/attachments/get', query: { key: POOLS.note(STATE.ns), password: 'x' }, expect: 400 }, (r) => isError(r, 400));

  // Compare.
  const v2 = uid('v2');
  call('save_v2', { method: 'POST', path: '/mail/attachments/save', query: { key: v2, filename: 'invoice-v2.pdf' },
    headers: { 'Content-Type': 'text/plain' }, body: FIXTURES.invoiceV2Pdf });
  call('compare_different', { path: '/mail/attachments/compare', query: { key, otherkey: v2 } }, (r, b) => {
    eq([b.same, b.sameText, b.differenceCount], [false, false, 1], 'one difference'); has(b.verdict, 'different', 'verdict');
    const d = b.differences[0]; eq(d.change, 'changed', 'changed line'); has(d.first.join(' '), '$123.45', 'first'); has(d.second.join(' '), '$132.45', 'second');
  });
  call('compare_same_bytes', { path: '/mail/attachments/compare', query: { key, otherkey: raw } }, (r, b) => eq([b.same, b.sameBytes], [true, true], 'identical'));
  call('compare_locked_with_password', { path: '/mail/attachments/compare', query: { key, otherkey: locked, otherpassword: pw } }, (r, b) => {
    eq([b.same, b.sameBytes], [true, false], 'same text, different bytes'); eq(b.second.encrypted, true, 'second encrypted');
  });
  call('compare_locked_no_password_422', { path: '/mail/attachments/compare', query: { key, otherkey: locked }, expect: 422 }, (r) => isError(r, 422));
  call('compare_pdf_vs_docx', { path: '/mail/attachments/compare', query: { key, otherkey: POOLS.note(STATE.ns) } }, (r, b) => eq(b.same, false, 'different documents'));
  call('compare_missing_404', { path: '/mail/attachments/compare', query: { key, otherkey: uid('missing') }, expect: 404 }, (r) => isError(r, 404));

  // Large-file route: presigned S3 POST, then the file is in the store.
  const big = uid('big');
  const content = 'big-file '.repeat(500);
  const up = call('upload_url', { path: '/mail/attachments/upload/url', query: { key: big, filename: 'big.txt', size: content.length } }, (r, b) => {
    eq([b.key, b.method, b.filename], [big, 'POST', 'big.txt'], 'upload fields'); isStr(b.url, 'url'); isStr(b.fields.policy, 'policy');
  }).json;
  const form = multipart(up.fields, 'big.txt', content, up.fields['Content-Type'] || 'text/plain');
  call('upload_s3_post', { method: 'POST', url: up.url, headers: { 'Content-Type': form.contentType }, body: form.body, expect: [201, 204] });
  call('upload_get', { path: '/mail/attachments/get', query: { key: big } }, (r, b) => { eq(b.bytes, content.length, 'bytes'); eq(fromB64(b.data), content, 'content'); });
  call('upload_page_html', { path: '/mail/attachments/upload' }, (r) => { eq(r.contentType, 'text/html', 'HTML'); has(r.body, '<html', 'page'); });

  // Delete.
  for (const k of [key, raw, v2, nf, big]) {
    call('delete', { path: '/mail/attachments/delete', query: { key: k } }, (r, b) => eq(b.key, k, 'key'));
  }
  call('get_deleted_404', { path: '/mail/attachments/get', query: { key }, expect: 404 }, (r) => isError(r, 404));
});
