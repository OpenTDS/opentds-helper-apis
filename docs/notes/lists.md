# lists — notes for the template author

Service: `lists` (Node.js 22, AWS SDK v3 from the runtime, no other packages).
Sources merged: `ListFunction.js`, `ListVarFunction.js`,
`DataRegisterFunction.js`, `DataImportFunction.js`.

## Code layout (all flat in `src/`)

| File | What |
|---|---|
| `lists.js` | API entry `exports.handler`; queue actions push/pop/random/list/reset/clear/codes; wires the route tables of the modules below into `common.run`. |
| `lists_vars.js` | `vars/set`, `vars/get`, `vars/list`. |
| `lists_register.js` | `counters/add`, `counters/get`, `cleanup/push`, `cleanup/pop`, `cleanup/list`. |
| `lists_import.js` | `upload`, `upload/url`, `import`, `import/status`, the hand-signed presigned POST, CSV/JSON parsers, and `exports.worker` (background `import` and `cleanup` jobs). |
| `lists_page.js` | The HTML upload page (one page, CSV/JSON switchable). |
| `lists_store.js` | Shared DynamoDB/S3/Lambda clients, table-name lookup, shard-key layout, record presentation, `invokeWorker`. |

No helper was missing from `common.js`. Two spots read `req.qs` / the raw
`event.queryStringParameters` directly on purpose: `vars/set` and
`cleanup/push` must accept an empty `value` (`Req.text` treats empty as
missing), and `push` keeps the original case of field names.

## Lambda functions

Both use the shared zip. The API function invokes the worker asynchronously
(`InvocationType: Event`) by name from `WORKER_FUNCTION`; the worker never
invokes anything.

| | API | Worker |
|---|---|---|
| Handler | `lists.handler` | `lists_import.worker` |
| Timeout | 29 s | 900 s |
| Memory | 512 MB suggested (request bodies up to 4 MB, list responses up to ~4 MB) | 1769 MB (one full vCPU; parses up to 50 MB files in memory) |
| Triggered by | API Gateway `/lists`, `/lists/{proxy+}` ANY, AWS_PROXY | async invoke from the API: `{job:"import", key, ref, format, options, base}` from `lists/import`; `{job:"cleanup", key, gen, shards}` from `lists/clear` |
| Async config | – | `MaximumRetryAttempts: 0` recommended (the job records its own failure in the status file and a retry of a started job is a no-op anyway) |

### Environment variables

| Variable | API | Worker | Value |
|---|---|---|---|
| `LISTS_TABLE` | yes | yes | records table name |
| `VARS_TABLE` | yes | – | variables table name |
| `COUNTERS_TABLE` | yes | – | counters table name |
| `CLEANUP_TABLE` | yes | – | cleanup-register table name |
| `IMPORT_BUCKET` | yes | yes | import bucket name |
| `WORKER_FUNCTION` | yes | – | worker function name (or ARN) |
| `BASE_URL` | yes | (optional) | `https://api.<domain>/v1` — the API passes it to the worker in the payload for the `next` URLs |
| `CLEANUP_GRACE_MS` | – | optional | delay before deleting a replaced/cleared generation (default 5000; lets in-flight pops land). Leave unset in production. |
| `LOCAL_WORKER` | tests only | – | `1` runs worker jobs in-process instead of invoking the worker. Never set in AWS. |

`AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN` come from the Lambda runtime; the API function uses them
to sign the presigned POST (see IAM).

## IAM

API function role:

| Resource | Actions | Why |
|---|---|---|
| records table | `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query`, `BatchWriteItem` | queue actions; `clear` batch-deletes |
| vars table | `dynamodb:GetItem`, `PutItem`, `Query` | |
| counters table | `dynamodb:GetItem`, `UpdateItem` | |
| cleanup table | `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query` | |
| `arn:aws:s3:::<bucket>/imports/*` | `s3:PutObject`, `s3:GetObject` | body imports and status files; **PutObject is also what makes the presigned POST valid**, because it is signed with this role's credentials |
| `arn:aws:s3:::<bucket>` | `s3:ListBucket` (optionally with condition `s3:prefix` = `imports/*`) | `import/status` without `ref` lists uploads; also makes a HEAD/GET of a missing object return 404 instead of 403, which the code relies on |
| worker function | `lambda:InvokeFunction` | |

Worker role:

| Resource | Actions |
|---|---|
| records table | `dynamodb:PutItem`, `Query`, `BatchWriteItem` |
| `arn:aws:s3:::<bucket>/imports/*` | `s3:GetObject`, `s3:PutObject` |
| `arn:aws:s3:::<bucket>` | `s3:ListBucket` (404-vs-403 on the status file) |

Plus the usual `AWSLambdaBasicExecutionRole` logs for both.

