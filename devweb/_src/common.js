// ============================================================================
// Test Helper API - DevWeb regression suite: common library.
// build.py copies this block into every script's main.js (DevWeb scripts are
// self-contained), so edit it in devweb/_src/common.js and rebuild.
// ============================================================================

// ---------------------------------------------------------------- settings
// Override any of these without editing the script: put them in the script's
// user_args.json (e.g. { "project": "acme-regress-7QK2" }) or pass them as
// command-line user arguments.
function arg(name, dflt) {
  const a = (load.config.user && load.config.user.args) || {};
  return a[name] !== undefined && a[name] !== null && a[name] !== '' ? a[name] : dflt;
}

const CONFIG = {
  baseUrl: String(arg('baseUrl', 'https://api.testdata.help/v1')).replace(/\/+$/, ''),
  // The control key every script shares (recipe 4.12 of llms.txt). The API is
  // open, so keep it distinctive; change it to run two suites side by side.
  project: arg('project', 'th-devweb-regress-Q7M4'),
  // How long a script waits for the init script to finish (seconds).
  gateWaitSeconds: Number(arg('gateWaitSeconds', 600)),
  // Optional: the SMS capture device key. Blank = sms/receive is only checked
  // for refusing a missing or wrong key.
  smsDeviceKey: arg('smsDeviceKey', ''),
};
CONFIG.rootUrl = CONFIG.baseUrl.replace(/\/v1$/, '');

// Names of the data the init script creates in each cycle (<ns> = namespace).
const POOLS = {
  logins: (ns) => `${ns}_logins`,          // 30 records, CSV import  - cyclepop
  products: (ns) => `${ns}_products`,      // 12 records, JSON import - random
  invoice: (ns) => `${ns}_invoice`,        // saved attachment, invoice PDF
  invoiceLocked: (ns) => `${ns}_invoice_locked`,  // the same, AES-256 protected
  note: (ns) => `${ns}_note`,              // saved attachment, Word document
  totp: (ns) => `${ns}_totp`,              // TOTP key, user "shared"
};
const SEED = { logins: 30, products: 12, totpSecret: 'JBSWY3DPEHPK3PXP', lockedPassword: 'Reg-2468' };

// Every HTTP status is checked by the script itself, so DevWeb must not end
// the iteration on a 4xx/5xx (several negative tests expect one).
load.WebRequest.defaults.returnBody = true;
load.WebRequest.defaults.handleHTTPError = () => false;

// ---------------------------------------------------------------- state
const STATE = { ns: null, mailDomain: null, current: null };
const userId = () => load.config.user.userId;
const iteration = () => load.config.runtime.iteration;

// A unique, valid key for this script, virtual user and iteration.
function uid(label) {
  return `${STATE.ns}_${SCRIPT}_${label}_u${userId()}i${iteration()}_${load.utils.randomString(4, { letters: false, digits: true })}`;
}

// ---------------------------------------------------------------- HTTP
function enc(v) {
  return encodeURIComponent(String(v));   // '+' -> %2B, ' ' -> %20, '&' -> %26
}

function qs(query) {
  if (!query) return '';
  const parts = [];
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null) continue;
    parts.push(`${enc(k)}=${enc(v)}`);
  }
  return parts.length ? '?' + parts.join('&') : '';
}

function header(r, name) {
  const want = name.toLowerCase();
  for (const [k, v] of Object.entries(r.headers || {})) {
    if (k.toLowerCase() === want) return Array.isArray(v) ? v.join(', ') : String(v);
  }
  return undefined;
}

function buildRequest(o) {
  const url = (o.url || CONFIG.baseUrl + o.path) + qs(o.query);
  const req = { url, method: o.method || 'GET', headers: o.headers || {}, returnBody: true,
                handleHTTPError: () => false };
  if (o.body !== undefined) req.body = o.body;
  if (o.noRedirect) req.disableRedirection = true;
  if (o.binary) req.isBinaryResponse = true;
  return req;
}

function wrap(res, req, ms) {
  let json = res.jsonBody;
  if (json === undefined && typeof res.body === 'string') {
    try { json = JSON.parse(res.body); } catch (e) { json = undefined; }
  }
  const r = { status: res.status, headers: res.headers || {}, body: res.body, json, url: req.url,
              method: req.method, ms };
  r.contentType = (header(r, 'content-type') || '').split(';')[0].trim().toLowerCase();
  return r;
}

function http(o) {
  const req = buildRequest(o);
  const t0 = Date.now();
  const res = new load.WebRequest(req).sendSync();
  return wrap(res, req, Date.now() - t0);
}

// Starts a request without waiting (for long-poll tests); await the result.
function httpAsync(o) {
  const req = buildRequest(o);
  const t0 = Date.now();
  return new load.WebRequest(req).send().then((res) => wrap(res, req, Date.now() - t0));
}

