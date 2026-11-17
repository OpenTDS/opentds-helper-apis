// Tests for the lists service (src/lists*.js) against moto_server.
//
// Run:   cd v1/tests/node && npm install && node --test
//   or:  pytest v1/tests/test_lists.py      (wraps the same command)
//
// Starts its own moto_server on a free port unless AWS_ENDPOINT_URL is
// already set. The API runs the background worker in-process
// (LOCAL_WORKER=1) instead of invoking the worker Lambda.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const Module = require('node:module');

// The Lambda runtime provides the AWS SDK; here it comes from this folder's
// node_modules, which src/ cannot see on its own.
process.env.NODE_PATH = path.join(__dirname, 'node_modules');
Module._initPaths();

const SRC = path.resolve(__dirname, '..', '..', 'src');
const BASE = 'https://api.example.com/v1';

Object.assign(process.env, {
  AWS_ACCESS_KEY_ID: 'testing',
  AWS_SECRET_ACCESS_KEY: 'testing',
  AWS_SESSION_TOKEN: 'testing-token',
  AWS_DEFAULT_REGION: 'ap-southeast-2',
  AWS_REGION: 'ap-southeast-2',
  LISTS_TABLE: 'lists-records',
  VARS_TABLE: 'lists-vars',
  COUNTERS_TABLE: 'lists-counters',
  CLEANUP_TABLE: 'lists-cleanup',
  IMPORT_BUCKET: 'lists-imports-test',
  WORKER_FUNCTION: 'lists-worker',
  BASE_URL: BASE,
  LOCAL_WORKER: '1',
  CLEANUP_GRACE_MS: '0',
});

let moto;
let lists;
let store;
let imp;

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => { const { port } = srv.address(); srv.close(() => resolve(port)); });
    srv.on('error', reject);
  });
}

