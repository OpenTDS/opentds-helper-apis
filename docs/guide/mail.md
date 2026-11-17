## mail — catch inbound email, send email, store and search attachments

Use `mail` when a test has to prove that the system under test sent an email (sign-up
confirmation, password reset, one-time code, invoice) or has to send one into it
(email-to-case, inbound document processing).

* **Catching:** every message sent to `<anything>@<mail domain>` is captured and queued per
  recipient address. Invent a unique address per test run (`signup+run42@<mail domain>`), point the
  system under test at it, then `pop` it.
* **Sending:** `send` delivers real mail through SES. `from` must be at the mail domain; `to` must
  be at the mail domain or on the deployment's allow-list.
* **Attachments:** received attachments get a short `ref` (kept 7 days). Files you want to keep
  or send go in the saved store under a `key` you choose. `attachments/find` searches inside PDF,
  Word/Excel/PowerPoint and text files without downloading them (password-protected PDFs too),
  and `attachments/text` shows exactly the text it searches.

`{BASE}` is `https://api.<domain>/v1`. Every action answers GET. Parameter names are
case-insensitive; values containing `+ & % #` go in the `...64` (base64) form.

### Actions

```
{BASE}/mail/pop?address=signup+run42@<mail domain>&wait=15     # next unread message; wait up to 15 s for one
{BASE}/mail/reset?address=signup+run42@<mail domain>           # rewind to the first message (messages kept)
{BASE}/mail/clear?address=signup+run42@<mail domain>           # delete all messages for the address
{BASE}/mail/send?from=test@<mail domain>&to=inbox@<mail domain>&subject=Hi&body=Hello        # plain send
{BASE}/mail/send?from=test@<mail domain>&to=inbox@<mail domain>&subject=Hi&body=See+file&attachmentkb=64   # + generated 64 KB text file
{BASE}/mail/send?from=...&to=...&subject=...&body=...&key=invoice_template.pdf   # + a saved attachment
{BASE}/mail/send?from=...&to=...&subject=...&body=...&ref=rb0q4ipfcu            # + a received attachment (forward it)
POST {BASE}/mail/send?from=...&to=...&subject=...        (body = message text)
POST {BASE}/mail/attachments/save?key=invoice_template&filename=invoice.pdf   (body = raw bytes with Content-Type: application/octet-stream, or base64 text; max 4096 KB)
{BASE}/mail/attachments/save?key=kept_invoice.pdf&ref=rb0q4ipfcu     # keep a received attachment
{BASE}/mail/attachments/get?ref=rb0q4ipfcu            # base64 content of a received attachment
{BASE}/mail/attachments/get?key=invoice_template.pdf&url=true   # presigned download URL instead of base64
{BASE}/mail/attachments/get?ref=rb0q4ipfcu&view=true             # browser: redirect to the file, PDF opens in a tab
{BASE}/mail/attachments/get?ref=rb0q4ipfcu&download=true         # browser: redirect to the file, saved
{BASE}/mail/attachments/get?ref=rb0q4ipfcu&view=true&password=secret   # protected PDF, served unlocked
{BASE}/mail/attachments/delete?key=invoice_template.pdf
{BASE}/mail/attachments/find?ref=rb0q4ipfcu&text=Invoice%20number%20INV-261002-1TFJP   # literal (spaces match any gap)
{BASE}/mail/attachments/find?ref=rb0q4ipfcu&regex64=SU5WLShcZCsp     # regex (base64 of INV-(\d+))
{BASE}/mail/attachments/find?ref=rb0q4ipfcu&left=Total%20including%20GST   # capture the value in the next cell
{BASE}/mail/attachments/find?ref=rb0q4ipfcu&left=Reference:&right=Due     # capture between two markers
{BASE}/mail/attachments/find?ref=rb0q4ipfcu&in=metadata&left=InvoiceNumber   # search the PDF document properties
{BASE}/mail/attachments/find?ref=rb0q4ipfcu&text=Visa%20ending%201111&password=secret   # protected PDF
{BASE}/mail/attachments/text?ref=rb0q4ipfcu&format=text              # the searchable text, as plain text
{BASE}/mail/attachments/compare?ref=rb0q4ipfcu&otherkey=from_app     # same document? (same, sameBytes, sameText, differences)
{BASE}/mail/attachments/text?ref=rb0q4ipfcu&page=2&password64=c2VjcmV0   # one page of a protected PDF (JSON)
{BASE}/mail/attachments/upload/url?key=big.pdf&size=15728640   # presigned S3 POST for files up to 100 MB
{BASE}/mail/attachments/upload                                   # drag-and-drop page for humans
```

