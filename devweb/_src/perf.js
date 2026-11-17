// ============================================================================
// perf - add (batches, failures, back-dated times, raw points), stats checked
// against values computed here (count, errors, min, max, mean, stdDev, sum and
// percentiles within the 2% histogram accuracy), time windows, series, assert
// (pass, fail, strict 412, unknown check), keys, export (csv/json), clear.
// ============================================================================

load.initialize(async function () { startScript(); });

function exactStats(values) {
  const v = [...values].sort((a, b) => a - b), n = v.length;
  const mean = v.reduce((s, x) => s + x, 0) / n;
  const sd = Math.sqrt(v.reduce((s, x) => s + x * x, 0) / n - mean * mean);
  const pct = (p) => v[Math.max(1, Math.ceil((p / 100) * n)) - 1];      // nearest rank
  return { n, min: v[0], max: v[n - 1], mean, sd, sum: v.reduce((s, x) => s + x, 0), pct };
}

function checkPct(b, field, exact) {
  isNum(b[field], field);
  ok(Math.abs(b[field] - exact) <= exact * 0.021 + 0.001, `${field} ${b[field]} within 2% of ${exact}`);
}

load.action('perf', async function () {
  call('service_index', { path: '/perf' }, (r, b) => ['add', 'stats', 'series', 'assert', 'keys', 'clear', 'export'].forEach((a) => ok(b.actions.includes(a), a)));
  const key = uid('tx');
  const now = nowSec();

  call('stats_no_data', { path: '/perf/stats', query: { key } }, (r, b) => eq(b.count, 0, 'count 0'));
  call('assert_no_data_fails', { path: '/perf/assert', query: { key, p90lt: 1000 } }, (r, b) => eq(b.pass, false, 'no data never passes'));

  // 60 successful samples in three batches, plus 4 failures.
  const good = [];
  for (let i = 0; i < 60; i++) good.push(50 + ((i * 37) % 950) + (i % 7) * 0.25);
  const tGood = nowSec();
  for (let i = 0; i < 3; i++) {
    const batch = good.slice(i * 20, i * 20 + 20);
    call('add_batch', { path: '/perf/add', query: { key, value: batch.join(','), raw: 'true' } }, (r, b) => {
      eq([b.key, b.recorded, b.ok, b.raw], [key, 20, true, true], 'batch recorded'); isNum(b.time, 'time');
    });
  }
  const bad = [3000, 3500, 4000, 4500];
  call('add_failures', { path: '/perf/add', query: { key, value: bad.join(','), ok: 'false', raw: 'true' } }, (r, b) => eq([b.recorded, b.ok], [4, false], 'failures'));
  // Two samples back-dated by 2 minutes (outside the window used below).
  call('add_backdated', { path: '/perf/add', query: { key, value: '10,20', time: now - 120 } }, (r, b) => eq(b.time, now - 120, 'time kept'));
  const all = [...good, ...bad, 10, 20];
  const x = exactStats(all);

  call('stats', { path: '/perf/stats', query: { key, p: '50,90,95,99.5' } }, (r, b) => {
    eq([b.key, b.count, b.errors, b.source], [key, all.length, 4, 'lifetime'], 'count/errors');
    near(b.errorRate, 4 / all.length, 0.000002, 'errorRate'); eq([b.min, b.max], [10, 4500], 'min/max exact');
    near(b.mean, x.mean, 0.001, 'mean'); near(b.stdDev, x.sd, 0.01, 'stdDev'); near(b.sum, x.sum, 0.01, 'sum');
    checkPct(b, 'p50', x.pct(50)); checkPct(b, 'p90', x.pct(90)); checkPct(b, 'p95', x.pct(95)); checkPct(b, 'p99_5', x.pct(99.5));
    ['p75', 'p99', 'p99_9'].forEach((f) => isNum(b[f], `${f} (standard)`));
    eq(b.firstTime, now - 120, 'firstTime = back-dated sample'); isNum(b.durationSeconds, 'durationSeconds');
  });
  // Window: only the recent 64 samples.
  // Windows read one-minute buckets, included when the minute STARTS inside the window.
  call('stats_window', { path: '/perf/stats', query: { key, since: tGood - (tGood % 60), until: nowSec() + 300 } }, (r, b) => {
    eq(b.count, 64, 'back-dated samples excluded'); eq(b.min, Math.min(...good), 'min in window');
  });
  call('series', { path: '/perf/series', query: { key, interval: 60, p: 95 } }, (r, b) => {
    eq(b.intervalSeconds, 60, 'interval'); ok(b.buckets >= 2, 'at least two buckets (back-dated and now)');
    eq(b.series.reduce((s, x2) => s + x2.count, 0), all.length, 'buckets add up');
    b.series.forEach((s) => { eq(s.startTime % 60, 0, 'bucket aligned'); isNum(s.p95, 'p95 per bucket'); });
  });

  // assert: pass, fail, strict, many checks at once.
  call('assert_pass', { path: '/perf/assert', query: { key, p90lt: 5000, errorratelt: 0.2, countge: all.length, meanle: 2000, maxle: 4500 } }, (r, b) => {
    eq([b.pass, b.failed, b.checks.length], [true, 0, 5], 'all checks pass');
    b.checks.forEach((c) => { isNum(c.actual, 'actual'); isStr(c.operator, 'operator'); eq(c.pass, true, c.check); });
  });
  call('assert_fail', { path: '/perf/assert', query: { key, p50lt: 1, errorrateeq: 0 } }, (r, b) => {
    eq([b.pass, b.failed], [false, 2], 'two checks fail, still HTTP 200'); isNum(b.summary.count, 'summary');
  });
  call('assert_strict_412', { path: '/perf/assert', query: { key, p50lt: 1, strict: 'true' }, expect: 412 }, (r, b) => eq(b.pass, false, 'same body, 412'));
  call('assert_percentile_decimal', { path: '/perf/assert', query: { key, 'p99.9le': 4500, p99_5gt: 0 } }, (r, b) => eq(b.pass, true, 'p99.9 and p99_5 checks'));
  call('assert_unknown_check_400', { path: '/perf/assert', query: { key, avglt: 100 }, expect: 400 }, (r, b) => { isError(r, 400); isStr(b.hint, 'hint'); });
  call('assert_no_checks_400', { path: '/perf/assert', query: { key }, expect: 400 }, (r) => isError(r, 400));

  // keys, export, validation, clear.
  call('keys_prefix', { path: '/perf/keys', query: { prefix: key } }, (r, b) => {
    eq([b.count, b.prefix, b.keys[0].key], [1, key, key], 'our key'); isNum(b.keys[0].lastTime, 'lastTime');
  });
  call('export_json', { path: '/perf/export', query: { key, format: 'json' } }, (r, b) => {
    eq(b.count, 64, 'only raw=true points'); eq(b.points.filter((p) => p.ok === false).length, 4, 'failures flagged');
    b.points.forEach((p) => isNum(p.timestampMs, 'timestampMs'));
  });
  call('export_json_limit', { path: '/perf/export', query: { key, format: 'json', limit: 5 } }, (r, b) => eq([b.count, b.truncated], [5, true], 'limited'));
  call('export_csv', { path: '/perf/export', query: { key, format: 'csv' } }, (r) => {
    eq(r.contentType, 'text/csv', 'CSV'); const lines = r.body.trim().split(/\r?\n/);
    eq(lines[0], 'timestampMs,value,ok', 'header'); eq(lines.length, 65, 'header + 64 rows');
  });
  call('add_bad_value_400', { path: '/perf/add', query: { key, value: 'fast' }, expect: 400 }, (r) => isError(r, 400));
  call('stats_bad_p_400', { path: '/perf/stats', query: { key, p: '101' }, expect: 400 }, (r) => isError(r, 400));
  call('clear', { path: '/perf/clear', query: { key } }, (r, b) => { eq(b.key, key, 'key'); ok(b.deleted > 0, 'deleted'); });
  call('stats_after_clear', { path: '/perf/stats', query: { key } }, (r, b) => eq(b.count, 0, 'cleared'));
});
