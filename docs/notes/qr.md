# qr — integration notes

Source: `v1/src/qr.py` (ported from `QrFunction.py`; the encoder — GF(256) Reed-Solomon, matrix
placement, masking, PNG and SVG writers — is carried over unchanged). Uses `common.run` / `Req`
/ `ApiError` / `respond` / `raw` / `base_url`. No helpers were missing from `common.py`.

## Lambda function

### QrFunction — `qr.handler`

* Runtime python3.12, **MemorySize 512**, **Timeout 29** (as before: generation is CPU-bound and
  single-threaded; a URL-sized code takes ~20 ms, a near-capacity version-40 symbol with all 8
  mask evaluations much longer, so keep the CPU share that 512 MB buys).
* Invoked by API Gateway: `/qr` and `/qr/{proxy+}` (ANY), AWS_PROXY.
* Environment: `BASE_URL` = `https://api.<domain>/v1` — used as the HTML page's form action and
  in the HTML error page's example link.
* IAM: `AWSLambdaBasicExecutionRole` only. No tables, buckets or other AWS resources.
* No binary media types needed: PNG only ever travels as base64 inside JSON or a `data:` URI;
  SVG and HTML are text.
* No async self-invocation.

## Behaviour changes vs the old API

* Three routes collapse to one: `/qr/get` → `qr/get` (`format=json`, the default),
  `/qr/svg` → `qr/get?format=svg`, `/qr/show` → `qr/get?format=html`. An unknown `format` is a
  JSON 400 with `allowed`.
* The HTML page's re-encode form now submits to `{BASE_URL}/qr/get` with a hidden
  `format=html` (plus `ecc`, `scale`, `border` as before) and carries **no api key**; footer text
  points at the v1 formats.
* JSON field names were already camelCase and are unchanged (`text, bytes, ecc, version, modules,
  mask, scale, border, pixels, pngBytes, png, dataUri`).
* Errors use the common shape `{error, status, ...}` (was `{error, hint}`), e.g. `allowed` on a
  bad `ecc`. With `format=html` errors are still an HTML page (status 400), now linking to a v1
  example URL.
* Stricter parsing: a non-numeric `scale`/`border`/`version` is now a 400 (was silently the
  default); out-of-range values are still clamped. `ecc` is trimmed and case-insensitive as
  before.
* `text64` decoding uses `common.b64decode` (standard or URL-safe, padding optional) — same
  accepted inputs as before.
* The page and responses no longer carry any api-key or agency text.

## Open questions

* None.