## DynamoDB tables (all `PAY_PER_REQUEST`, no GSIs, no TTL)

| Table | Partition key | Sort key | Notes |
|---|---|---|---|
| records (`LISTS_TABLE`) | `key` (S) | `index` (N) | Row `index = 0` is a key's metadata (`count`, `nextPop`, and for imported keys `gen`, `shards`, `importRef`, `importFormat`, `importTime`). Data rows: `{key, index, data: {...user fields}, createdTime}`. Pushed rows live under the plain key; imported rows under `KEY#<gen>#<index % shards>` (1–16 shards, 12,500 records per shard) so bulk writes spread over partitions. On-demand mode is important: a 100K-record import writes ~100K items in well under a minute. |
| vars (`VARS_TABLE`) | `key` (S) | `name` (S) | `{value, savedTime}` |
| counters (`COUNTERS_TABLE`) | `key` (S) | `name` (S) | `{value (N), updatedTime}` |
| cleanup (`CLEANUP_TABLE`) | `key` (S) | `index` (N) | Row 0 = `{count, nextClean}`; rows 1..n = `{value, note?, savedTime, removedTime?}` |

Nothing expires. If data retention matters, see open questions.

## S3 bucket (`IMPORT_BUCKET`)

* Layout: `imports/<key>/<ref>.csv|json` (the uploaded or POSTed file) and
  `imports/<key>/<ref>.status.json` (import progress/result, written by API and
  worker). `ref` looks like `20260922T031500Z-k3x9qa`.
* **Lifecycle:** expire objects under `imports/` after **90 days** (the docs
  promise files are kept 90 days for reload). Also abort incomplete multipart
  uploads after 1 day (harmless; POST uploads are single-part).
* **CORS** (the upload page POSTs from the API's origin straight to the
  regional bucket endpoint):
  `AllowedOrigins: ["*"]` (or `https://api.<domain>`), `AllowedMethods: ["POST"]`,
  `AllowedHeaders: ["*"]`, `ExposeHeaders: ["ETag", "Location"]`, `MaxAgeSeconds: 3000`.
* Block all public access (presigned POSTs still work). Default SSE-S3 is fine.
* No S3 event notifications — the import is started by the API call, not by
  the upload.
