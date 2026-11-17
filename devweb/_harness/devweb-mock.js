#!/usr/bin/env node
// A small stand-in for the DevWeb runtime, for checking these scripts without
// LoadRunner: runs a script folder's main.js with N virtual users for a
// duration and prints a transaction summary. Covers only the parts of the
// DevWeb JavaScript SDK the suite uses (WebRequest sendSync/send, Transaction,
// log, sleep, exit, config, utils, global). It is NOT DevWeb; use it for a
// quick local check, then run the real thing.
//
//   node devweb-mock.js <scriptDir> [--vusers 3] [--duration 120] [--pacing 1]
//                       [--arg name=value ...] [--quiet]
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawnSync, fork } = require('child_process');

function parseArgs(argv) {
  const o = { dir: null, vusers: 1, duration: 60, pacing: 1, args: {}, quiet: false, child: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--vusers') o.vusers = Number(argv[++i]);
    else if (a === '--duration') o.duration = Number(argv[++i]);
    else if (a === '--pacing') o.pacing = Number(argv[++i]);
    else if (a === '--arg') { const [k, ...v] = argv[++i].split('='); o.args[k] = v.join('='); }
    else if (a === '--quiet') o.quiet = true;
    else if (a === '--child') o.child = Number(argv[++i]);
    else o.dir = a;
  }
  return o;
}

const OPT = parseArgs(process.argv.slice(2));
if (OPT.child === false) parent(); else child(OPT.child);

