# lock — integration notes

Source: `v1/src/lock.py` (ported from `LockFunction.py`). Uses `common.run` / `Req` / `ApiError`
/ `respond` / `base_url` / `KEY_RE`. No helpers were missing from `common.py`.

## Lambda function

### LockFunction — `lock.handler`

* Runtime python3.12, **MemorySize 128**, **Timeout 29** (as before: a waiting `acquire` can hold
  for up to 20 s, and it is almost entirely sleep plus one tiny conditional write per ~0.25 s
  poll, so the minimum memory tier keeps waiting cheap).
* Invoked by API Gateway: `/lock` and `/lock/{proxy+}` (ANY), AWS_PROXY.
* Environment:

| Var | Value |
|---|---|
| `LOCK_TABLE` | the lock table name (was `TABLE_NAME`) |
| `BASE_URL` | `https://api.<domain>/v1` — used to build `releaseUrl` |

* IAM (role for LockFunction): `dynamodb:UpdateItem, dynamodb:GetItem` on the lock table ARN
  (unchanged), plus `AWSLambdaBasicExecutionRole`.
* No async self-invocation, no S3. Consider a reserved-concurrency ceiling if you want to bound
  how many held `wait` requests can run at once.

## Table — LockTable

| | |
|---|---|
| Billing | `PAY_PER_REQUEST` |
| Partition key | `LockKey` (S) — the caller's key |
| Sort key | none |
| TTL | attribute `GcAt`, enabled — housekeeping only (claim time + 24 h); the 60 s lease is enforced by the `ExpiresAt` condition, never by TTL |
| GSIs | none (deliberately: release must be strongly consistent, which is why the ref carries its key) |
| Other attributes | `LockId` (S, the current ref), `ExpiresAt` (N, epoch s; 0 = free), `ClaimedAt` (N) |
| Retention | not Retain (ephemeral coordination state), as before |

Storage is unchanged from the old service (the stored `LockId` value is simply the v1 `ref`).

## Behaviour changes vs the old API

* `/lock/lock` → `/v1/lock/acquire`; `/lock/release?lockid=` → `/v1/lock/release?ref=`. The
  old `lockid` parameter is not accepted (400 "missing ref").
* Response fields: `lockid` → `ref`; `expiresUtc` → `expiresTime`; busy response `message` →
  `comment`; the acquire `comment` now points at the v1 release URL and a new field
  `releaseUrl` carries it ready-made (URL-encoded). `maxSeconds`, `waitedMs`, `freeInSeconds`,
  `released`, `heldSeconds`, `reason` unchanged.
* **Key rule is now the common `KEY_RE`** (1–128 chars `[A-Za-z0-9._:@+=-]`, first char a letter,
  digit or `+`). Newly allowed: `:`, `@`, `=`. A leading `+` (phone-number keys) is still allowed. Because keys may now contain
  `:`, the ref is split on its **last** `:` (the random suffix is hex), so refs remain
  self-describing and release still goes straight to the partition.
* Errors use the common shape `{error, status, ...}`; `received` instead of `key`/`lockid` for
  the offending value. Unknown action → 404 with `actions`.
* `wait` that is not a number is still a 400; out-of-range is clamped 0–20 as before.
* Unchanged: 60 s maximum hold, no renew, busy = 200 `locked:false`, release by non-holder = 200
  `released:false` with a reason, poll interval 0.20–0.35 s with jitter.
* Device-specific wording in comments and hints replaced by generic "shared resource" wording; api-key handling
  removed.

## Open questions

* (Resolved) `common.KEY_RE` now allows a leading `+`, matching its help text.
