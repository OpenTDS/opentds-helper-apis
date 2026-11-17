// ============================================================================
// sms - mocksend (codes, templates, sender, rate limit), get (since, real,
// waiting), list, the refusals of send and mocksend, and sms/receive's device
// key. Optional (user arguments): smsHandset = a registered handset number
// (checks mocksend refuses it); with smsDeviceKey as well, sms/receive is
// exercised for real; smsRealSend=true also sends one real text per
// iteration to smsHandset (costs money; leave off by default).
// ============================================================================

const HANDSET = arg('smsHandset', '');
const REAL_SEND = String(arg('smsRealSend', 'false')) === 'true';

load.initialize(async function () { startScript(); });

const last9 = (n) => n.replace(/\D/g, '').slice(-9);

load.action('sms', async function () {
  call('service_index', { path: '/sms' }, (r, b) => ['get', 'list', 'send', 'mocksend', 'receive'].forEach((a) => ok(b.actions.includes(a), a)));
  const number = mockNumber();
  const before = nowSec() - 1;

  call('get_nothing_yet', { path: '/sms/get', query: { number } }, (r, b) => eq([b.found, b.body], [false, null], 'nothing yet'));

  // Template with {code}, a chosen sender, a random code.
  const sent = call('mocksend_template', { path: '/sms/mocksend', query: { number, body: 'Your code is {code}', from: 'RegApp' } }, (r, b) => {
    eq([b.sent, b.mocked, b.number, b.sender, b.codeInBody], [true, true, last9(number), 'RegApp', true], 'mocked text');
    ok(/^\d{6}$/.test(b.code), 'random 6-digit code'); eq(b.body, `Your code is ${b.code}`, 'code substituted'); isStr(b.getUrl, 'getUrl');
  }).json;
  call('get_latest', { path: '/sms/get', query: { number, since: before } }, (r, b) => {
    eq([b.found, b.mocked, b.sender, b.body], [true, true, 'RegApp', sent.body], 'the text'); isNum(b.receivedTime, 'receivedTime');
  });
  call('get_by_local_format', { path: '/sms/get', query: { number: '0' + last9(number) } }, (r, b) => eq(b.body, sent.body, 'same handset by last nine digits'));
  call('get_via_getUrl', { url: sent.getUrl }, (r, b) => eq(b.found, true, 'getUrl works'));
  call('get_real_only', { path: '/sms/get', query: { number, real: 'true' } }, (r, b) => eq(b.found, false, 'mocked texts excluded'));
  call('get_since_future', { path: '/sms/get', query: { number, since: nowSec() + 60 } }, (r, b) => eq(b.found, false, 'nothing after since'));

  // Rate limit, one per second per number (an atomic conditional write): of two
  // texts sent at the same moment exactly one is accepted, the other is a 429.
  sleep(1.2);
  await stepAsync('mocksend_too_fast_429', async () => {
    const pair = await Promise.all([1, 2].map(() => httpAsync({ path: '/sms/mocksend', query: { number, body: 'pair {code}' } })));
    const statuses = pair.map((r) => r.status).sort();
    STATE.current = pair.find((r) => r.status !== 200) || pair[0];
    eq(statuses, [200, 429], 'one accepted, one refused');
    const refused = pair.find((r) => r.status === 429).json;
    eq(refused.status, 429, 'error body'); isNum(refused.retryAfterSeconds, 'retryAfterSeconds');
  });
  sleep(1.2);

  // Waiting for a NEW text: start the wait, then send.
  await stepAsync('get_wait_for_new', async () => {
    const pending = httpAsync({ path: '/sms/get', query: { number, wait: 15 } });
    await load.sleepAsync(1.5);
    http({ path: '/sms/mocksend', query: { number, code: 'AB12CD', body: 'Login code {code}' } });
    const r = await pending; STATE.current = r;
    eq([r.json.found, r.json.body], [true, 'Login code AB12CD'], 'the new text, not the older one');
    between(r.json.waitedMs, 500, 14000, 'returned when it arrived');
  });
  call('get_wait_runs_out', { path: '/sms/get', query: { number, wait: 2 } }, (r, b) => {
    eq(b.found, false, 'no newer text'); between(b.waitedMs, 1500, 8000, 'waitedMs');
  });
  call('list_number', { path: '/sms/list', query: { number, seconds: 120 } }, (r, b) => {
    eq(b.count, 3, 'three texts'); eq(b.numbers, [last9(number)], 'numbers'); eq(b.messages[0].body, 'Login code AB12CD', 'newest first');
    ok(b.messages.every((m) => m.mocked === true), 'all mocked');
  });
  call('list_recent_all', { path: '/sms/list', query: { seconds: 60 } }, (r, b) => {
    ok(b.numbers.includes(last9(number)), 'our number among recent ones'); isNum(b.sinceTime, 'sinceTime');
  });
  call('list_real_only', { path: '/sms/list', query: { number, real: 'true' } }, (r, b) => eq(b.count, 0, 'no real texts'));

  // Refusals.
  call('send_unregistered_403', { path: '/sms/send', query: { number }, expect: 403 }, (r, b) => {
    isError(r, 403); isStr(b.hint, 'hint'); has(b.mockUrl, '/sms/mocksend', 'mockUrl');
  });
  call('mocksend_code_too_long_400', { path: '/sms/mocksend', query: { number: mockNumber(), code: 'ABCDEFGHIJKLM' }, expect: 400 }, (r) => isError(r, 400));
  call('get_bad_number_400', { path: '/sms/get', query: { number: 'abc' }, expect: 400 }, (r) => isError(r, 400, 'digits'));
  call('get_missing_number_400', { path: '/sms/get', expect: 400 }, (r) => isError(r, 400));
  call('receive_no_key_401', { method: 'POST', path: '/sms/receive', query: { number },
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender: 'x', time: Date.now(), body: 'x' }), expect: [401, 403] }, (r) => isError(r, r.status));
  call('receive_wrong_key_401', { method: 'POST', path: '/sms/receive', query: { number },
    headers: { 'Content-Type': 'application/json', 'X-Device-Key': 'not-the-key' }, body: JSON.stringify({ sender: 'x', time: Date.now(), body: 'x' }), expect: [401, 403] });

  if (HANDSET) {
    call('mocksend_registered_403', { path: '/sms/mocksend', query: { number: HANDSET }, expect: 403 }, (r, b) => { isError(r, 403); isStr(b.hint, 'hint'); });
    if (CONFIG.smsDeviceKey && userId() === 1) {
      const t = Date.now(), text = `Regression capture ${uid('cap')}`;
      call('receive_with_key', { method: 'POST', path: '/sms/receive', query: { number: HANDSET },
        headers: { 'Content-Type': 'application/json', 'X-Device-Key': CONFIG.smsDeviceKey },
        body: JSON.stringify({ sender: 'RegressionSender', time: t, body: text }) }, (r, b) => eq(b.saved, true, 'saved'));
      call('get_real_captured', { path: '/sms/get', query: { number: HANDSET, real: 'true', since: Math.floor(t / 1000) - 1 } },
        (r, b) => eq([b.found, b.mocked, b.body], [true, false, text], 'captured text'));
    }
    if (REAL_SEND && userId() === 1) {
      call('send_real', { path: '/sms/send', query: { number: HANDSET, code: 'REG123', body: 'Regression {code}' }, expect: [200, 429] }, (r, b) => {
        if (r.status === 200) eq([b.sent, b.mocked, b.code], [true, false, 'REG123'], 'sent');
      });
    }
  }
});
