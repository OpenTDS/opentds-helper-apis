// ============================================================================
// init - starts a test cycle (llms.txt recipe 4.12), then exits.
//
// Run it with ONE Vuser, before or at the same time as the other scripts:
// they wait at the gate (setupReady) until this script has finished.
//   1. setupReady=0 (close the gate)       2. clear the previous cycle's pools
//   3. new namespace from lists/codes      4. seed the shared data
//   5. verify every pool                   6. setupReady=1 (open the gate)
// On any failure it sets setupReady=failed, so waiting scripts stop at once.
// ============================================================================

const CSV_HEADER = 'username,password,tier,display';

function loginsCsv(ns) {
  const rows = [CSV_HEADER];
  for (let i = 1; i <= SEED.logins; i++) {
    const n = String(i).padStart(2, '0');
    rows.push(`reg-user-${n},Pw-${n}-${ns.slice(0, 4)},${i % 3 === 0 ? 'gold' : 'standard'},"Regression User ${n}, QA"`);
  }
  return rows.join('\r\n') + '\r\n';
}

function productsJson() {
  const items = [];
  for (let i = 1; i <= SEED.products; i++) {
    items.push({ sku: `SKU-${1000 + i}`, name: `Product ${i}`, price: 10 + i, inStock: i % 2 === 0, tags: ['regression', `p${i}`] });
  }
  return JSON.stringify({ products: items });
}

function setGate(value) {
  return call(`gate_set_${value}`, { path: '/lists/vars/set', query: { key: CONFIG.project, name: 'setupReady', value } },
    (r, b) => { eq(b.value, String(value), 'stored value'); });
}

// Previous cycle: remove its pools and saved files (variables and counters stay).
function clearPrevious() {
  const r = call('previous_namespace', { path: '/lists/vars/get', query: { key: CONFIG.project, name: 'namespace' }, expect: [200, 404] });
  if (r.status === 404) return;
  const old = r.json.value;
  call('previous_record', { path: '/lists/vars/set', query: { key: CONFIG.project, name: 'previousNamespace', value: old } });
  for (const pool of [POOLS.logins(old), POOLS.products(old)]) {
    call('previous_clear_pool', { path: '/lists/clear', query: { key: pool } }, (res, b) => { eq(b.key, pool, 'key'); });
  }
  for (const key of [POOLS.invoice(old), POOLS.invoiceLocked(old), POOLS.note(old)]) {
    call('previous_delete_file', { path: '/mail/attachments/delete', query: { key }, expect: [200, 404] });
  }
}

function seed(ns) {
  // Logins: CSV through lists/import (POST body), the pool cyclepop hands out.
  call('seed_logins_import', { method: 'POST', path: '/lists/import', query: { key: POOLS.logins(ns), format: 'csv' },
    headers: { 'Content-Type': 'text/csv' }, body: loginsCsv(ns) }, (r, b) => {
    eq(b.state, 'done', 'import state'); eq(b.loaded, SEED.logins, 'records loaded');
    eq(b.columns, ['username', 'password', 'tier', 'display'], 'columns');
    eq(b.sample.display, 'Regression User 01, QA', 'quoted field with a comma');
  });
  // Products: JSON object with one array property.
  call('seed_products_import', { method: 'POST', path: '/lists/import', query: { key: POOLS.products(ns), format: 'json' },
    headers: { 'Content-Type': 'application/json' }, body: productsJson() }, (r, b) => {
    eq(b.state, 'done', 'import state'); eq(b.loaded, SEED.products, 'records loaded');
    eq(b.sample.price, 11, 'numbers keep their JSON type'); eq(b.sample.tags, ['regression', 'p1'], 'arrays kept');
  });
  // Documents: base64 text bodies (text/plain), with file names.
  const files = [[POOLS.invoice(ns), 'regression-invoice.pdf', FIXTURES.invoicePdf, 'application/pdf'],
                 [POOLS.invoiceLocked(ns), 'regression-invoice-locked.pdf', FIXTURES.invoiceLockedPdf, 'application/pdf'],
                 [POOLS.note(ns), 'regression-note.docx', FIXTURES.noteDocx,
                  'application/vnd.openxmlformats-officedocument.wordprocessingml.document']];
  for (const [key, filename, data, type] of files) {
    call('seed_save_file', { method: 'POST', path: '/mail/attachments/save', query: { key, filename },
      headers: { 'Content-Type': 'text/plain' }, body: data }, (r, b) => {
      eq(b.result, 'saved', 'result'); eq(b.filename, filename, 'filename'); eq(b.contentType, type, 'content type');
      eq(b.bodyFormat, 'base64', 'body format');
    });
  }
  // A shared TOTP secret.
  call('seed_totp', { path: '/totp/set', query: { key: POOLS.totp(ns), user: 'shared', secret: SEED.totpSecret } },
    (r, b) => { eq(b.saved, true, 'saved'); });
  // Cycle facts the other scripts and a person debugging can read.
  call('seed_vars', { path: '/lists/vars/set', query: { key: ns, name: 'startedTime', value: String(nowSec()) } });
  call('seed_counter', { path: '/lists/counters/add', query: { key: ns, name: 'cycles', increment: 1 } });
}

