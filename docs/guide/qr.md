## qr — generate QR codes from text

Use `qr` when a test needs a QR code: to feed a scanner or camera-based flow, to display a code on
screen for a device to read, to produce an enrolment code from a secret (e.g. an
`otpauth://` URI for an authenticator app), or to compare against a QR code the system under test
produced. One action, `get`, with three output formats. Nothing is stored.

`{BASE}` is `https://api.<domain>/v1`. Every action answers GET. Parameter names are
case-insensitive.

### Actions

```
{BASE}/qr/get?text=hello%20world                          # JSON: PNG as base64 + metadata (default)
{BASE}/qr/get?text=hello%20world&format=svg               # image/svg+xml - usable as <img src>
{BASE}/qr/get?text=hello%20world&format=html              # a page to look at / scan off a screen
{BASE}/qr/get?text64=b3RwYXV0aDovL3RvdHAvRXhhbXBsZTp1c2VyP3NlY3JldD1KQlNXWTNEUEVIUEszUFhQ&ecc=H   # base64 text, high error correction
{BASE}/qr/get?text=ABC123&scale=4&border=2&version=5      # smaller pixels, thinner margin, fixed min size
```

### Fields to branch on (format=json)

* `png` (base64 PNG bytes), `dataUri` (`data:image/png;base64,…`).
* `text` (decoded input, echoed), `bytes` (UTF-8 length), `ecc`, `version` (1–40),
  `modules` (symbol width = 4 × version + 17), `mask`, `scale`, `border`,
  `pixels` (image width = (modules + 2 × border) × scale), `pngBytes`.

### Recipes

**Get image bytes in a script:** call with `format=json`, base64-decode `png`, write it to a
`.png` file or upload it to the system under test.

**Embed in a web page / report:** `<img src="{BASE}/qr/get?text=...&format=svg">`, or use
`dataUri` from the JSON form.

**Text with special characters** (`+ & % #`, e.g. an `otpauth://` URI or a URL with a query
string): base64 the text and send it as `text64` (standard or URL-safe alphabet, padding
optional).

### Pitfalls

* Send `text` **or** `text64`, not both (400). Missing text is a 400.
* Maximum 2900 bytes of UTF-8 at `ecc=L`; much less at `ecc=H` (about 1270). Too long is a 400
  that says so — lower `ecc` or shorten the text.
* `ecc` is one of `L M Q H` (default `M`); anything else is a 400.
* `scale` (1–40), `border` (0–16) and `version` (1–40) are clamped, not rejected; non-numbers are
  a 400. A `border` below 4 can stop some scanners finding the code.
* `format` is `json`, `svg` or `html`; there is no binary PNG response — decode `png` from the
  JSON.
* With `format=html`, errors are also returned as an HTML page (status 400), not JSON.