Remember to URL-encode the `+` in an address as `%2B` (`signup%2Brun42@...`) when you build the URL
by hand; HTTP libraries that take a params dict do this for you.

### Response fields to branch on

`pop`:

```json
{"address": "signup+run42@mail.example.com", "exhausted": false, "repeat": false,
 "serial": 4, "remaining": 0, "waitedMs": 2310, "receivedTime": 1785400000, "ageSeconds": 1,
 "subject": "Welcome", "from": "Example App <no-reply@example.com>",
 "body": "Your verification code is 482913",
 "attachments": [{"filename": "invoice.pdf", "ref": "rb0q4ipfcu", "bytes": 90941}]}
```

* `exhausted` — `true` means nothing was returned (`body` is `null`). Still HTTP 200.
* `pending` — present and `true` when a message is arriving but not yet readable; call again.
* `repeat` — `true` when this is a re-offer of an already-popped message inside the `grace`
  window. Ignore repeats if you count messages.
* `body` — plain-text part (HTML part if there is no plain part). Extract codes/links with a regex.
  `bodyTruncated: true` means the body was over 300 KB and was clipped.
* `attachments[].ref` — handle for `attachments/get|find|save` and `send?ref=`.

`send`: `result` = `"sent"`, `messageId`, `sentTime` (comparable with `receivedTime`),
`attachments` `[{filename, bytes}]`, plus `warning` (JSON sent without the JSON content type) or
`deliverabilityWarning` / `refusedBy` (message over 20 MB).

`attachments/get`: `data` (base64) **or** `url` (presigned, `expiresInSeconds`), `filename`,
`bytes`, `source` (`saved` / `received`).

`attachments/find`: `count` (true total), `results[]` `{offset, page, matched, context,
contextEscaped, value}`, `values` (regex/boundary captures, flat list), `emptyMatches` (captures
that came out empty, not counted), `truncated`, `document` (`characters`, `pages`, `encrypted`,
`encryption`, `passwordUsed`); with `count` 0 also `caseInsensitiveCount` and a `hint`.

`attachments/text`: `pages[]` `{page, text}`, `metadata` (PDF document properties), `document`,
`truncated`. `format=text` returns `text/plain`.

**How PDF text is laid out.** Each visual line of the page is one line of text; a wide gap
(a table column, a label and its value) becomes a **tab**, a normal word gap one space. The
sample invoice reads:

```
Verdant Dock Pty Ltd (fictional)	Invoice number	INV-261002-1TFJP
Qty	Description	Unit price	Amount
1	AutoPlanter Small (VD-AP-S)	$1,490.00	$1,490.00
Total including GST	$2,972.00
```

**Whitespace is flexible.** A space in `text`, `left` or `right` matches any run of spaces,
tabs and line breaks, so `text=Total including GST $2,972.00` matches the line above. In
boundary mode whitespace right after `left` is skipped, values are trimmed, and with no `right`
the capture ends at the end of the cell, so `left=Total including GST` gives `$2,972.00`. Add
`exact=true` to match whitespace character for character.

**Password-protected PDFs.** Pass `password=` (or `password64=` for passwords with `+ & % #`
or non-ASCII characters). The open (user) password and the owner password both work; a PDF
protected only against printing/copying opens without one. RC4 40/128-bit, AES-128 and
AES-256 are supported. Without the right password the response is **422**:

```json
{"error": "This PDF is password-protected. Pass password= (or password64=).",
 "status": 422, "passwordRequired": true, "encrypted": true, "filename": "statement.pdf"}
```

`passwordIncorrect: true` means the password was wrong; `unsupported: true` a non-standard
(e.g. certificate) security handler.

Errors are `{"error": "...", "status": N}`: 400 bad input, 403 recipient or sender not permitted,
404 unknown `key`/`ref`, 422 PDF password missing or wrong, 502 SES refused the message.

### Recipes

**Wait for a verification email and read the code**

