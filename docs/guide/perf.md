## perf — record performance results, read statistics, gate a run

Use `perf` when a test script (LoadRunner, k6, JMeter, Gatling, newman, a CI job) should record
response times or any other non-negative measurement centrally and later ask "what were p90 and
the error rate?", "did it degrade as load ramped?" or "did this run pass its thresholds?". Many
virtual users can write to the same key at once; nothing is lost and nothing needs setting up
first.

* A **key** is a series name you invent (`Login`, `checkout.submit`, `run42:Search`). 1–128
  characters: letters, digits and `. _ - : @ + =`, starting with a letter, digit or +. No `/`.
* count, errors, mean, stdDev, sum, min and max are **exact**; percentiles come from a
  histogram and are within about **2 % relative**.
* Lifetime figures never expire. Per-minute interval buckets (used by `series` and by
  `since`/`until` windows) and raw points (`raw=true`) are kept **7 days**.

`{BASE}` is `https://api.<domain>/v1`. Every action answers GET. Parameter names are
case-insensitive. Times are UTC epoch **seconds**.

### Actions

```
{BASE}/perf/add?key=Login&value=248.5                          # record one point (ok=true, time=now)
{BASE}/perf/add?key=Login&value=248.5,301,199.2                # up to 100 points in one call
{BASE}/perf/add?key=Login&value=5012&ok=false                  # record a failed transaction
{BASE}/perf/add?key=Login&value=248.5&time=1785400020&raw=true # backfill at a time; keep the raw point
{BASE}/perf/stats?key=Login                                    # lifetime summary
{BASE}/perf/stats?key=Login&p=99.5,99.99&since=1785400020&until=1785403620   # windowed + extra percentiles
{BASE}/perf/series?key=Login&interval=300&p=90,99              # 5-minute buckets, oldest first
{BASE}/perf/assert?key=Login&p90lt=800&errorratelt=0.01&countge=1000          # pass/fail gate (200)
{BASE}/perf/assert?key=Login&p90lt=800&errorratelt=0.01&strict=true          # 412 when it fails
{BASE}/perf/keys?prefix=run42:                                 # which keys exist
{BASE}/perf/export?key=Login&format=csv&limit=10000            # raw points (only those added with raw=true)
{BASE}/perf/clear?key=Login                                    # delete everything for the key
```

### Fields to branch on

* `add` → `{key, recorded, time, ok, raw}`. `recorded` = number of points stored.
* `stats` → `count` (0 = no data, not an error), `errors`, `errorRate` (0–1), `min`, `max`,
  `mean`, `stdDev`, `sum`, `p50 p75 p90 p95 p99 p99_9` (+ any `p=` extras, e.g. `p99_5`),
  `firstTime`, `lastTime`, `durationSeconds`, `throughput` (points/s; null when all points share
  one second), `source` (`lifetime` | `window`), `truncated`.
* `series` → `intervalSeconds`, `buckets`, `series[]` of `{startTime, count, errors, errorRate,
  min, max, mean, throughput, p90…}`.
* `assert` → **`pass`** (boolean), `failed` (number of failing checks), `checks[]` of
  `{check, statistic, actual, operator, threshold, pass}`, `summary` (the stats object).
  HTTP 200 unless `strict=true` **and** a check failed → HTTP 412 with the same body.
* `clear` → `more: true` means call it again (≈5000 items deleted per call).
* `export` → CSV `timestampMs,value,ok`, or with `format=json`
  `{key, count, truncated, points[{timestampMs, value, ok}]}`.

### Assert check syntax

Each check is one query parameter `<statistic><operator>=<number>`:

* statistics: `count errors errorrate mean min max stddev sum throughput`, or a percentile
  `p50`, `p90`, `p99`, `p99.9` (or `p99_9`), any `p<0–100>`;
* operators: `lt` `<`, `le` `<=`, `gt` `>`, `ge` `>=`, `eq` `==`.

Example: `p95le=1200&errorratelt=0.02&countgt=500&throughputge=10`. All checks must pass.

### Recipes

**Record from every iteration, gate at the end (CI):**

```
# before the run
{BASE}/perf/clear?key=run42:Login            (repeat while more=true)
# in each virtual user, after each transaction (value in ms; ok=false on failure)
{BASE}/perf/add?key=run42:Login&value={elapsedMs}&ok={passed}
# after the run - fail the pipeline step on HTTP 412
{BASE}/perf/assert?key=run42:Login&p90lt=800&errorratelt=0.01&countge=1000&strict=true
```

**Cheaper recording at volume:** buffer timings in the script and send up to 100 at once:
`{BASE}/perf/add?key=Search&value=231,244.7,198,305`.

**Degradation check:** `{BASE}/perf/series?key=Login&interval=60&p=95` and compare the `p95`
of the first and last buckets.

### Pitfalls

* `ok`, `time`, `raw` apply to **every** value in a comma-separated batch — send failures in a
  separate call with `ok=false`.
* Values must be ≥ 0 and ≤ 1e12; negative, `NaN`, or non-numeric values are a 400 (the whole
  call is rejected, nothing recorded).
* `time`, `since`, `until` are epoch **seconds**; a 13-digit millisecond value is a 400.
  The pre-v1 names `ts`, `from`, `to` are rejected with a 400 that names the new parameter.
* A key with **no data fails** `assert` (it never passes by accident); `stats` returns
  `count: 0`.
* `assert` without `strict=true` is HTTP 200 even when the gate fails — branch on `pass`.
* Windows (`since`/`until`) are whole minutes: a minute bucket is included when its start lies
  in the window. They only reach back 7 days; lifetime figures (no window) never expire.
* `export` returns only points recorded with `raw=true`.
* `keys` `lastTime` is approximate (refreshed about once a minute).
* Pick one unit per key (ms is usual); the service does not know or convert units.
