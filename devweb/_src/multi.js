// ============================================================================
// multi - end-to-end flows that use several services together, the way a
// test of a real application would, plus the discovery documents.
//   signup   names -> lists -> mail (verify code) -> vars -> totp -> qr -> webhook
//   order    cyclepop login -> lock -> mail with the invoice PDF -> find/compare
//            -> save -> webhook -> counters
//   sms otp  mocksend -> sms/get -> vars
// Each flow is timed into perf; finalize gates on perf/assert. Everything a
// flow creates is registered with lists/cleanup and removed at the end of the
// iteration by popping the register.
// ============================================================================

const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const SERVICES = ['lists', 'names', 'mail', 'sms', 'totp', 'webhook', 'lock', 'qr', 'perf'];

load.initialize(async function () { startScript(); });

function cleanupKey() { return `${STATE.ns}_multi_cleanup_u${userId()}`; }
function register(kind, value) {
  call('cleanup_register', { path: '/lists/cleanup/push', query: { key: cleanupKey(), value, note: kind } }, (r, b) => eq(b.registered, true, 'registered'));
}
function timed(flow, fn) {
  const t0 = Date.now();
  const out = fn();
  call('perf_record', { path: '/perf/add', query: { key: `${STATE.ns}_multi_${flow}`, value: Date.now() - t0 } });
  return out;
}
function popMessage(tx, address) {
  return step(tx, () => {
    for (let i = 0; i < 3; i++) {
      const r = http({ path: '/mail/pop', query: { address, wait: 20 } }); STATE.current = r;
      eq(r.status, 200, 'status');
      if (!r.json.exhausted) return r.json;
    }
    fail(`no email at ${address} within 60 s`);
  });
}

function discovery() {
  call('disc_index', { path: '' }, (r, b) => {
    eq(b.services.map((s) => s.name), SERVICES, 'nine services'); eq(b.baseUrl, CONFIG.baseUrl, 'baseUrl');
    b.services.forEach((s) => ok(s.actions.length > 0, `${s.name} actions`));
  });
  for (const s of SERVICES) call('disc_service_index', { path: `/${s}` }, (r, b) => eq(b.service, s, 'service'));
  call('disc_openapi_32', { path: '/openapi.json' }, (r, b) => {
    eq(b.openapi, '3.2.1', 'OpenAPI 3.2.1'); eq(b.$self, `${CONFIG.baseUrl}/openapi.json`, '$self'); ok(Object.keys(b.paths).length > 60, 'paths');
  });
  call('disc_openapi_30', { path: '/openapi-3.0.json' }, (r, b) => eq(b.openapi, '3.0.3', 'OpenAPI 3.0.3 copy'));
  call('disc_llms', { path: '/llms.txt' }, (r) => { eq(r.contentType, 'text/plain', 'text'); has(r.body, `${CONFIG.baseUrl}/lists/pop`, 'URLs use this base'); });
  call('disc_root_llms', { url: `${CONFIG.rootUrl}/llms.txt` }, (r) => has(r.body, '# Test Helper API', 'guide at the domain root'));
  call('disc_api_catalog', { url: `${CONFIG.rootUrl}/.well-known/api-catalog` }, (r) => {
    eq(r.contentType, 'application/linkset+json', 'linkset'); const j = JSON.parse(r.body);
    eq(j.linkset[0]['service-desc'].map((d) => d.href), [`${CONFIG.baseUrl}/openapi.json`, `${CONFIG.baseUrl}/openapi-3.0.json`], 'both documents');
  });
  call('disc_endpoints', { url: `${CONFIG.rootUrl}/.well-known/endpoints` }, (r, b) => eq(b.services.length, 9, 'services'));
  call('disc_wellknown_openapi', { url: `${CONFIG.rootUrl}/.well-known/openapi.json` }, (r, b) => eq(b.openapi, '3.2.1', 'well-known OpenAPI'));
  call('disc_wellknown_llms', { url: `${CONFIG.rootUrl}/.well-known/llms.txt` }, (r) => has(r.body, '# Test Helper API', 'well-known guide'));
  call('disc_wellknown_guide_md', { url: `${CONFIG.rootUrl}/.well-known/guide.md` }, (r) => eq(r.contentType, 'text/markdown', 'markdown'));
  call('disc_robots', { url: `${CONFIG.rootUrl}/robots.txt` }, (r) => has(r.body, 'User-agent', 'robots'));
  call('disc_landing_page', { url: `${CONFIG.rootUrl}/`, headers: { Accept: 'text/html' } }, (r) => { eq(r.contentType, 'text/html', 'HTML'); has(r.body, 'llms.txt', 'links the guide'); });
  call('disc_unknown_path_404', { path: '/nosuchservice', expect: 404 }, (r, b) => { isError(r, 404); eq(b.services, SERVICES, 'services listed'); });
  call('cors_preflight', { path: '/lists/pop', method: 'OPTIONS', expect: [200, 204] }, (r) => eq(header(r, 'access-control-allow-origin'), '*', 'CORS open'));
}