* The presigned POST targets `https://<bucket>.s3.<region>.amazonaws.com/`
  (regional virtual-hosted, avoids the 307 a browser won't follow), so the
  bucket name must be DNS-compatible (no dots).
* Upload size caps: 50 MB via presigned POST (enforced by the policy's
  `content-length-range`), 4 MB via POST body.

## API Gateway

* For byte-exact CSV bodies (Windows-1252, UTF-16 files), the REST API's
  `BinaryMediaTypes` should include `text/csv`, `application/json` and
  `application/octet-stream` (or simply `*/*`), so bodies arrive base64. The handler accepts either encoding.
  This is an API-wide setting; check it with the other services (mail POSTs).
* 29 s integration timeout: `import` waits at most 25 s.

## Tests

`tests/node/lists.test.js` (node:test) starts its own `moto_server` on a free
port (or uses `AWS_ENDPOINT_URL` if set), creates the four tables and the
bucket, and drives `lists.handler` with API Gateway proxy events. Worker jobs
run in-process via `LOCAL_WORKER=1`; `CLEANUP_GRACE_MS=0`.

```
cd v1/tests/node && npm install && node --test     # ~30 s
pytest v1/tests/test_lists.py                       # same suite via pytest
```

`tests/node/package.json` holds the test-only `@aws-sdk/*` packages; the test
points `NODE_PATH` at that `node_modules` so `src/` resolves them.
Environment note: the installed `moto_server` failed to start until
`flask-cors` was installed (`pip install flask-cors`) — moto[server]
dependency missing from the image.

Covered: routing/index/404/OPTIONS, key validation, push (field case kept,
reserved names rejected), pop/exhausted/reset, random, list paging, clear
(plain and sharded with background delete), codes, vars set/get/list/404,
counters, cleanup push/pop/list/filter, CSV import by POST with wait, JSON
import by base64 POST (big ints, non-objects, reserved renames), wait=0 +
import/status polling, failed imports (422, key unchanged), import parameter
errors, upload/url shape and policy, GET import by ref + reload, multi-shard
import (12,600 records, 2 shards) with ordered list across shards, upload page.

## Behaviour changes vs the old `/data/*` API

* Routes renamed per ENDPOINTS.md (`/data/new` → `lists/push`, `/data/get` →
  `lists/random`, `/data/random` → `lists/codes`, `/data/importcsv` +
  `importjson` → `lists/import?format=`, `/data/uploadcsv` + `uploadjson` →
  `lists/upload?format=`, etc.). No api key anywhere.
* Key rule is the v1 universal rule (adds `. : @ + =`, max 128) instead of
  `[A-Za-z0-9_-]+`.
* **Record shape unified.** Pushed and imported records are both returned as
  `{...user fields, key, index, createdTime}`. Imported records are no longer
  under `record.data` (JSONPath `$.record.email`, not `$.record.data.email`).
  Storage now always keeps user fields in a `data` map attribute (a fresh
  deployment has no old rows), so user fields can never collide with storage
  attributes.
* `CreateTime` → `createdTime`; `SavedTime` → `savedTime`; `UpdateTime` →
  `updatedTime`; `RemovedTime` → `removedTime`; import-status ISO strings
  (`queued`, `started`, `finished`, `updated`) → epoch seconds `queuedTime`,
  `startedTime`, `finishedTime`, `updatedTime`; `maxKB` → `maxKb`; `expiresIn`
  → `expiresInSeconds` (+ `expiresTime`).
* `push`: field names keep their original case; parameters named `index` or
  `createdTime` (any case) are rejected with 400 (old code silently
  overwrote them). Response is `{key, index, createdTime, count, record}`
  (old: `{key, index, CreateTime, stored}`).
* Import renames a CSV column or top-level JSON property named `key`, `index`
  or `createdTime` to `key_2` etc. with a warning.
* `pop` adds `exhausted: false`, `count`, `remaining` on success; exhausted
  response has `index: null, record: null`.
* `random` on an empty key: `{empty: true, count: 0, record: null}` (was
  `{empty: true}` with no record field); success adds `empty: false`, `count`.
* `list` no longer returns `variables` (use `vars/list`); always returns
  `truncated`, `nextStart`, `importRef` (null when not imported) and `start`.
* `reset` returns `reset: true|false`, `count`, `nextPop`.
* `clear` returns `cleared`, `deleted` (data rows removed now, excluding the
  metadata row), `backgroundDelete` (0 when none).
* `vars/set` no longer returns `result: "saved"`; returns `savedTime`. Empty
  value allowed (as before). `vars/get` 404 uses the standard error shape.
* `counters/*`: `UpdateTime` → `updatedTime`; unused counter also returns
  `updatedTime: null`.
* `cleanup/pop` now returns `note`; exhausted response is `{exhausted: true,
  index: null, value: null}`. `cleanup/list`: `nextClean` → `nextPop`, the
  echoed filter is `filter` (not `status`, which is the numeric HTTP status in
  error bodies); records carry `note: null` when absent.
* **Import state field is `state`** (was `status`), because `status` is the
  numeric HTTP status in the v1 error shape. HTTP codes unchanged: done 200,
  queued/running 202, failed 422 — the 422 body is the standard error shape
  (`error`, `status: 422`) plus the full import state. 409 when a ref is
  already importing; 413 when the body is over 4096 KB.
* `wait` is now seconds (0–25, default 25, clamped); `true/yes/on` = 25,
  `false/no/off` = 0; a bare number is always seconds (`wait=1` is 1 s).
* `format` is required on `import` and `upload/url`; `delimiter` is ignored
  for JSON.
* `import/status` for a ref that was uploaded but not imported returns
  `state: "uploaded"` with `format`, `fileBytes` and a `next` URL; an unknown
  ref is 404 (old: 200 with `status: uploaded` and no format).
* The `next` URLs in an import result are absolute v1 URLs (`pop`, `random`,
  `reset`, `list`, `reload`); the old `field` hint is gone (the `sample` is
  now record 1 exactly as `pop` returns it).
* Background jobs run on a separate worker function (`lists_import.worker`)
  instead of the import function invoking itself; payload key is `job`
  (was `worker`).
* Upload page: one page for both formats with a CSV/JSON switch (a dropped
  `.json`/`.csv` file also switches it), v1 URLs, no api-key handling, no
  domain-specific sample data.

## Open questions

1. `clear` deletes records only (as before). CONVENTIONS says `clear` =
   "delete everything under a namespace" — should it also clear the key's
   variables, counters and cleanup register? Kept as-is because wiping the
   cleanup register on a data reload would lose teardown information.
2. No TTL on any table and no retention on records. For a public site,
   consider a TTL attribute (e.g. 30–90 days since last write) on all four
   tables; the code would need to stamp it.
3. OpenAPI: two operations on `/lists/import`, so the POST is
   `listsImportPost` (GET is `listsImport`).
4. `wait` also accepts booleans for backward compatibility; CONVENTIONS says
   one meaning per parameter — drop the boolean form if strictness is
   preferred.
5. Public, unauthenticated bulk import (50 MB files, 1M records) is an abuse
   vector; consider API Gateway throttling / usage plans on `lists/import` and
   `lists/upload/url`.