function verify(ns) {
  call('verify_logins', { path: '/lists/list', query: { key: POOLS.logins(ns), limit: 1 } }, (r, b) => {
    ok(b.count >= SEED.logins, `logins pool has ${SEED.logins} records (count ${b.count})`); eq(b.nextPop, 1, 'pointer at 1');
  });
  call('verify_products', { path: '/lists/list', query: { key: POOLS.products(ns), limit: 1 } }, (r, b) => {
    ok(b.count >= SEED.products, `products pool has ${SEED.products} records`);
  });
  for (const key of [POOLS.invoice(ns), POOLS.invoiceLocked(ns), POOLS.note(ns)]) {
    call('verify_file', { path: '/mail/attachments/get', query: { key, url: 'true' } }, (r, b) => {
      eq(b.source, 'saved', 'source'); isStr(b.url, 'download url');
    });
  }
  call('verify_pdf_text', { path: '/mail/attachments/find', query: { key: POOLS.invoice(ns), left: 'Total including GST' } },
    (r, b) => { eq(b.values, ['$123.45'], 'invoice total'); });
  call('verify_totp', { path: '/totp/get', query: { key: POOLS.totp(ns), user: 'shared' } },
    (r, b) => { ok(/^\d{6}$/.test(b.code), 'six-digit code'); });
}

load.action('Initialise cycle', async function () {
  if (userId() > 1) load.exit(load.ExitType.stop, 'init runs in the first Vuser only');
  // A second init started at the same moment would overwrite the namespace.
  const lock = call('init_lock', { path: '/lock/acquire', query: { key: `${CONFIG.project}-init`, wait: 0 } }).json;
  if (!lock.locked) {
    load.log('init: another init holds the lock; leaving the cycle to it.', load.LogLevel.warning);
    load.exit(load.ExitType.stop, 'init already running');
  }
  try {
    setGate(0);
    clearPrevious();
    const ns = call('new_namespace', { path: '/lists/codes' }, (r, b) => {
      for (const n of [3, 4, 6, 8, 10]) ok(new RegExp(`^[A-Z0-9]{${n}}$`).test(b[`rand${n}`]), `rand${n}`);
    }).json.rand10;
    STATE.ns = ns;
    call('publish_namespace', { path: '/lists/vars/set', query: { key: CONFIG.project, name: 'namespace', value: ns } },
      (r, b) => { eq(b.value, ns, 'namespace stored'); });
    seed(ns);
    verify(ns);
    setGate(1);
    call('gate_open_check', { path: '/lists/vars/get', query: { key: CONFIG.project, name: 'setupReady', equals: '1' } },
      (r, b) => { eq(b.matched, true, 'gate open'); });
    load.log(`init: cycle ${ns} ready under ${CONFIG.project}`);
  } catch (e) {
    try { setGate('failed'); } catch (ignored) { /* best effort */ }
    throw e;
  } finally {
    call('init_unlock', { path: '/lock/release', query: { ref: lock.ref } });
  }
  load.exit(load.ExitType.stop, 'cycle initialised');
});