function signup() {
  return timed('signup', () => {
    const p = call('signup_persona', { path: '/names/get', query: { region: 'ANY' } }, (r, b) => isStr(b.ascii.femaleFirstName, 'name')).json;
    const first = p.ascii.femaleFirstName.toLowerCase().replace(/[^a-z]/g, '') || 'user';
    const email = `${first}.${uid('su').toLowerCase()}@${STATE.mailDomain}`;
    const customers = `${STATE.ns}_multi_customers_u${userId()}`;
    call('signup_store_customer', { path: '/lists/push', query: { key: customers, email, name: p.femaleFullName, region: p.region } },
      (r, b) => eq(b.record.email, email, 'stored'));
    register('list', customers); register('mailbox', email);

    // The application would send this; here mail/send stands in for it.
    const code = load.utils.randomString(6, { letters: false, digits: true });
    call('signup_send_verification', { path: '/mail/send', query: { from: `noreply@${STATE.mailDomain}`, to: email,
      subject: `Verify your account, ${p.femaleFirstName}`, body: `Welcome!\nYour verification code is ${code}.\n` } });
    const m = popMessage('signup_receive_verification', email);
    const got = (m.body.match(/verification code is (\d{6})/) || [])[1];
    step('signup_extract_code', () => eq(got, code, 'code from the email'));
    const state = `${STATE.ns}_multi_state_u${userId()}`;
    call('signup_share_code', { path: '/lists/vars/set', query: { key: state, name: 'lastCode', value: got } });
    call('signup_check_code', { path: '/lists/vars/get', query: { key: state, name: 'lastCode', equals: code } }, (r, b) => eq(b.matched, true, 'shared'));

    // 2FA enrolment: secret -> otpauth QR -> codes.
    let secret = ''; for (let i = 0; i < 16; i++) secret += BASE32[Math.floor(Math.random() * 32)];
    const totpKey = `${STATE.ns}_multi_totp`;
    call('signup_totp_enrol', { path: '/totp/set', query: { key: totpKey, user: email, secret } }, (r, b) => eq(b.user, email, 'user with @'));
    const otpauth = `otpauth://totp/Regression:${email}?secret=${secret}&issuer=Regression`;
    call('signup_totp_qr', { path: '/qr/get', query: { text64: b64(otpauth), scale: 4 } }, (r, b) => { eq(b.text, otpauth, 'QR payload'); isStr(b.png, 'png'); });
    call('signup_totp_code', { path: '/totp/get', query: { key: totpKey, user: email } }, (r, b) => {
      const now = Date.now();
      ok([0, -30000, 30000].map((d) => load.utils.totp(secret, now + d)).includes(b.code), 'the authenticator code');
    });
    // The application's "customer.created" webhook.
    const hook = `${STATE.ns}_multi_hooks_u${userId()}`;
    register('webhook', hook);
    const since = Date.now();
    call('signup_webhook_send', { path: '/webhook/receive', query: { key: hook }, method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'customer.created', email, region: p.region }) });
    call('signup_webhook_check', { path: '/webhook/list', query: { key: hook, since, wait: 10 } }, (r, b) => {
      ok(b.events.some((e) => e.json && e.json.type === 'customer.created' && e.json.email === email), 'event captured');
    });
    call('signup_count', { path: '/lists/counters/add', query: { key: STATE.ns, name: 'multiSignups' } });
    return email;
  });
}