// ---------------------------------------------------------------- checks
function describe(r) {
  if (!r) return '';
  const body = typeof r.body === 'string' ? r.body : '<binary>';
  return ` [${r.method} ${r.url} -> ${r.status}] ${body.slice(0, 400)}`;
}
function fail(msg) { throw new Error(msg + describe(STATE.current)); }
function ok(cond, msg) { if (!cond) fail('check failed: ' + msg); }
function eq(actual, expected, msg) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) fail(`${msg}: expected ${e}, got ${a}`);
}
function isNum(v, msg) { ok(typeof v === 'number' && Number.isFinite(v), `${msg} is a number (got ${JSON.stringify(v)})`); }
function isStr(v, msg) { ok(typeof v === 'string' && v.length > 0, `${msg} is a non-empty string (got ${JSON.stringify(v)})`); }
function near(v, target, tol, msg) { isNum(v, msg); ok(Math.abs(v - target) <= tol, `${msg}: ${v} within ${tol} of ${target}`); }
function between(v, lo, hi, msg) { isNum(v, msg); ok(v >= lo && v <= hi, `${msg}: ${v} in [${lo}, ${hi}]`); }
function has(text, part, msg) { ok(String(text).includes(part), `${msg}: ${JSON.stringify(String(text).slice(0, 200))} contains ${JSON.stringify(part)}`); }
function nowSec() { return Math.floor(Date.now() / 1000); }

// The error body every service uses: {"error": "...", "status": N, ...}.
function isError(r, status, contains) {
  eq(r.status, status, 'HTTP status');
  ok(r.json && typeof r.json.error === 'string', 'error body has "error"');
  eq(r.json.status, status, 'error body repeats the status');
  if (contains) has(r.json.error.toLowerCase(), contains.toLowerCase(), 'error message');
}

// ---------------------------------------------------------------- transactions
// Each step is a DevWeb transaction named <script>_<name>. A failed check fails
// the transaction, logs the reason and ends the iteration (so it is counted).
function step(name, fn) {
  const t = new load.Transaction(`${SCRIPT}_${name}`);
  t.start();
  try {
    const v = fn();
    t.stop(load.TransactionStatus.Passed);
    return v;
  } catch (e) {
    t.stop(load.TransactionStatus.Failed);
    load.log(`FAILED ${SCRIPT}_${name}: ${e.message}`, load.LogLevel.error);
    throw e;
  }
}

async function stepAsync(name, fn) {
  const t = new load.Transaction(`${SCRIPT}_${name}`);
  t.start();
  try {
    const v = await fn();
    t.stop(load.TransactionStatus.Passed);
    return v;
  } catch (e) {
    t.stop(load.TransactionStatus.Failed);
    load.log(`FAILED ${SCRIPT}_${name}: ${e.message}`, load.LogLevel.error);
    throw e;
  }
}

// One request as one transaction: status check (default 200), JSON check for
// JSON responses, then the caller's checks.
function call(name, o, check) {
  return step(name, () => {
    const r = http(o);
    STATE.current = r;
    expectStatus(r, o.expect === undefined ? 200 : o.expect);
    if (check) check(r, r.json);
    return r;
  });
}

function expectStatus(r, expect) {
  const want = Array.isArray(expect) ? expect : [expect];
  if (!want.includes(r.status)) fail(`HTTP status ${r.status}, expected ${want.join(' or ')}`);
  if (r.contentType === 'application/json' && r.json === undefined) fail('body is not valid JSON');
}

// ---------------------------------------------------------------- cycle gate
// Recipe 4.12: wait until init has set setupReady=1, read the namespace, then
// confirm the gate is still open (so the two belong to the same cycle).
function waitForCycle() {
  const deadline = Date.now() + CONFIG.gateWaitSeconds * 1000;
  for (;;) {
    const gate = call('gate_wait', { path: '/lists/vars/get',
      query: { key: CONFIG.project, name: 'setupReady', equals: '1', wait: 20 } }, (r, b) => {
      ok(typeof b.matched === 'boolean', 'matched is boolean');
      isNum(b.waitedMs, 'waitedMs');
    }).json;
    if (gate.matched) {
      const ns = call('gate_namespace', { path: '/lists/vars/get',
        query: { key: CONFIG.project, name: 'namespace' } }, (r, b) => {
        ok(/^[A-Z0-9]{10}$/.test(b.value), 'namespace is a rand10 code');
      }).json.value;
      const again = call('gate_recheck', { path: '/lists/vars/get',
        query: { key: CONFIG.project, name: 'setupReady', equals: '1' } }).json;
      if (again.matched) return ns;
      continue;   // a new cycle started in between: wait again
    }
    if (gate.value === 'failed') throw new Error('The init script reported setupReady=failed - see its log.');
    if (Date.now() > deadline) {
      throw new Error(`Timed out after ${CONFIG.gateWaitSeconds} s waiting for setupReady=1 under ` +
                      `${CONFIG.project}. Run the init script (1 Vuser) first or alongside.`);
    }
  }
}

// Standard initialize for the test scripts: gate, namespace, mail domain.
function startScript() {
  STATE.ns = waitForCycle();
  STATE.mailDomain = call('discovery_index', { path: '' }, (r, b) => {
    isStr(b.mailDomain, 'mailDomain');
  }).json.mailDomain;
  load.log(`${SCRIPT}: user ${userId()} using namespace ${STATE.ns}, mail domain ${STATE.mailDomain}`);
}

// ---------------------------------------------------------------- helpers
const b64 = (s) => load.utils.base64Encode(s);
const fromB64 = (s) => load.utils.base64Decode(s);
function mailAddress(label) {
  return `${uid(label)}@${STATE.mailDomain}`.toLowerCase();
}
// A mobile number unique to this user and iteration (+614 + 8 digits).
function mockNumber() {
  return '+614' + load.utils.randomString(8, { letters: false, digits: true });
}
function sleep(seconds) { load.sleep(seconds); }