// ------------------------------------------------------------------ parent
function parent() {
  const t0 = Date.now();
  const stats = {};
  let iterations = 0, iterationErrors = 0, vuserErrors = 0, done = 0;
  const errors = [];
  for (let u = 1; u <= OPT.vusers; u++) {
    const c = fork(__filename, [...process.argv.slice(2), '--child', String(u)], { stdio: ['ignore', 'inherit', 'inherit', 'ipc'] });
    c.on('message', (m) => {
      if (m.type === 'tx') {
        const s = stats[m.name] || (stats[m.name] = { pass: 0, fail: 0, ms: 0 });
        s[m.status === 'Passed' ? 'pass' : 'fail']++; s.ms += m.ms;
      } else if (m.type === 'iteration') { iterations++; if (!m.ok) { iterationErrors++; errors.push(`u${m.user}: ${m.error}`); } }
      else if (m.type === 'vuserError') { vuserErrors++; errors.push(`u${m.user} (${m.phase}): ${m.error}`); }
    });
    c.on('exit', (code) => {
      if (code) { vuserErrors++; errors.push(`u${u}: vuser process exited with code ${code}`); }
      if (++done === OPT.vusers) report();
    });
  }
  function report() {
    const names = Object.keys(stats).sort();
    let fails = 0;
    console.log(`\n${path.basename(path.resolve(OPT.dir))}: ${OPT.vusers} vusers, ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    for (const n of names) {
      const s = stats[n]; fails += s.fail;
      console.log(`  ${s.fail ? 'FAIL' : 'ok  '} ${n.padEnd(52)} pass ${String(s.pass).padStart(4)}  fail ${String(s.fail).padStart(3)}  avg ${Math.round(s.ms / (s.pass + s.fail))} ms`);
    }
    console.log(`  transactions: ${names.length} names, ${fails} failures; iterations ${iterations}, failed ${iterationErrors}; vuser errors ${vuserErrors}`);
    const seen = new Set();
    for (const e of errors) {
      const k = e.replace(/^u\d+: /, '').replace(/\s+/g, ' ').slice(0, 300);
      if (!seen.has(k)) { seen.add(k); console.log('  ! ' + e.replace(/\s+/g, ' ').slice(0, 300)); }
    }
    process.exit(fails || iterationErrors || vuserErrors ? 1 : 0);
  }
}

// ------------------------------------------------------------------ child (one vuser)
function child(userId) {
  const send = (m) => process.send && process.send(m);
  class Exit extends Error { constructor(type, msg) { super(msg || type); this.exitType = type; } }
  const sleepMs = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  const cb = { initialize: [], action: [], finalize: [] };
  let iter = 0;

  function toWire(o) {
    const headers = { ...(o.headers || {}) };
    let body = o.body;
    if (body !== undefined && body !== null && !Buffer.isBuffer(body) && typeof body === 'object') {
      const ct = Object.entries(headers).find(([k]) => k.toLowerCase() === 'content-type');
      if (ct && /json/.test(ct[1])) body = JSON.stringify(body);
      else body = Object.entries(body).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join(o.formDelimiter || '&');
    }
    const w = { url: o.url, method: o.method || 'GET', headers, noRedirect: !!o.disableRedirection };
    if (body !== undefined && body !== null) w.bodyB64 = (Buffer.isBuffer(body) ? body : Buffer.from(String(body), 'utf8')).toString('base64');
    return w;
  }
  function fromWire(o, out) {
    if (out.error) throw new Error(`request to ${o.url} failed: ${out.error}`);
    const buf = Buffer.from(out.bodyB64, 'base64');
    const res = { status: out.status, headers: out.headers, size: buf.length };
    if (o.returnBody) {
      res.body = o.isBinaryResponse ? buf : buf.toString('utf8');
      if (!o.isBinaryResponse) { try { res.jsonBody = JSON.parse(res.body); } catch (e) { /* not json */ } }
    }
    if (res.status >= 400 && o.handleHTTPError === null) throw new Exit('iteration', `HTTP ${res.status} (unhandled)`);
    if (res.status >= 400 && typeof o.handleHTTPError === 'function' && o.handleHTTPError(res) !== false) throw new Exit('iteration', `HTTP ${res.status}`);
    return res;
  }

  const defaults = { method: 'GET', headers: {}, returnBody: false, handleHTTPError: null };
  class WebRequest {
    constructor(o) { Object.assign(this, defaults, typeof o === 'string' ? { url: o } : o); }
    sendSync() {
      const r = spawnSync(process.execPath, [path.join(__dirname, 'fetch-sync.js')],
        { input: JSON.stringify(toWire(this)), maxBuffer: 256 * 1024 * 1024 });
      return fromWire(this, JSON.parse(r.stdout.toString('utf8') || '{"error":"no output"}'));
    }
    async send() { return fromWire(this, await require('./fetch-sync.js').doFetch(toWire(this))); }
  }
  WebRequest.defaults = defaults;

  const TransactionStatus = { Passed: 'Passed', Failed: 'Failed' };
  class Transaction {
    constructor(name) { this.name = name; this.state = 'NotStarted'; }
    start() { this.t0 = Date.now(); this.state = 'InProgress'; }
    stop(status) { this.state = 'Ended'; send({ type: 'tx', name: this.name, status: status || 'Passed', ms: Date.now() - this.t0 }); }
  }
  function totp(secret, ts) {
    const alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    let bits = '';
    for (const ch of secret.replace(/=+$/, '').toUpperCase()) bits += alpha.indexOf(ch).toString(2).padStart(5, '0');
    const key = Buffer.from(bits.match(/.{8}/g).map((b) => parseInt(b, 2)));
    const ctr = Buffer.alloc(8); ctr.writeBigUInt64BE(BigInt(Math.floor((ts === undefined ? Date.now() : ts) / 30000)));
    const h = crypto.createHmac('sha1', key).update(ctr).digest();
    const o = h[h.length - 1] & 15;
    return String(((h.readUInt32BE(o) & 0x7fffffff) % 1e6)).padStart(6, '0');
  }
  const load = {
    initialize: (n, f) => cb.initialize.push(f || n), action: (n, f) => cb.action.push(f || n),
    finalize: (n, f) => cb.finalize.push(f || n),
    WebRequest, Transaction, TransactionStatus,
    LogLevel: { error: 'error', warning: 'warning', info: 'info', debug: 'debug', trace: 'trace' },
    ExitType: { iteration: 'iteration', stop: 'stop', abort: 'abort' },
    log: (m, level) => {
      if (OPT.quiet && level !== 'error') return;
      const text = OPT.quiet ? String(m).replace(/\s+/g, ' ').slice(0, 500) : m;
      console.log(`[u${userId}${level ? ' ' + level : ''}] ${text}`);
    },
    sleep: (s) => sleepMs(s * 1000), thinkTime: (s) => sleepMs(s * 1000),
    sleepAsync: (s) => new Promise((r) => setTimeout(r, s * 1000)),
    exit: (type, msg) => { throw new Exit(type, msg); },
    config: { user: { userId, args: OPT.args }, runtime: { get iteration() { return iter; } },
              script: { name: path.basename(path.resolve(OPT.dir)), directory: path.resolve(OPT.dir) },
              host: { name: 'mock', platform: process.platform }, env: process.env },
    global: {},
    utils: {
      base64Encode: (v) => Buffer.from(String(v), 'utf8').toString('base64'),
      base64Decode: (v) => Buffer.from(String(v), 'base64').toString('utf8'),
      uuid: () => crypto.randomUUID(),
      randomString: (n, o = {}) => {
        const pool = (o.letters !== false ? 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ' : '') + (o.digits !== false ? '0123456789' : '');
        return Array.from({ length: n }, () => pool[crypto.randomInt(pool.length)]).join('');
      },
      totp,
    },
  };
  global.load = load;
  const code = fs.readFileSync(path.join(path.resolve(OPT.dir), 'main.js'), 'utf8');
  new Function('load', 'Buffer', code)(load, Buffer);

  (async () => {
    const end = Date.now() + OPT.duration * 1000;
    let stopped = false;
    try { for (const f of cb.initialize) await f(); }
    catch (e) { send({ type: 'vuserError', user: userId, phase: 'initialize', error: e.message }); process.exit(0); }
    while (!stopped && Date.now() < end) {
      iter++;
      try { for (const f of cb.action) await f(); send({ type: 'iteration', user: userId, ok: true }); }
      catch (e) {
        if (e instanceof Exit && e.exitType === 'stop') { stopped = true; send({ type: 'iteration', user: userId, ok: true }); }
        else if (e instanceof Exit && e.exitType === 'abort') process.exit(1);
        else send({ type: 'iteration', user: userId, ok: e instanceof Exit && e.exitType === 'iteration', error: e.message });
      }
      if (!stopped && Date.now() < end) sleepMs(OPT.pacing * 1000);
    }
    try { for (const f of cb.finalize) await f(); }
    catch (e) { send({ type: 'vuserError', user: userId, phase: 'finalize', error: e.message }); }
    process.exit(0);
  })();
}
