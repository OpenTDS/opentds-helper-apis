# names — integration notes

Source: `v1/src/names.py` (handler and logic) and `v1/src/names_data.py` (the curated `REGIONS`
lists, copied verbatim from `NameFunction.py`), ported from `NameFunction.py` (old `/name`).
Uses `common.run` / `Req` / `ApiError` / `respond`. No helpers were missing from `common.py`.

## Lambda function

### NamesFunction — `names.handler`

* Runtime python3.12, **MemorySize 256**, **Timeout 10** (as before; the lists are split once
  per container and cached, so a warm call is a few dict lookups and `random.choice` calls).
* Invoked by API Gateway: `/names` and `/names/{proxy+}` (ANY), AWS_PROXY.
* Environment: `BASE_URL` only (set on every function; not used here).
* IAM: `AWSLambdaBasicExecutionRole` only. No tables, buckets or other AWS resources.
* No async self-invocation.

## Behaviour changes vs the old API

* Service renamed `/name` → `/v1/names`; routes `names/get`, `names/regions`.
* **Seeded output is identical** to the old service for every seed (same data, same
  SHA-256-hashed `random.Random` stream, same draw order, raw unstripped seed value) — verified
  by comparing old and new outputs across regions/seeds; `tests/test_names.py` pins golden
  values.
* Response fields were already camelCase and are unchanged.
* Error bodies use the common shape `{error, status, ...}`; the list of valid codes is now
  `allowed` (was `known`) and includes `ANY` where it applies. Status codes unchanged: both
  params / neither / everything excluded → 400; unknown region (also inside `notregion`) → 404.
* `regions` comment now points at `names/get`.
* The lists are split once per container instead of on every call (no output change).
* Removed the old description's reference to an external identity-data site; api-key handling
  removed.

## Open questions

* None.