```python
import re, requests
BASE = "https://api.example.com/v1"
addr = f"signup+{run_id}@mail.example.com"
# ... trigger the sign-up in the system under test with addr ...
m = requests.get(f"{BASE}/mail/pop", params={"address": addr, "wait": 20}).json()
assert not m["exhausted"], "no email within 20 s"
code = re.search(r"\b(\d{6})\b", m["body"]).group(1)
```

For a longer deadline, loop on `pop?wait=20` until `exhausted` is false; keep the gap between
calls short (the default `grace=2` covers it).

**Assert on the content of an emailed PDF**

```python
att = m["attachments"][0]
r = requests.get(f"{BASE}/mail/attachments/find",
                 params={"ref": att["ref"], "left": "Invoice number"}).json()
assert r["values"] == ["INV-261002-1TFJP"], r
r = requests.get(f"{BASE}/mail/attachments/find",
                 params={"ref": att["ref"], "text": "Total including GST $2,972.00"}).json()
assert r["count"] == 1, r
```

If a search unexpectedly returns 0, look at `{BASE}/mail/attachments/text?ref=...&format=text`
to see the text as the API sees it.

**Read a password-protected statement**

```python
r = requests.get(f"{BASE}/mail/attachments/find", params={
    "ref": att["ref"], "password": "Dob19800101", "left": "Closing balance"}).json()
assert r["document"]["encrypted"] and r["values"] == ["$1,234.56"], r
```

**Send a file into the system under test, then verify it arrived back**

```python
import base64
data = base64.b64encode(open("claim_form.pdf", "rb").read()).decode()
requests.post(f"{BASE}/mail/attachments/save", params={"key": "claim_form.pdf"}, data=data)
requests.get(f"{BASE}/mail/send", params={
    "from": "tester@mail.example.com", "to": "inbox+run42@mail.example.com",
    "subject": "Claim 42", "body": "Please process", "key": "claim_form.pdf"})
back = requests.get(f"{BASE}/mail/pop",
                    params={"address": "inbox+run42@mail.example.com", "wait": 20}).json()
assert back["attachments"][0]["filename"] == "claim_form.pdf"
```

Several attachments in one message: `POST` with `Content-Type: application/json` and
`{"body": "...", "attachments": [{"key": "a.pdf"}, {"ref": "rb0q4ipfcu", "filename": "b.pdf"}]}`.

### Pitfalls

* **Use a fresh address per run** (or `clear` it first). `pop` hands out messages in arrival
  order, so leftovers from an earlier run come out first.
* **Mail is not instant.** Use `wait` (functional tests) rather than a single immediate `pop`.
  Do **not** use `wait` in a load test — each waiting call holds a Lambda for its duration; use
  `wait=0` with client-side back-off there.
* **Drain loops need `grace=0`** when they also pass `wait`, or the last message is re-offered
  (`repeat: true`) for a couple of seconds.
* **Structured send needs `Content-Type: application/json`.** Sent as `text/plain` (Postman's
  default) the JSON goes out as the literal message text with no attachments, and the response
  has a `warning` — surface it.
* `from` must be at the mail domain; `to` must be at the mail domain or allow-listed — anything
  else is a 403 whose body lists what is permitted.
* `attachmentkb`, `key` and `ref` are mutually exclusive on `send`.
* `ref`s expire 7 days after the email arrived. `attachments/save?key=...&ref=...` to keep one.
* The save body is **base64 of the raw bytes** in the POST body, not in the query string. Over
  4096 KB, use `attachments/upload/url` and POST the file to S3 (fields first, file last).
* Sending caps at 28000 KB of attachments, and real mailboxes refuse far less (20–25 MB on the
  wire). A 200 from `send` means SES accepted it, not that it was delivered.
* In `find`, send any pattern containing `+`, `\` or `&` as `regex64` — a raw `+` becomes a space.
  Boundaries (`left`/`right`) are literal text, not regex. `document.characters` near 0 means
  the PDF has no text layer (a scan) — there is no OCR.
* Only PDFs can be opened with a password; password-protected Word/Excel files are not supported.
* Values in the PDF document properties (`in=metadata`) are often cleaner than the printed
  text (e.g. `TotalIncludingGst` = `2972.00`), when the generator writes them.
