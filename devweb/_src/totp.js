// ============================================================================
// totp - totp/get stateless and stored, checked against DevWeb's own
// load.utils.totp (RFC 6238, SHA-1, 6 digits, 30 s); digits/period/algorithm
// options; set/overwrite; users with + and @; validation errors.
// ============================================================================

const SECRET = 'JBSWY3DPEHPK3PXP';

load.initialize(async function () { startScript(); });

// The server's code must be DevWeb's code for this, the previous or the next
// 30-second step (allows for clock skew and a step boundary mid-request).
function matchesLocal(b) {
  const now = Date.now();
  const local = [0, -30000, 30000].map((d) => load.utils.totp(SECRET, now + d));
  ok(local.includes(b.code), `code ${b.code} is a valid RFC 6238 code (local ${local.join('/')})`);
}

function shape(b, digits, period, algorithm) {
  const re = new RegExp(`^\\d{${digits}}$`);
  ['code', 'nextCode', 'previousCode'].forEach((f) => ok(re.test(b[f]), `${f} has ${digits} digits`));
  ok(b.code !== b.nextCode || b.code !== b.previousCode, 'adjacent codes differ');
  eq([b.digits, b.period, b.algorithm], [digits, period, algorithm], 'settings');
  between(b.secondsRemaining, 0, period, 'secondsRemaining');
}

load.action('totp', async function () {
  call('service_index', { path: '/totp' }, (r, b) => ['get', 'set'].forEach((a) => ok(b.actions.includes(a), a)));

  // Stateless, default settings, against the local implementation.
  call('get_stateless', { path: '/totp/get', query: { secret: SECRET } }, (r, b) => { shape(b, 6, 30, 'sha1'); matchesLocal(b); });
  call('get_stateless_lowercase_secret', { path: '/totp/get', query: { secret: SECRET.toLowerCase() } }, (r, b) => matchesLocal(b));
  for (const [digits, period, algorithm] of [[8, 60, 'sha256'], [7, 45, 'sha512'], [6, 10, 'sha1']]) {
    call(`get_stateless_${algorithm}_${digits}d_${period}s`, { path: '/totp/get', query: { secret: SECRET, digits, period, algorithm } },
      (r, b) => shape(b, digits, period, algorithm));
  }

  // Stored: set, get, and the same code as the stateless form.
  const key = uid('totp');
  const user = `ann+${userId()}@example.com`;
  call('set', { path: '/totp/set', query: { key, user, secret: SECRET } }, (r, b) => {
    eq([b.key, b.user, b.saved, b.digits, b.period, b.algorithm], [key, user, true, 6, 30, 'sha1'], 'stored with defaults');
    isNum(b.savedTime, 'savedTime');
  });
  call('get_stored', { path: '/totp/get', query: { key, user } }, (r, b) => { eq([b.key, b.user], [key, user], 'echoed'); shape(b, 6, 30, 'sha1'); matchesLocal(b); });
  call('set_overwrite_options', { path: '/totp/set', query: { key, user, secret: SECRET, digits: 8, period: 60, algorithm: 'SHA-256' } },
    (r, b) => eq([b.digits, b.period, b.algorithm], [8, 60, 'sha256'], 'overwritten'));
  step('stored_equals_stateless', () => {
    for (let i = 0; i < 3; i++) {
      const s = http({ path: '/totp/get', query: { key, user } }).json;
      const t = http({ path: '/totp/get', query: { secret: SECRET, digits: 8, period: 60, algorithm: 'sha256' } }).json;
      if (s.code === t.code) { shape(s, 8, 60, 'sha256'); return; }   // else a step boundary fell between: retry
    }
    fail('stored and stateless codes never matched');
  });
  // The shared secret from the init script.
  call('get_shared', { path: '/totp/get', query: { key: POOLS.totp(STATE.ns), user: 'shared' } }, (r, b) => matchesLocal(b));

  // Errors.
  call('get_unknown_user_404', { path: '/totp/get', query: { key, user: 'nobody' }, expect: 404 }, (r) => isError(r, 404));
  call('set_bad_secret_400', { path: '/totp/set', query: { key, user, secret: 'not-base32!' }, expect: 400 }, (r) => isError(r, 400, 'base32'));
  call('set_bad_digits_400', { path: '/totp/set', query: { key, user, secret: SECRET, digits: 9 }, expect: 400 }, (r) => isError(r, 400, 'digits'));
  call('set_bad_period_400', { path: '/totp/set', query: { key, user, secret: SECRET, period: 5 }, expect: 400 }, (r) => isError(r, 400, 'period'));
  call('set_bad_algorithm_400', { path: '/totp/set', query: { key, user, secret: SECRET, algorithm: 'md5' }, expect: 400 }, (r) => isError(r, 400, 'algorithm'));
  call('set_missing_secret_400', { path: '/totp/set', query: { key, user }, expect: 400 }, (r) => isError(r, 400, 'secret'));
  call('get_no_secret_or_key_400', { path: '/totp/get', expect: 400 }, (r) => isError(r, 400));
});
