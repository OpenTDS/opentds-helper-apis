// ============================================================================
// webhook - receive with every method, chosen statuses (incl. no-body ones),
// headers, query, JSON, text and binary bodies; list (since, limit, waiting);
// clear.
// ============================================================================

load.initialize(async function () { startScript(); });

function receive(tx, key, o, expect, check) {
  return call(tx, { path: '/webhook/receive', query: { key, ...(o.query || {}) }, method: o.method || 'POST',
    headers: o.headers, body: o.body, expect: expect || 200 }, check);
}

load.action('webhook', async function () {
  call('service_index', { path: '/webhook' }, (r, b) => ['receive', 'list', 'clear'].forEach((a) => ok(b.actions.includes(a), a)));
  const key = uid('wh');
  const t0 = nowSec() - 1;
  call('list_empty', { path: '/webhook/list', query: { key } }, (r, b) => eq([b.key, b.count, b.events], [key, 0, []], 'nothing captured'));

  // JSON body with custom headers and extra query parameters.
  const payload = { event: 'order.paid', orderId: `ORD-${iteration()}`, amount: 10.5, lines: [{ sku: 'A', qty: 2 }] };
  receive('receive_json_post', key, { query: { orderId: '42', source: 'regression' },
    headers: { 'Content-Type': 'application/json', 'X-Signature': 'sha256=abc123', 'X-Request-Id': uid('req') },
    body: JSON.stringify(payload) }, 200, (r, b) => {
    eq([b.received, b.key, b.status], [true, key, 200], 'receipt'); isStr(b.seq, 'seq'); isNum(b.receivedTime, 'receivedTime');
  });
  // Every method.
  for (const method of ['GET', 'PUT', 'PATCH', 'DELETE']) {
    receive(`receive_${method.toLowerCase()}`, key, { method, query: { m: method },
      headers: method === 'GET' ? {} : { 'Content-Type': 'text/plain' }, body: method === 'GET' ? undefined : `${method} body & more` });
  }
  // Chosen statuses: the sender's retry logic sees them.
  receive('receive_status_503', key, { query: { status: 503 }, body: 'retry me' }, 503, (r, b) => eq([b.received, b.status], [true, 503], 'receipt with 503'));
  receive('receive_status_201', key, { query: { status: 201 }, body: 'created' }, 201);
  receive('receive_status_204_no_body', key, { query: { status: 204 }, body: 'x' }, 204, (r) => ok(!r.body, 'no body'));
  receive('receive_status_clamped', key, { query: { status: 700 }, body: 'x' }, 599);
  // Binary body: not UTF-8, returned as bodyBase64.
  receive('receive_binary', key, { headers: { 'Content-Type': 'application/octet-stream' }, body: Buffer.from([0xff, 0xfe, 0x00, 0x41]) });

  call('list_all', { path: '/webhook/list', query: { key, since: t0 } }, (r, b) => {
    eq(b.count, 10, 'ten events'); eq(b.more, false, 'more');
    const byMethod = (m) => b.events.filter((e) => e.method === m);
    const json = b.events.find((e) => e.query && e.query.source === 'regression');
    ok(json, 'the JSON event'); eq(json.json, payload, 'JSON body parsed'); eq(json.body, JSON.stringify(payload), 'body exactly as sent');
    eq(json.query, { orderId: '42', source: 'regression' }, 'query without key and status');
    eq(json.headers['x-signature'], 'sha256=abc123', 'custom header'); eq(json.contentType, 'application/json', 'contentType');
    ['GET', 'PUT', 'PATCH', 'DELETE'].forEach((m) => ok(byMethod(m).length >= 1, `${m} captured`));
    eq(byMethod('PATCH')[0].body, 'PATCH body & more', 'text body');
    const bin = b.events.find((e) => e.bodyBase64);
    ok(bin, 'binary event'); eq(bin.bodyBase64, Buffer.from([0xff, 0xfe, 0x00, 0x41]).toString('base64'), 'bytes kept');
    const seqs = b.events.map((e) => e.seq); eq(seqs, [...seqs].sort().reverse(), 'newest first');
    b.events.forEach((e) => { isNum(e.receivedTimeMs, 'receivedTimeMs'); isStr(e.sourceIp, 'sourceIp'); });
  });
  call('list_limit', { path: '/webhook/list', query: { key, limit: 3 } }, (r, b) => eq([b.events.length, b.more], [3, true], 'limited, more'));
  call('list_since_future', { path: '/webhook/list', query: { key, since: nowSec() + 60 } }, (r, b) => eq(b.count, 0, 'nothing after since'));

  // Waiting: the list returns when the next event arrives.
  await stepAsync('list_wait_for_event', async () => {
    // since in epoch MILLISECONDS (also accepted) is precise: the earlier events of this second are excluded.
    const since = Date.now();
    const pending = httpAsync({ path: '/webhook/list', query: { key, since, wait: 15 } });
    await load.sleepAsync(1.5);
    http({ path: '/webhook/receive', query: { key, late: 'yes' }, method: 'POST', body: 'late' });
    const r = await pending; STATE.current = r;
    eq(r.status, 200, 'status'); eq(r.json.count, 1, 'only the late event'); eq(r.json.events[0].query.late, 'yes', 'the late event');
    between(r.json.waitedMs, 500, 14000, 'returned when it arrived');
  });
  call('list_wait_runs_out', { path: '/webhook/list', query: { key, since: nowSec() + 1, wait: 2 } }, (r, b) => {
    eq(b.count, 0, 'none'); between(b.waitedMs, 1500, 8000, 'waitedMs');
  });

  call('receive_missing_key_400', { path: '/webhook/receive', method: 'POST', body: 'x', expect: 400 }, (r) => isError(r, 400, 'key'));
  call('receive_bad_status_400', { path: '/webhook/receive', query: { key, status: 'abc' }, expect: 400 }, (r) => isError(r, 400));
  call('clear', { path: '/webhook/clear', query: { key } }, (r, b) => eq([b.key, b.deleted], [key, 11], 'deleted all'));
  call('list_after_clear', { path: '/webhook/list', query: { key } }, (r, b) => eq(b.count, 0, 'empty'));
});
