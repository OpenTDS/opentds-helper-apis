// ============================================================================
// lock - acquire/release on a private key (busy, release, re-acquire, stale
// ref, waiting for a release), errors, and real mutual exclusion between the
// Vusers on one shared lock (checked with an atomic counter).
// ============================================================================

load.initialize(async function () { startScript(); });

const acquire = (tx, key, wait, check) => call(tx, { path: '/lock/acquire', query: { key, wait } }, check);
const release = (tx, ref, check) => call(tx, { path: '/lock/release', query: { ref } }, check);

load.action('lock', async function () {
  call('service_index', { path: '/lock' }, (r, b) => ['acquire', 'release'].forEach((a) => ok(b.actions.includes(a), a)));
  const key = uid('lk');

  const a = acquire('acquire_free', key, 0, (r, b) => {
    eq([b.locked, b.key, b.maxSeconds], [true, key, 60], 'locked'); isStr(b.ref, 'ref'); isNum(b.expiresTime, 'expiresTime');
    has(b.releaseUrl, '/lock/release?ref=', 'releaseUrl');
  }).json;
  acquire('acquire_busy', key, 0, (r, b) => {
    eq([b.locked, b.ref], [false, null], 'busy is 200 locked:false'); between(b.freeInSeconds, 1, 60, 'freeInSeconds');
  });
  acquire('acquire_busy_wait_2', key, 2, (r, b) => { eq(b.locked, false, 'still busy'); between(b.waitedMs, 1500, 8000, 'waited about 2 s'); });
  release('release', a.ref, (r, b) => { eq([b.released, b.key, b.ref], [true, key, a.ref], 'released'); isNum(b.heldSeconds, 'heldSeconds'); });
  release('release_again', a.ref, (r, b) => { eq(b.released, false, 'already released'); isStr(b.reason, 'reason'); });
  const c = acquire('acquire_after_release', key, 0, (r, b) => { eq(b.locked, true, 'free again'); ok(b.ref !== a.ref, 'new ref'); }).json;
  release('release_old_ref_after_reacquire', a.ref, (r, b) => eq(b.released, false, 'an old ref cannot release the new holder'));
  acquire('still_held_by_new_ref', key, 0, (r, b) => eq(b.locked, false, 'still held'));

  // Waiting: a second request is handed the lock the moment it is released.
  await stepAsync('acquire_wait_for_release', async () => {
    const pending = httpAsync({ path: '/lock/acquire', query: { key, wait: 15 } });
    await load.sleepAsync(1.5);
    http({ path: '/lock/release', query: { ref: c.ref } });
    const r = await pending; STATE.current = r;
    eq(r.json.locked, true, 'got it after the release'); between(r.json.waitedMs, 500, 14000, 'waited for the release');
    http({ path: '/lock/release', query: { ref: r.json.ref } });
  });
  // Releasing via the releaseUrl it gave us.
  const d = acquire('acquire_for_url', key, 0).json;
  call('release_via_url', { url: d.releaseUrl }, (r, b) => eq(b.released, true, 'releaseUrl works'));

  // Errors.
  call('acquire_missing_key_400', { path: '/lock/acquire', expect: 400 }, (r) => isError(r, 400, 'key'));
  call('release_missing_ref_400', { path: '/lock/release', expect: 400 }, (r) => isError(r, 400, 'ref'));
  call('release_malformed_ref_400', { path: '/lock/release', query: { ref: 'nonsense' }, expect: 400 }, (r) => isError(r, 400));
  acquire('acquire_wait_clamped', uid('free'), 999, (r, b) => eq(b.locked, true, 'free lock, wait clamped not rejected'));

  // Mutual exclusion across every Vuser of this script: inside the lock the
  // shared "inside" counter must be exactly 1.
  const shared = `${STATE.ns}_lock_shared`, inside = { key: STATE.ns, name: 'lockInside' };
  for (let i = 0; i < 3; i++) {
    step('mutual_exclusion', () => {
      let got = null;
      for (let t = 0; t < 4 && !got; t++) {
        const r = http({ path: '/lock/acquire', query: { key: shared, wait: 20 } }); STATE.current = r;
        eq(r.status, 200, 'status');
        if (r.json.locked) got = r.json;
      }
      ok(got, 'acquired the shared lock within 80 s');
      try {
        const n = http({ path: '/lists/counters/add', query: { ...inside, increment: 1 } }).json.value;
        eq(n, 1, 'nobody else inside the lock');
        sleep(0.3);
      } finally {
        http({ path: '/lists/counters/add', query: { ...inside, increment: -1 } });
        http({ path: '/lock/release', query: { ref: got.ref } });
      }
    });
  }
});