function order() {
  return timed('order', () => {
    const login = call('order_login', { path: '/lists/cyclepop', query: { key: POOLS.logins(STATE.ns) } }, (r, b) => {
      eq(b.exhausted, false, 'a login'); isStr(b.record.password, 'password');
    }).json.record;
    const lock = step('order_lock_login', () => {
      for (let i = 0; i < 4; i++) {
        const r = http({ path: '/lock/acquire', query: { key: `${STATE.ns}_login_${login.username}`, wait: 10 } }); STATE.current = r;
        if (r.json && r.json.locked) return r.json;
      }
      fail('login stayed locked for 40 s');
    });
    try {
      const orderId = `ORD-${load.utils.randomString(8, { letters: false, digits: true })}`;
      const inbox = `orders.${uid('ob').toLowerCase()}@${STATE.mailDomain}`;
      register('mailbox', inbox);
      call('order_send_invoice', { path: '/mail/send', query: { from: `billing@${STATE.mailDomain}`, to: inbox,
        subject: `Invoice for ${orderId}`, body: `Dear ${login.display}, your invoice for ${orderId} is attached.`, key: POOLS.invoice(STATE.ns) } });
      const m = popMessage('order_receive_invoice', inbox);
      const att = step('order_invoice_attached', () => {
        eq(m.subject, `Invoice for ${orderId}`, 'subject'); eq(m.attachments.length, 1, 'attachment'); return m.attachments[0];
      });
      call('order_invoice_total', { path: '/mail/attachments/find', query: { ref: att.ref, left: 'Total including GST' } }, (r, b) => eq(b.values, ['$123.45'], 'total'));
      call('order_invoice_number', { path: '/mail/attachments/find', query: { ref: att.ref, in: 'metadata', left: 'InvoiceNumber' } }, (r, b) => eq(b.values, ['TH-REG-0001'], 'number'));
      // "Download it from the app" = the saved copy; the emailed one must match.
      call('order_compare', { path: '/mail/attachments/compare', query: { ref: att.ref, otherkey: POOLS.invoice(STATE.ns) } }, (r, b) => eq(b.same, true, 'same document'));
      const keep = uid('inv');
      call('order_keep_invoice', { path: '/mail/attachments/save', query: { key: keep, ref: att.ref, filename: `${orderId}.pdf` } }, (r, b) => eq(b.contentType, 'application/pdf', 'pdf'));
      register('file', keep);
      call('order_view_link', { path: '/mail/attachments/get', query: { key: keep, view: 'true' }, noRedirect: true, expect: 302 });
      const hook = `${STATE.ns}_multi_hooks_u${userId()}`;
      call('order_webhook_paid', { path: '/webhook/receive', query: { key: hook, orderId }, method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: 'order.paid', orderId, user: login.username }) });
      call('order_count', { path: '/lists/counters/add', query: { key: STATE.ns, name: 'multiOrders' } }, (r, b) => ok(b.value >= 1, 'counted'));
    } finally {
      call('order_unlock_login', { path: '/lock/release', query: { ref: lock.ref } }, (r, b) => eq(b.released, true, 'released'));
    }
  });
}

function smsOtp() {
  return timed('smsotp', () => {
    const number = mockNumber();
    const sent = call('sms_app_sends_code', { path: '/sms/mocksend', query: { number, body: 'Your login code is {code}', from: 'Regression' } }).json;
    const got = call('sms_read_code', { path: '/sms/get', query: { number, since: sent.sentTime - 1 } }, (r, b) => eq(b.found, true, 'found')).json;
    step('sms_extract_code', () => eq((got.body.match(/code is (\d{6})/) || [])[1], sent.code, 'code'));
    call('sms_share_code', { path: '/lists/vars/set', query: { key: `${STATE.ns}_multi_state_u${userId()}`, name: 'smsCode', value: sent.code } });
  });
}

// Pop the cleanup register and delete everything it lists.
function teardown() {
  step('teardown', () => {
    for (let i = 0; i < 50; i++) {
      const r = http({ path: '/lists/cleanup/pop', query: { key: cleanupKey() } }); STATE.current = r;
      eq(r.status, 200, 'cleanup/pop');
      if (r.json.exhausted) return;
      const { value, note } = r.json;
      const req = { list: { path: '/lists/clear', query: { key: value } }, mailbox: { path: '/mail/clear', query: { address: value } },
                    webhook: { path: '/webhook/clear', query: { key: value } }, file: { path: '/mail/attachments/delete', query: { key: value } } }[note];
      ok(req, `known kind ${note}`);
      const d = http(req); STATE.current = d; ok(d.status === 200, `deleted ${note} ${value}`);
    }
  });
}

load.action('flows', async function () {
  discovery();
  signup();
  order();
  smsOtp();
  teardown();
  call('teardown_complete', { path: '/lists/cleanup/list', query: { key: cleanupKey(), status: 'pending' } }, (r, b) => eq(b.pending, 0, 'nothing left'));
});

load.finalize(async function () {
  // A pipeline-style gate over this run's flow timings.
  for (const flow of ['signup', 'order', 'smsotp']) {
    call('perf_gate', { path: '/perf/assert', query: { key: `${STATE.ns}_multi_${flow}`, p95lt: 90000, countge: 1 } }, (r, b) => {
      eq(b.pass, true, `${flow} timings within limits`);
    });
  }
});
