# perf — integration notes

Source: `v1/src/perf.py` (ported from `PerfApiFunction.py`). Uses `common.run` / `Req` /
`ApiError` / `respond` / `raw`. No helpers were missing from `common.py`.

## Lambda function

### PerfFunction — `perf.handler`

* Runtime python3.12, **MemorySize 512**, **Timeout 29** (as before: `add` is a handful of tiny
  DynamoDB writes, but `series` merges up to 20,000 interval items and `export` sorts up to
  50,001 points; 512 MB is sized for the read paths and costs almost nothing on writes).
* Invoked by API Gateway: `/perf` and `/perf/{proxy+}` (ANY), AWS_PROXY.
* Environment:

| Var | Value |
|---|---|
| `PERF_TABLE` | the perf table name (was `TABLE_NAME`) |
| `BASE_URL` | `https://api.<domain>/v1` (set on every function; not otherwise used here) |

* IAM (role for PerfFunction), all on the perf table ARN:
  `dynamodb:GetItem, BatchGetItem, PutItem, UpdateItem, DeleteItem, BatchWriteItem, Query`
  (unchanged from the old `PerfApiRole`), plus `AWSLambdaBasicExecutionRole`.
* No async self-invocation, no S3.

## Table — PerfTable

| | |
|---|---|
| Billing | `PAY_PER_REQUEST` |
| Partition key | `PK` (S) — `"<key>#<shard 0-15>"`, or `__KEYS__` for the key registry |
| Sort key | `SK` (S) — `AGG` \| `BKT#<epoch-minute, 13 digits>` \| `RAW#<epoch-ms, 15 digits>#<4 digits>` \| (registry) `<key>` |
| TTL | attribute `ttl`, enabled (interval buckets and raw points only; set to write time + 7 days) |
| GSIs | none |
| Retention | `DeletionPolicy: Retain` in the old stack — keep or drop as you prefer for v1 |

Storage layout is unchanged from the old service except that `BKT#` items now also carry
`mn`/`mx` (see below). Registry items (`PK=__KEYS__`) hold `t0`/`t1`.

## Behaviour changes vs the old API

* Routes `/v1/perf/{add,stats,series,assert,keys,clear,export}`; the old per-route resources are
  replaced by one proxy resource.
* Parameters: `ts` → `time`, `from`/`to` → `since`/`until`. The old names are **rejected with a
  400** naming the new one (otherwise `from=` would silently give lifetime figures and in
  `assert` `to=` would be mistaken for a check).
* **Key rule is now the common `KEY_RE`**: 1–128 characters `[A-Za-z0-9._:@+=-]`, starting with a
  letter or digit. Previously 1–120 characters and `/` was allowed. **Keys containing `/` are now
  a 400** — use `.` or `:` for hierarchy. (The old reserved-`__` check is subsumed: keys can no
  longer start with `_`.)
* All response fields camelCase: `Count→count`, `Errors→errors`, `ErrorRate→errorRate`,
  `Min/Max/Mean/StdDev/Sum→min/max/mean/stdDev/sum`, `P50…P99_9→p50…p99_9` (extras `p99_5` …),
  `FirstSeenUtc/LastSeenUtc→firstTime/lastTime`, `DurationSeconds→durationSeconds`,
  `Throughput→throughput`, `Key→key`, `Source→source`, `Truncated→truncated`,
  `Comment→comment`; series `Interval→intervalSeconds`, `Buckets→buckets`, `Series→series`,
  `StartUtc→startTime`; assert `Pass/Failed/Checks/Summary→pass/failed/checks/summary` and
  `Check/Statistic/Actual/Operator/Threshold→check/statistic/actual/operator/threshold`
  (`statistic` now holds the camelCase field name, e.g. `errorRate`, `p99_9`); keys
  `Key/FirstSeenUtc/LastSeenUtc→key/firstTime/lastTime`; export JSON
  `Key/Count/Truncated/Points/TimestampMs/Value/Ok→key/count/truncated/points/timestampMs/value/ok`;
  add `timestampUtc→time`. Stats in a window also echo `since`/`until`; assert echoes `source`.
* Errors use the common shape `{error, status, ...}` (was `{error}`), and unknown actions 404 with
  the valid `actions` list.
* Stricter parsing (convention: malformed = 400): `ok`/`raw`/`strict` must be booleans; `p` values
  must be numbers in 0–100 (were silently ignored); `since`/`until`/`time` must be numbers (were
  silently ignored) and ≤ 99999999999 (13-digit milliseconds are a 400 with a hint);
  `since > until` is a 400; assert percentiles > 100 are a 400; export `format` other than
  csv/json is a 400 (was silently CSV). `limit` is clamped 1–50000 as before.
* **Bug fix — min/max in windows and series.** Old code only wrote `mn`/`mx` on the `AGG` item,
  so `series` rows and windowed `stats` always had `Min`/`Max` = null (and percentiles lost their
  clamp to the true bounds). Bounds are now kept on `BKT#` items too.
* **Bug fix — stale container caches.** The old per-container min/max and registry caches were
  keyed by key only, so after `clear` a warm container could skip the bound write (wrong
  `min`/`max`) or never re-register the key (missing from `keys`). Now each accumulator update
  seeds `mn`/`mx` with `if_not_exists` and uses `ReturnValues=ALL_NEW` to see the stored bounds;
  the conditional bound write is issued only when the point is a new extreme. The registry is
  written whenever a shard aggregate is freshly created (`n == 1`) and otherwise refreshed at
  most once per 60 s per container, so `keys.lastTime` is now approximately current (it used to
  be frozen at the first write per container). Net DynamoDB cost per point is unchanged (2
  updates + an occasional conditional bound write).
* **Bug fix — backfill TTL and raw timestamps.** `ttl` is now write time + 7 days (was point
  time + 7 days, so backfilling a run older than a week expired immediately), and raw points are
  stored under the point's `time` in ms when `time=` is given (was wall-clock ms, so `export`
  windows ignored backfilled times).
* **Bug fix — export ordering/truncation.** Old export stopped reading further shards once the
  limit was hit (result biased to low shards, not the oldest points) and could miss truncation.
  Now each shard is read up to `limit+1`, merged oldest-first and trimmed, so it returns the
  oldest `limit` points and `truncated` is exact.
* CSV export: `Content-Type: text/csv; charset=utf-8`, `Content-Disposition: inline;
  filename="<key>.csv"`; values are printed as plain numbers (no `1E+3`).
* `keys` is hard-capped at 2000 entries with `truncated: true` (old could overshoot by a page).
* api-key handling removed.

## Open questions

* `AGG.t1` is last-writer-wins (`SET t1 = :now`), so with out-of-order backfilled `time=` values
  `lastTime`/`durationSeconds` can be understated. Unchanged from the old service; fixing it
  needs a conditional write per point.
* Should `keys` accept `limit`/`start` for paging past 2000 keys?
