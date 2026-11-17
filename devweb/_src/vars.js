// ============================================================================
// vars - lists/vars/set, lists/vars/get (incl. equals/wait), lists/vars/list.
// ============================================================================

const SPECIAL = 'https://shop.example.com/a?b=1&c=2#frag 100% A+B café ✓';

load.initialize(async function () { startScript(); });

load.action('vars', async function () {
  const key = uid('vars');
  const set = (name, value, tx) => call(tx || 'set', { path: '/lists/vars/set', query: { key, name, value } }, (r, b) => {
    eq(b.key, key, 'key echoed'); eq(b.name, name, 'name echoed'); eq(b.value, String(value), 'value echoed');
    between(b.savedTime, nowSec() - 300, nowSec() + 300, 'savedTime');
  });
  const get = (name, extra, tx, expect, check) =>
    call(tx || 'get', { path: '/lists/vars/get', query: { key, name, ...(extra || {}) }, expect }, check);

  // Never set: a plain get is a 404 naming the key and variable.
  get('orderId', null, 'get_missing_404', 404, (r, b) => { isError(r, 404, 'not found'); eq(b.name, 'orderId', 'name in 404 body'); });
  call('list_empty', { path: '/lists/vars/list', query: { key } }, (r, b) => { eq(b.count, 0, 'count'); eq(b.variables, [], 'variables'); });

  // Set, read back, overwrite.
  set('orderId', 'SO-1001');
  get('orderId', null, 'get_value', 200, (r, b) => eq(b.value, 'SO-1001', 'value'));
  set('orderId', 'SO-1002', 'set_overwrite');
  get('orderId', null, 'get_overwritten', 200, (r, b) => eq(b.value, 'SO-1002', 'overwritten value'));

  // Values with characters a query string mangles, unicode, and empty values.
  set('url', SPECIAL, 'set_special_chars');
  get('url', null, 'get_special_chars', 200, (r, b) => eq(b.value, SPECIAL, 'special characters round-trip'));
  set('blank', '', 'set_empty');
  get('blank', null, 'get_empty', 200, (r, b) => eq(b.value, '', 'empty string, not 404'));

  // Names are case-sensitive; long names; validation.
  get('OrderId', null, 'get_case_sensitive_404', 404, (r) => isError(r, 404));
  set('n'.repeat(256), 'max', 'set_name_256');
  call('set_name_257_400', { path: '/lists/vars/set', query: { key, name: 'n'.repeat(257), value: 'x' }, expect: 400 },
    (r) => isError(r, 400, '256'));
  call('set_missing_value_400', { path: '/lists/vars/set', query: { key, name: 'x' }, expect: 400 }, (r) => isError(r, 400, 'value'));
  call('set_missing_name_400', { path: '/lists/vars/set', query: { key, value: 'x' }, expect: 400 }, (r) => isError(r, 400, 'name'));
  call('get_missing_key_400', { path: '/lists/vars/get', query: { name: 'x' }, expect: 400 }, (r) => isError(r, 400, 'key'));
  call('get_bad_key_400', { path: '/lists/vars/get', query: { key: 'bad key!', name: 'x' }, expect: 400 }, (r) => isError(r, 400));
  // Parameter NAMES are case-insensitive.
  call('param_names_case_insensitive', { path: '/lists/vars/get', query: { KEY: key, Name: 'orderId' } },
    (r, b) => eq(b.value, 'SO-1002', 'value via KEY=/Name='));

  // vars/list: everything, sorted by name.
  call('list_all', { path: '/lists/vars/list', query: { key } }, (r, b) => {
    eq(b.count, 4, 'count');
    const names = b.variables.map((v) => v.name);
    eq(names, [...names].sort(), 'sorted by name');
    const m = Object.fromEntries(b.variables.map((v) => [v.name, v.value]));
    eq(m.orderId, 'SO-1002', 'orderId'); eq(m.blank, '', 'blank'); eq(m.url, SPECIAL, 'url');
    b.variables.forEach((v) => isNum(v.savedTime, 'savedTime'));
  });

  // equals without wait: answers at once, always 200.
  get('orderId', { equals: 'SO-1002' }, 'equals_match', 200,
    (r, b) => eq([b.matched, b.exists, b.equals, b.value], [true, true, 'SO-1002', 'SO-1002'], 'matched/exists/equals/value'));
  get('orderId', { equals: 'so-1002' }, 'equals_is_case_sensitive', 200, (r, b) => eq(b.matched, false, 'case-sensitive compare'));
  get('nothing', { equals: '1' }, 'equals_never_set_200', 200,
    (r, b) => eq([b.exists, b.matched, b.value, b.savedTime], [false, false, null, null], 'never set'));

  // wait on a variable that exists: no wait at all.
  get('orderId', { wait: 20 }, 'wait_exists_immediate', 200, (r, b) => {
    eq(b.exists, true, 'exists'); ok(b.waitedMs < 3000, `answered at once (${b.waitedMs} ms)`);
  });
  // wait runs out: about 2 s, exists false.
  get('nothing', { wait: 2 }, 'wait_runs_out', 200, (r, b) => {
    eq(b.exists, false, 'still not set'); between(b.waitedMs, 1500, 8000, 'waitedMs');
  });
  // Out-of-range wait is clamped, a non-number is a 400.
  get('nothing', { wait: -5 }, 'wait_negative_clamped', 200, (r, b) => ok(b.waitedMs < 3000, 'negative wait = no wait'));
  get('nothing', { wait: 'soon' }, 'wait_not_a_number_400', 400, (r) => isError(r, 400, 'whole number'));

  // The long poll: start a wait, then set the value; the wait must return early.
  set('gate', '0', 'gate_close');
  await stepAsync('wait_until_equal', async () => {
    const pending = httpAsync({ path: '/lists/vars/get', query: { key, name: 'gate', equals: '1', wait: 15 } });
    await load.sleepAsync(1.5);   // lets the pending request go out
    http({ path: '/lists/vars/set', query: { key, name: 'gate', value: '1' } });
    const r = await pending; STATE.current = r;
    eq(r.status, 200, 'status'); eq(r.json.matched, true, 'matched'); eq(r.json.value, '1', 'value');
    between(r.json.waitedMs, 800, 12000, 'returned soon after the set');
  });
  // A change to something else also ends the wait (0 -> failed).
  set('gate', '0', 'gate_reclose');
  await stepAsync('wait_ends_on_change', async () => {
    const pending = httpAsync({ path: '/lists/vars/get', query: { key, name: 'gate', equals: '1', wait: 15 } });
    await load.sleepAsync(1.5);   // lets the pending request go out
    http({ path: '/lists/vars/set', query: { key, name: 'gate', value: 'failed' } });
    const r = await pending; STATE.current = r;
    eq([r.json.matched, r.json.value], [false, 'failed'], 'reported the change');
    ok(r.json.waitedMs < 12000, 'did not wait the full 15 s');
  });
  // wait without equals returns when the variable appears.
  await stepAsync('wait_until_exists', async () => {
    const pending = httpAsync({ path: '/lists/vars/get', query: { key, name: 'late', wait: 15 } });
    await load.sleepAsync(1.5);   // lets the pending request go out
    http({ path: '/lists/vars/set', query: { key, name: 'late', value: 'here' } });
    const r = await pending; STATE.current = r;
    eq([r.json.exists, r.json.value], [true, 'here'], 'appeared'); ok(r.json.waitedMs < 12000, 'early return');
  });

  // Variables live beside the queue: lists/clear does not remove them.
  call('push_record', { path: '/lists/push', query: { key, a: '1' } });
  call('clear_records', { path: '/lists/clear', query: { key } }, (r, b) => eq(b.cleared, true, 'cleared'));
  get('orderId', null, 'survives_lists_clear', 200, (r, b) => eq(b.value, 'SO-1002', 'variable kept'));

  // The cycle's own control variables are readable by everyone.
  call('cycle_namespace', { path: '/lists/vars/get', query: { key: CONFIG.project, name: 'namespace' } },
    (r, b) => eq(b.value, STATE.ns, 'namespace unchanged during the run'));
});