async function waitForPort(port, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const ok = await new Promise((r) => {
      const s = net.connect(port, '127.0.0.1', () => { s.end(); r(true); });
      s.on('error', () => r(false));
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error('moto_server did not start');
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test.before(async () => {
  if (!process.env.AWS_ENDPOINT_URL) {
    const port = await freePort();
    // Single-threaded moto (see moto_serial.py); MOTO_SERVER=<path> uses a stock moto_server instead.
    moto = process.env.MOTO_SERVER
      ? spawn(process.env.MOTO_SERVER, ['-p', String(port)], { stdio: 'ignore' })
      : spawn(process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3'),
          [path.join(__dirname, 'moto_serial.py'), String(port)], { stdio: 'ignore' });
    await waitForPort(port);
    process.env.AWS_ENDPOINT_URL = `http://127.0.0.1:${port}`;
  }
  lists = require(path.join(SRC, 'lists.js'));
  store = require(path.join(SRC, 'lists_store.js'));
  imp = require(path.join(SRC, 'lists_import.js'));

  const { DynamoDB } = require('@aws-sdk/client-dynamodb');
  const ddb = new DynamoDB({});
  const mk = (name, sk, skType) => ddb.createTable({
    TableName: name,
    BillingMode: 'PAY_PER_REQUEST',
    AttributeDefinitions: [{ AttributeName: 'key', AttributeType: 'S' }, { AttributeName: sk, AttributeType: skType }],
    KeySchema: [{ AttributeName: 'key', KeyType: 'HASH' }, { AttributeName: sk, KeyType: 'RANGE' }],
  });
  await mk('lists-records', 'index', 'N');
  await mk('lists-vars', 'name', 'S');
  await mk('lists-counters', 'name', 'S');
  await mk('lists-cleanup', 'index', 'N');
  await store.s3.createBucket({
    Bucket: 'lists-imports-test',
    CreateBucketConfiguration: { LocationConstraint: 'ap-southeast-2' },
  });
});

test.after(async () => {
  await sleep(300);   // let detached local-worker cleanups finish
  if (moto) moto.kill();
});

// A realistic API Gateway REST proxy event.
function event(action, qs = {}, { method = 'GET', body = null, base64 = false, headers = {} } = {}) {
  return {
    resource: '/lists/{proxy+}',
    path: `/v1/lists/${action}`,
    httpMethod: method,
    headers: { Host: 'api.example.com', 'User-Agent': 'node-test', ...headers },
    multiValueHeaders: {},
    queryStringParameters: Object.keys(qs).length ? qs : null,
    multiValueQueryStringParameters: null,
    pathParameters: action === null ? null : { proxy: action },
    stageVariables: null,
    requestContext: { stage: 'v1', identity: { sourceIp: '203.0.113.9' }, httpMethod: method },
    body: body === null ? null : (base64 ? Buffer.from(body).toString('base64') : body),
    isBase64Encoded: base64,
  };
}

async function call(action, qs, opts) {
  const res = await lists.handler(event(action, qs, opts));
  let json = null;
  if (/json/.test(res.headers['Content-Type'])) json = JSON.parse(res.body);
  return { status: res.statusCode, json, res };
}

async function pollDone(key, ref, ms = 20000) {
  const end = Date.now() + ms;
  for (;;) {
    const r = await call('import/status', { key, ref });
    if (r.json.state === 'done' || r.json.state === 'failed' || Date.now() > end) return r;
    await sleep(150);
  }
}

async function rowsFor(pkPrefix) {
  const out = [];
  let ExclusiveStartKey;
  do {
    const page = await store.dynamo.scan({ TableName: 'lists-records', ExclusiveStartKey });
    for (const it of page.Items || []) if (String(it.key).startsWith(pkPrefix)) out.push(it);
    ExclusiveStartKey = page.LastEvaluatedKey;
  } while (ExclusiveStartKey);
  return out;
}

// ---------------------------------------------------------------- routing
test('service index, unknown action, OPTIONS', async () => {
  const idx = await call(null);
  assert.equal(idx.status, 200);
  assert.equal(idx.json.service, 'lists');
  for (const a of ['push', 'pop', 'cyclepop', 'vars/list', 'counters/add', 'cleanup/list', 'upload/url', 'import/status']) {
    assert.ok(idx.json.actions.includes(a), a);
  }
  const nf = await call('nope', { key: 'k1' });
  assert.equal(nf.status, 404);
  assert.equal(nf.json.status, 404);
  assert.ok(nf.json.actions.includes('pop'));
  const opt = await lists.handler(event('pop', {}, { method: 'OPTIONS' }));
  assert.equal(opt.statusCode, 204);
});

test('bad or missing key is a 400', async () => {
  let r = await call('pop', { key: 'bad key!' });
  assert.equal(r.status, 400);
  assert.equal(r.json.received, 'bad key!');
  r = await call('pop', { key: 'a#b' });
  assert.equal(r.status, 400);
  r = await call('push', {});
  assert.equal(r.status, 400);
  assert.match(r.json.error, /key/);
});

// ---------------------------------------------------------------- queue
test('push, pop, exhausted, reset', async () => {
  const key = 'queue-1';
  const p1 = await call('push', { KEY: key, Email: 'a@example.com', plan: 'gold' });
  assert.equal(p1.status, 200);
  assert.equal(p1.json.key, key);
  assert.equal(p1.json.index, 1);
  assert.ok(p1.json.createdTime > 1.7e9);
  assert.deepEqual(p1.json.record, { Email: 'a@example.com', plan: 'gold', key, index: 1, createdTime: p1.json.createdTime });
  await call('push', { key, Email: 'b@example.com' });

  const bad = await call('push', { key, index: '9' });
  assert.equal(bad.status, 400);
  assert.deepEqual(bad.json.reserved, ['key', 'index', 'createdTime']);

  let r = await call('pop', { key });
  assert.equal(r.json.exhausted, false);
  assert.equal(r.json.index, 1);
  assert.equal(r.json.remaining, 1);
  assert.equal(r.json.record.Email, 'a@example.com');
  r = await call('pop', { key });
  assert.equal(r.json.record.Email, 'b@example.com');
  assert.equal(r.json.record.key, key);
  r = await call('pop', { key });
  assert.equal(r.status, 200);
  assert.equal(r.json.exhausted, true);
  assert.equal(r.json.record, null);

  r = await call('reset', { key });
  assert.deepEqual([r.json.reset, r.json.count, r.json.nextPop], [true, 2, 1]);
  r = await call('pop', { key });
  assert.equal(r.json.index, 1);

  r = await call('reset', { key: 'never-used' });
  assert.equal(r.status, 200);
  assert.equal(r.json.reset, false);
  r = await call('pop', { key: 'never-used' });
  assert.equal(r.json.exhausted, true);
});

test('cyclepop wraps round, numbers the passes and shares pop\'s pointer', async () => {
  const key = 'cyc-' + Date.now();
  let r = await call('cyclepop', { key });
  assert.equal(r.status, 200);
  assert.equal(r.json.exhausted, true);
  assert.equal(r.json.empty, true);
  assert.equal(r.json.record, null);
  for (const n of ['a', 'b', 'c']) await call('push', { key, n });
  const seen = [];
  for (let i = 0; i < 7; i++) {
    r = await call('cyclepop', { key });
    assert.equal(r.json.exhausted, false);
    seen.push([r.json.index, r.json.pass, r.json.record.n, r.json.wrapped, r.json.remaining]);
  }
  assert.deepEqual(seen, [
    [1, 1, 'a', false, 2], [2, 1, 'b', false, 1], [3, 1, 'c', false, 0],
    [1, 2, 'a', true, 2], [2, 2, 'b', false, 1], [3, 2, 'c', false, 0],
    [1, 3, 'a', true, 2],
  ]);
  // Shared pointer: list shows it, plain pop sees the key as used up, reset starts over.
  r = await call('list', { key, limit: 1 });
  assert.equal(r.json.nextPop, 8);
  assert.equal((await call('pop', { key })).json.exhausted, true);
  await call('reset', { key });
  r = await call('cyclepop', { key });
  assert.equal(r.json.index, 1);
  assert.equal(r.json.pass, 1);
  // pop then cyclepop continue from the same place
  assert.equal((await call('pop', { key })).json.index, 2);
  assert.equal((await call('cyclepop', { key })).json.index, 3);
  // A push makes the cycle longer from the next call on.
  await call('push', { key, n: 'd' });
  assert.equal((await call('cyclepop', { key })).json.record.n, 'd');
  assert.equal((await call('cyclepop', { key })).json.record.n, 'a');
});

test('cyclepop: concurrent callers get different records, each record once per pass', async () => {
  const key = 'cyc-par-' + Date.now();
  for (let i = 1; i <= 10; i++) await call('push', { key, n: String(i) });
  const res = await Promise.all(Array.from({ length: 30 }, () => call('cyclepop', { key })));
  const byPass = {};
  for (const r of res) (byPass[r.json.pass] ||= []).push(r.json.index);
  assert.deepEqual(Object.keys(byPass).sort(), ['1', '2', '3']);
  for (const p of Object.values(byPass)) assert.deepEqual(p.sort((a, b) => a - b), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  for (const r of res) assert.equal(r.json.record.n, String(r.json.index));
});

test('cyclepop on an imported (sharded) key, and a cleared key is empty again', async () => {
  const key = 'cyc-imp-' + Date.now();
  const csv = 'n\n' + Array.from({ length: 5 }, (_, i) => `r${i + 1}`).join('\n') + '\n';
  const r0 = await call('import', { key, format: 'csv' }, { method: 'POST', body: csv, base64: true, headers: { 'Content-Type': 'text/csv' } });
  assert.equal(r0.json.state, 'done');
  const got = [];
  for (let i = 0; i < 6; i++) got.push((await call('cyclepop', { key })).json.record.n);
  assert.deepEqual(got, ['r1', 'r2', 'r3', 'r4', 'r5', 'r1']);
  await call('clear', { key });
  assert.equal((await call('cyclepop', { key })).json.empty, true);
});

test('random', async () => {
  let r = await call('random', { key: 'rnd-empty' });
  assert.equal(r.status, 200);
  assert.equal(r.json.empty, true);
  assert.equal(r.json.record, null);
  for (let i = 0; i < 3; i++) await call('push', { key: 'rnd', n: String(i) });
  r = await call('random', { key: 'rnd' });
  assert.equal(r.json.empty, false);
  assert.equal(r.json.count, 3);
  assert.ok(r.json.index >= 1 && r.json.index <= 3);
  assert.equal(r.json.record.n, String(r.json.index - 1));
});

test('list with start/limit, then clear', async () => {
  const key = 'lst';
  for (let i = 1; i <= 5; i++) await call('push', { key, n: String(i) });
  let r = await call('list', { key });
  assert.equal(r.json.count, 5);
  assert.equal(r.json.returned, 5);
  assert.equal(r.json.truncated, false);
  assert.equal(r.json.nextStart, null);
  assert.equal(r.json.variables, undefined);
  assert.deepEqual(r.json.records.map((x) => x.n), ['1', '2', '3', '4', '5']);
  r = await call('list', { key, start: '2', limit: '2' });
  assert.deepEqual(r.json.records.map((x) => x.index), [2, 3]);
  assert.equal(r.json.nextStart, 4);

  r = await call('clear', { key });
  assert.equal(r.json.cleared, true);
  assert.equal(r.json.deleted, 5);
  r = await call('list', { key });
  assert.equal(r.json.count, 0);
  assert.deepEqual(r.json.records, []);
  r = await call('clear', { key });
  assert.equal(r.json.cleared, false);
});

test('codes', async () => {
  const r = await call('codes');
  assert.equal(r.status, 200);
  for (const n of [3, 4, 6, 8, 10]) assert.match(r.json[`rand${n}`], new RegExp(`^[A-Z0-9]{${n}}$`));
});

// ---------------------------------------------------------------- vars
test('vars set/get/list', async () => {
  const key = 'vars-1';
  let r = await call('vars/set', { key, name: 'baseUrl', value: 'https://sut.example.com/api?x=1&y=2' });
  assert.equal(r.status, 200);
  assert.equal(r.json.value, 'https://sut.example.com/api?x=1&y=2');
  assert.ok(r.json.savedTime > 1.7e9);
  await call('vars/set', { key, name: 'empty', value: '' });
  r = await call('vars/get', { key, name: 'baseUrl' });
  assert.equal(r.json.value, 'https://sut.example.com/api?x=1&y=2');
  r = await call('vars/get', { key, name: 'empty' });
  assert.equal(r.json.value, '');
  r = await call('vars/get', { key, name: 'missing' });
  assert.equal(r.status, 404);
  assert.equal(r.json.name, 'missing');
  r = await call('vars/set', { key, name: 'x' });
  assert.equal(r.status, 400);
  r = await call('vars/list', { key });
  assert.equal(r.json.count, 2);
  assert.deepEqual(r.json.variables.map((v) => v.name).sort(), ['baseUrl', 'empty']);
});

test('vars/get wait and equals: the setup gate', async () => {
  const key = 'vars-gate';
  // equals on a variable that was never set: 200, exists false (not 404)
  let r = await call('vars/get', { key, name: 'setupReady', equals: '1' });
  assert.equal(r.status, 200);
  assert.deepEqual([r.json.exists, r.json.matched, r.json.value], [false, false, null]);
  // wait without equals: wait for the variable to exist
  setTimeout(() => call('vars/set', { key, name: 'namespace', value: 'NS1' }), 600);
  r = await call('vars/get', { key, name: 'namespace', wait: '5' });
  assert.equal(r.status, 200);
  assert.equal(r.json.exists, true);
  assert.equal(r.json.value, 'NS1');
  assert.ok(r.json.waitedMs >= 400 && r.json.waitedMs < 4000, r.json.waitedMs);
  // already matching: returns at once
  await call('vars/set', { key, name: 'setupReady', value: '0' });
  r = await call('vars/get', { key, name: 'setupReady', equals: '0', wait: '20' });
  assert.equal(r.json.matched, true);
  assert.ok(r.json.waitedMs < 1000);
  // 0 -> 1 while waiting
  setTimeout(() => call('vars/set', { key, name: 'setupReady', value: '1' }), 700);
  r = await call('vars/get', { key, name: 'setupReady', equals: '1', wait: '20' });
  assert.deepEqual([r.json.matched, r.json.value, r.json.equals], [true, '1', '1']);
  assert.ok(r.json.waitedMs >= 500 && r.json.waitedMs < 5000, r.json.waitedMs);
  // a change to something else (failed) is reported at once, not after the wait
  await call('vars/set', { key, name: 'setupReady', value: '0' });
  setTimeout(() => call('vars/set', { key, name: 'setupReady', value: 'failed' }), 500);
  r = await call('vars/get', { key, name: 'setupReady', equals: '1', wait: '20' });
  assert.deepEqual([r.json.matched, r.json.value], [false, 'failed']);
  assert.ok(r.json.waitedMs < 5000, r.json.waitedMs);
  // nothing happens: returns when the wait runs out
  r = await call('vars/get', { key, name: 'setupReady', equals: '1', wait: '1' });
  assert.equal(r.json.matched, false);
  assert.ok(r.json.waitedMs >= 1000 && r.json.waitedMs < 3000, r.json.waitedMs);
  // comparison is exact (case-sensitive, no trimming)
  r = await call('vars/get', { key, name: 'setupReady', equals: 'FAILED' });
  assert.equal(r.json.matched, false);
  // wait is clamped, never rejected; a plain get keeps its 404
  r = await call('vars/get', { key, name: 'nothing', wait: '-5' });
  assert.deepEqual([r.status, r.json.exists], [200, false]);
  r = await call('vars/get', { key, name: 'nothing' });
  assert.equal(r.status, 404);
  r = await call('vars/get', { key, name: 'x', wait: 'soon' });
  assert.equal(r.status, 400);
});

// ---------------------------------------------------------------- counters
test('counters add/get', async () => {
  const key = 'ctr';
  let r = await call('counters/get', { key });
  assert.deepEqual([r.json.name, r.json.value, r.json.exists], ['default', 0, false]);
  r = await call('counters/add', { key });
  assert.equal(r.json.value, 1);
  r = await call('counters/add', { key, increment: '10' });
  assert.equal(r.json.value, 11);
  r = await call('counters/add', { key, increment: '-3', name: 'orders' });
  assert.equal(r.json.value, -3);
  r = await call('counters/add', { key, increment: '0' });
  assert.equal(r.json.value, 11);
  assert.ok(r.json.updatedTime > 1.7e9);
  r = await call('counters/get', { key });
  assert.deepEqual([r.json.value, r.json.exists], [11, true]);
  r = await call('counters/add', { key, increment: '1.5' });
  assert.equal(r.status, 400);
  r = await call('counters/add', { key, name: 'bad name' });
  assert.equal(r.status, 400);
});

// ---------------------------------------------------------------- cleanup
test('cleanup push/pop/list', async () => {
  const key = 'cln';
  let r = await call('cleanup/push', { key, value: 'ORDER-1', note: 'vu1' });
  assert.deepEqual([r.json.index, r.json.registered, r.json.note], [1, true, 'vu1']);
  await call('cleanup/push', { key, value: 'ORDER-2' });
  assert.equal((await call('cleanup/push', { key })).status, 400);

  r = await call('cleanup/pop', { key });
  assert.deepEqual([r.json.exhausted, r.json.index, r.json.value, r.json.note], [false, 1, 'ORDER-1', 'vu1']);
  assert.ok(r.json.removedTime >= r.json.savedTime);

  r = await call('cleanup/list', { key, status: 'pending' });
  assert.deepEqual([r.json.saved, r.json.removed, r.json.pending, r.json.nextPop], [2, 1, 1, 2]);
  assert.equal(r.json.filter, 'pending');
  assert.deepEqual(r.json.records.map((x) => x.value), ['ORDER-2']);
  r = await call('cleanup/list', { key });
  assert.equal(r.json.returned, 2);
  assert.equal(r.json.records[0].removedTime > 0, true);
  assert.equal(r.json.records[1].removedTime, null);

  await call('cleanup/pop', { key });
  r = await call('cleanup/pop', { key });
  assert.equal(r.status, 200);
  assert.equal(r.json.exhausted, true);
  assert.equal((await call('cleanup/list', { key, status: 'bogus' })).status, 400);
});

// ---------------------------------------------------------------- import
const CSV = 'email,key,postcode\r\nalice@example.com,K1,0800\r\n"bob@example.com","K2","3000"\r\n\r\ncarol@example.com,K3\r\n';

test('CSV import by POST body waits for done; records replace pushed ones', async () => {
  const key = 'imp-csv';
  await call('push', { key, old: 'row' });
  const r = await call('import', { key, format: 'csv' }, { method: 'POST', body: CSV, headers: { 'Content-Type': 'text/csv' } });
  assert.equal(r.status, 200, JSON.stringify(r.json));
  const st = r.json;
  assert.equal(st.state, 'done');
  assert.equal(st.loaded, 3);
  assert.equal(st.source, 'body');
  assert.deepEqual(st.columns, ['email', 'key_2', 'postcode']);
  assert.ok(st.warnings.some((w) => /reserved/.test(w)));
  assert.ok(st.warnings.some((w) => /fewer fields/.test(w)));
  assert.equal(st.blankRows, 1);
  assert.deepEqual(st.replaced, { count: 1, importRef: null });
  assert.equal(st.sample.postcode, '0800');
  assert.equal(st.sample.index, 1);
  assert.equal(st.next.pop, `${BASE}/lists/pop?key=${key}`);
  assert.match(st.next.reload, new RegExp(`/lists/import\\?key=${key}&format=csv&ref=${st.ref}$`));
  assert.ok(st.queuedTime && st.finishedTime && st.updatedTime);

  let p = await call('pop', { key });
  assert.deepEqual(p.json.record, {
    email: 'alice@example.com', key_2: 'K1', postcode: '0800', key, index: 1, createdTime: p.json.record.createdTime,
  });
  p = await call('list', { key });
  assert.equal(p.json.count, 3);
  assert.equal(p.json.importRef, st.ref);
  assert.equal(p.json.records[2].postcode, '');

  await sleep(200);   // the replaced plain rows are deleted in the background
  const plain = (await rowsFor(key)).filter((it) => it.key === key && it.index > 0);
  assert.equal(plain.length, 0);

  // push onto an imported key appends to the current generation
  const pushed = await call('push', { key, email: 'dave@example.com' });
  assert.equal(pushed.json.index, 4);
  const got = await call('list', { key, start: '4' });
  assert.equal(got.json.records[0].email, 'dave@example.com');
});

test('JSON import by base64 POST body, wait=true', async () => {
  const key = 'imp-json';
  const body = '[{"id": 123456789012345678901, "name": "A", "tags": ["x"], "index": 7},\n' +
    ' {"id": 2, "name": "B", "nested": {"a": {"b": 1}}},\n "scalar"]';
  const r = await call('import', { key, format: 'JSON', wait: 'true' }, { method: 'POST', body, base64: true });
  assert.equal(r.status, 200, JSON.stringify(r.json));
  assert.equal(r.json.shape, 'array');
  assert.ok(r.json.warnings.some((w) => /stored as \{"value"/.test(w)));
  assert.equal(r.json.loaded, 3);
  assert.ok(r.json.warnings.some((w) => /kept exactly/.test(w)));
  assert.ok(r.json.warnings.some((w) => /"index" -> index_2/.test(w)));
  const p = await call('pop', { key });
  assert.equal(p.json.record.id, '123456789012345678901');
  assert.equal(p.json.record.index_2, 7);
  assert.equal(p.json.record.index, 1);
  assert.deepEqual(p.json.record.tags, ['x']);
  const l = await call('list', { key });
  assert.deepEqual(l.json.records[1].nested, { a: { b: 1 } });
  assert.equal(l.json.records[2].value, 'scalar');
});

test('import wait=0 returns queued; import/status reaches done; no-ref status lists recent', async () => {
  const key = 'imp-async';
  const r = await call('import', { key, format: 'csv', wait: '0' }, { method: 'POST', body: 'a,b\n1,2\n3,4\n' });
  assert.equal(r.status, 202);
  assert.equal(r.json.state, 'queued');
  const done = await pollDone(key, r.json.ref);
  assert.equal(done.status, 200);
  assert.equal(done.json.loaded, 2);

  const s = await call('import/status', { key });
  assert.equal(s.json.ref, r.json.ref);
  assert.deepEqual(s.json.recent.map((x) => x.format), ['csv']);
  assert.equal((await call('import/status', { key: 'no-uploads' })).status, 404);
  assert.equal((await call('import/status', { key, ref: 'nonsense' })).status, 400);
});

test('failed import is 422 and leaves the key unchanged', async () => {
  const key = 'imp-fail';
  await call('push', { key, keep: 'me' });
  const r = await call('import', { key, format: 'csv' }, { method: 'POST', body: 'a,b\n"unclosed,1\n' });
  assert.equal(r.status, 422);
  assert.equal(r.json.status, 422);
  assert.equal(r.json.state, 'failed');
  assert.match(r.json.error, /never closed/);
  const p = await call('pop', { key });
  assert.equal(p.json.record.keep, 'me');

  const j = await call('import', { key, format: 'json' }, { method: 'POST', body: '[{"a":1},' });
  assert.equal(j.status, 422);
  assert.match(j.json.error, /Invalid JSON/);
});

test('import parameter errors', async () => {
  const key = 'imp-err';
  let r = await call('import', { key }, { method: 'POST', body: 'a\n1\n' });
  assert.equal(r.status, 400);
  assert.deepEqual(r.json.allowed, ['csv', 'json']);
  r = await call('import', { key, format: 'xml' });
  assert.equal(r.status, 400);
  r = await call('import', { key, format: 'csv' });
  assert.equal(r.status, 400);
  assert.match(r.json.error, /Nothing to import/);
  r = await call('import', { key, format: 'csv', delimiter: 'colon' }, { method: 'POST', body: 'a\n1\n' });
  assert.equal(r.status, 400);
  r = await call('import', { key, format: 'csv', wait: 'soon' }, { method: 'POST', body: 'a\n1\n' });
  assert.equal(r.status, 400);
  r = await call('import', { key, format: 'csv', ref: 'bad' });
  assert.equal(r.status, 400);
  r = await call('import', { key, format: 'csv', ref: '20260101T000000Z-abcdef' });
  assert.equal(r.status, 404);
});

test('upload/url shape, then import by ref (GET), reload, and a semicolon file', async () => {
  const key = 'imp-ref';
  const u = await call('upload/url', { key, format: 'csv', size: '100' });
  assert.equal(u.status, 200);
  const j = u.json;
  assert.equal(j.method, 'POST');
  assert.equal(j.url, 'https://lists-imports-test.s3.ap-southeast-2.amazonaws.com/');
  assert.equal(j.fields.key, `imports/${key}/${j.ref}.csv`);
  assert.equal(j.fields['x-amz-algorithm'], 'AWS4-HMAC-SHA256');
  assert.equal(j.fields['x-amz-security-token'], 'testing-token');
  assert.match(j.fields['x-amz-signature'], /^[0-9a-f]{64}$/);
  const policy = JSON.parse(Buffer.from(j.fields.policy, 'base64').toString());
  assert.deepEqual(policy.conditions[0], { bucket: 'lists-imports-test' });
  assert.deepEqual(policy.conditions.at(-1), ['content-length-range', 1, 51200 * 1024]);
  assert.equal(j.maxKb, 51200);
  assert.equal(j.expiresInSeconds, 900);
  assert.equal(j.next, `${BASE}/lists/import?key=${key}&format=csv&ref=${j.ref}`);

  assert.equal((await call('upload/url', { key })).status, 400);
  assert.equal((await call('upload/url', { key, format: 'csv', size: String(60 * 1024 * 1024) })).status, 400);

  // Stand in for the browser's S3 POST.
  await store.s3.putObject({ Bucket: 'lists-imports-test', Key: j.fields.key, Body: 'name;city\nAnn;Perth\nBen;Hobart\n' });
  let st = await call('import/status', { key, ref: j.ref });
  assert.equal(st.json.state, 'uploaded');

  let r = await call('import', { key, format: 'csv', ref: j.ref });
  assert.equal(r.status, 200, JSON.stringify(r.json));
  assert.equal(r.json.source, 'upload');
  assert.equal(r.json.delimiter, ';');
  assert.equal((await call('pop', { key })).json.record.city, 'Perth');

  r = await call('import', { key, format: 'csv', ref: j.ref, wait: '25' });
  assert.equal(r.json.source, 'reload');
  assert.equal(r.json.replaced.importRef, j.ref);
  assert.equal((await call('pop', { key })).json.index, 1);
  assert.equal((await call('import', { key, format: 'json', ref: j.ref })).status, 404);
});

test('multi-shard import keeps order; clear hands shards to the worker', async () => {
  const key = 'imp-big';
  const n = 12600;   // > 12,500 records per shard -> 2 shards
  const body = Array.from({ length: n }, (_, i) => JSON.stringify({ n: i + 1 })).join('\n');
  const r = await call('import', { key, format: 'json' }, { method: 'POST', body });
  const st = r.json.state === 'done' ? r : await pollDone(key, r.json.ref, 60000);
  assert.equal(st.json.state, 'done', JSON.stringify(st.json));
  assert.equal(st.json.shards, 2);
  assert.equal(st.json.shape, 'json lines');

  const l = await call('list', { key, start: '12498', limit: '5' });
  assert.deepEqual(l.json.records.map((x) => x.n), [12498, 12499, 12500, 12501, 12502]);
  assert.equal(l.json.nextStart, 12503);
  const p = await call('pop', { key });
  assert.equal(p.json.record.n, 1);

  const c = await call('clear', { key });
  assert.equal(c.json.cleared, true);
  assert.equal(c.json.backgroundDelete, n);
  assert.equal((await call('pop', { key })).json.exhausted, true);
  const end = Date.now() + 30000;
  let left;
  do { await sleep(300); left = (await rowsFor(`${key}#`)).length; } while (left && Date.now() < end);
  assert.equal(left, 0);
});

// ---------------------------------------------------------------- page
test('upload page is HTML with the format preselected', async () => {
  const r = await call('upload', { format: 'json' });
  assert.equal(r.status, 200);
  assert.match(r.res.headers['Content-Type'], /^text\/html/);
  assert.match(r.res.body, /"format":"json"/);
  assert.match(r.res.body, /"base":"https:\/\/api\.example\.com\/v1"/);
  assert.doesNotMatch(r.res.body, /apikey|x-api-key|__[A-Z]+__/i);
  const d = await call('upload');
  assert.match(d.res.body, /"format":"csv"/);
  assert.equal((await call('upload', { format: 'xml' })).status, 400);
});

// ---------------------------------------------------------------- units
test('wait parameter: seconds, clamped; booleans map to 25/0', () => {
  const { Req } = require(path.join(SRC, 'common.js'));
  const w = (v) => imp._t.waitParam(new Req(event('import', v === undefined ? {} : { wait: v }), 'lists'));
  assert.equal(w(undefined), 25);
  assert.equal(w('0'), 0);
  assert.equal(w('1'), 1);
  assert.equal(w('99'), 25);
  assert.equal(w('-4'), 0);
  assert.equal(w('true'), 25);
  assert.equal(w('No'), 0);
  assert.throws(() => w('later'));
});
