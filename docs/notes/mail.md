# mail — integration notes

Source modules (all flat in `v1/src/`):

| File | Role |
|---|---|
| `mail.py` | API handler `mail.handler` — routes via `common.run` |
| `mail_inbox.py` | `pop`, `reset`, `clear` (DynamoDB pointer queue; race-safe conditional pointer advance, pending/skip logic, grace re-offer — unchanged from the old function) |
| `mail_send.py` | `send` (SES v1 `SendEmail` for plain mail, SESv2 `SendEmail` Raw for attachments) |
| `mail_attachments.py` | `attachments/save|get|delete|find|text|upload|upload/url` + the uploader HTML page |
| `mail_extract.py` | text extraction for find/text (PDF via `mail_pdf.py`, Office XML, text) and the search helpers |
| `mail_pdf.py`, `mail_pdf_metrics.py`, `mail_crypto.py` | pure-Python PDF reader: objects, object streams, fonts/ToUnicode, page layout, standard security handler (RC4, AES-128/256) |
| `mail_extract.py` | stdlib PDF / OOXML / text extraction and literal / regex / boundary search (SIGALRM guards) |
| `mail_store.py` | shared env config, lazily-built boto3 clients (regional SigV4 S3 client for presigning), ref/key helpers |
| `mail_receive.py` | S3-event handler `mail_receive.handler` |

No helpers were missing from `common.py`.

## Lambda functions

### MailFunction — `mail.handler`

* Runtime python3.12, **MemorySize 1769** (one full vCPU: PDF extraction and building a ~28 MB
  MIME message are CPU-bound; the old send and attachment functions were both 1769), **Timeout 30**.
* Invoked by API Gateway: `/mail` and `/mail/{proxy+}` (ANY), AWS_PROXY.
* Environment:

| Var | Value |
|---|---|
| `MAIL_TABLE` | the mail table name |
| `MAIL_DOMAIN` | the mail domain (the SES domain identity and the inbound MX domain), e.g. `example.com` |
| `INBOUND_BUCKET` | inbound bucket name |
| `ATTACHMENT_BUCKET` | saved-attachment bucket name |
| `ALLOWED_TO` | comma-separated extra recipient addresses (optional, may be empty) |
| `ALLOWED_DOMAINS` | comma-separated extra recipient domains; `*.example.com` = any subdomain, not the apex (optional) |
| `BASE_URL` | `https://api.<domain>/v1` (used by the uploader page) |

* IAM (role for MailFunction):
  * `dynamodb:GetItem, Query, UpdateItem, DeleteItem, BatchWriteItem` on the mail table.
  * `ses:SendEmail, ses:SendRawEmail` on
    `arn:aws:ses:<region>:<acct>:identity/<MAIL_DOMAIN>`, `identity/*.<MAIL_DOMAIN>`,
    `identity/*@<MAIL_DOMAIN>`, and `configuration-set/*` (a default configuration set on the
    shared identity is authorised against its ARN too; this grants nothing about who we may send
    as). SESv2 `SendEmail` is authorised as `ses:SendEmail`.
  * Attachment bucket: `s3:GetObject, s3:PutObject, s3:DeleteObject` on `<bucket>/*`,
    `s3:ListBucket` on the bucket (so a missing key is a 404, not a 403).
  * Inbound bucket: `s3:GetObject` on `<inbound>/attachments/*`, `s3:ListBucket` on the bucket.
    (Promote uses `CopyObject`: GetObject on the source + PutObject on the destination is enough.)
  * `AWSLambdaBasicExecutionRole`.
* No async self-invocation.

Trade-off to be aware of: the old `pop` ran at 128 MB; now it shares the 1769 MB function, so a
held `pop?wait=20` costs ~14x more per second. If that matters, deploy a second function with the
same handler/env at 128 MB and route only `/mail/pop` to it (routing is by action, so the same
code works).

### MailReceiveFunction — `mail_receive.handler`

* Runtime python3.12, MemorySize 256 (raise to 1024 if very large inbound mails are expected —
  SES accepts 40 MB and the parser holds several copies), Timeout 30.
* Invoked by S3 `s3:ObjectCreated:*` on the inbound bucket, prefix `inbound/`.
  `AWS::Lambda::Permission` for `s3.amazonaws.com` with `SourceAccount` and `SourceArn` = the
  inbound bucket ARN (build the ARN from the bucket's literal name to avoid a circular
  dependency, as the old template did).
* Environment: `MAIL_TABLE`, `MAIL_DOMAIN`. (The bucket comes from the event.)
* IAM: `s3:GetObject` on `<inbound>/inbound/*`, `s3:PutObject` on `<inbound>/attachments/*`;
  `dynamodb:UpdateItem, PutItem` on the mail table; basic execution role.

## Table — MailTable

* Key: `EmailAddress` (S, HASH), `Serial` (N, RANGE). `PAY_PER_REQUEST`. No GSIs. No TTL
  (a TTL would delete the newest row out from under a claimed serial and leave `pop` reporting
  `pending` forever — see open questions).
* Items: `Serial = 0` header row `{MessageCount, NextMessage}`; `Serial >= 1` message rows
  `{SavedTime, Subject, FromAddr, MessageText, S3Key, Attachments: [{filename, ref, bytes}]}`.
* `DeletionPolicy: Retain` was used before; optional for v1.

## Buckets

### Inbound bucket (SES writes raw mail here)

* Prefixes: `inbound/` (raw MIME from the SES receipt rule S3 action, `ObjectKeyPrefix: inbound/`),
  `attachments/<ref>` (extracted attachments, metadata `filename`, content type from the MIME part).
* Lifecycle: expire `inbound/` after 7 days; expire `attachments/` after 7 days.
* Notification: `LambdaConfigurations` → `s3:ObjectCreated:*`, filter prefix `inbound/`,
  → MailReceiveFunction. (Bucket `DependsOn` the invoke permission.)
* Bucket policy: allow `ses.amazonaws.com` `s3:PutObject` on `<bucket>/*` with
  `aws:SourceAccount` = account. Must exist before the receipt rule.
* Block all public access; SSE-S3.
* No CORS needed (received attachments are only downloaded via presigned GET URLs, which a
  browser navigates to rather than fetches).

### Attachment bucket (saved store)

* Object key = the caller's `key` (flat, no prefix). No lifecycle (kept until deleted).
* CORS (the uploader page POSTs straight to S3; presigned GETs for large downloads):
  `AllowedMethods [POST, PUT, GET, HEAD]`, `AllowedOrigins ["https://api.<domain>"]` (plus the
  execute-api origin if the page is ever served from it), `AllowedHeaders ['*']`,
  `ExposedHeaders [ETag, Location]`, `MaxAge 3000`.
* Block all public access; SSE-S3.

### SES (unchanged from the old template)

Domain identity for `MAIL_DOMAIN` with DKIM + custom MAIL FROM, MX to
`inbound-smtp.<region>.amazonaws.com`, a receipt rule set with a rule
`Recipients: [<MAIL_DOMAIN>]`, `ScanEnabled: true`, `TlsPolicy: Optional`, S3 action to the inbound
bucket with prefix `inbound/`, and the rule-set activator custom resource.
`SesRuleSetActivatorFunction` was not part of this port.

## Behaviour changes vs the old API

* Routes/params renamed per ENDPOINTS.md: `email`→`address`, `fromaddress`→`from`,
  `toaddress`→`to`, `message`→`body`, `attachmentsize`→`attachmentkb`,
  `name`/`attachmentname`→`filename`, `inject{start,middle,end}`→`{prepend,overwrite,append}64`,
  `fileref`→`ref` everywhere (including pop's attachment list and structured-send items),
  `search`/`search64`→`text`/`text64`, `leftboundary`/`lb`→`left`, `rightboundary`/`rb`→`right`
  (+`left64`/`right64`), `contextchars`→`context`, `maxresults`→`limit`.
  `getreceivedattachment` is merged into `attachments/get?ref=`.
* **Identification is explicit:** `key` = saved store, `ref` = received store. The old
  "temporary-first, then permanent" fallback (a `ref` that could be either, a `key` that might be a
  pop token) is gone; passing both is a 400.
* **Keys** follow the universal v1 key rule (1–128 chars, letters/digits and `. _ - : @ + =`),
  wider than the old `[A-Za-z0-9._-]`.
* **pop** response: camelCase v1 fields — `address`, `exhausted`, `repeat` (always present),
  `receivedTime` (was `savedTimeUtc`), `body` (was `message`); `attachments` is always a list.
  Empty inbox and `pending` both carry `exhausted: true` and `body: null`.
* **clear**: `deleted` counts messages only (the old count included the header row).
  **reset** adds `reset: true|false`.
* **send**:
  * `from` is now validated: it must be at `MAIL_DOMAIN` or a subdomain (403 with `allowedFrom`).
    Before, any `fromaddress` was passed to SES and failed there (IAM/identity) as a 502.
    Display-name form `Name <addr@domain>` is still accepted.
  * Recipients at `MAIL_DOMAIN` are always permitted (new) in addition to `ALLOWED_TO` /
    `ALLOWED_DOMAINS`; 403 body also reports `mailDomain`.
  * `subject` and `body` are required (400 if missing); an empty POST body falls back to `?body=`.
  * New `ref` query parameter attaches a received attachment directly.
  * `attachmentkb` out of range is a 400 (not clamped — clamping would silently send a different file).
  * Response: `sentTime` (was `sendTimeUtc`), `attachments` always present; the single-attachment
    duplicates `attachment` / `attachmentBytes` were dropped; `messageMB`→`messageMb`.
    SES failure is `{"error", "status": 502}` instead of `{"result": "error"}`.
  * Structured JSON items use `key` or `ref` (exactly one) instead of the ambiguous `fileref`.
* **attachments/save**: response gains `filename` and `source` (`upload`/`received`); a GET
  without `ref` is a 400 telling you to POST.
* **attachments/get**: one response shape for both stores: `key`|`ref`, `source`
  (`saved`/`received`, was `permanent`/`temporary`), `filename`, `bytes`, `contentType`, and
  `data` or `url` + `expiresInSeconds` (was `expiresIn`).
* **attachments/delete**: `{key, deleted, comment}` (was `result`/`found`); still idempotent 200.
* **attachments/find**: all fields camelCase (`count`, `results[].offset/matched/value/context/
  contextEscaped/groups/named`, `values`, `document.*`...). `SearchTerm` and
  `Match` merged into `match` (`mode`, `text`|`regex`|`left`/`right`, `pattern`, `ignoreCase`,
  `dotAll`, `context`). `right` without `left` is a 400.
* **attachments/find (PDF rework, Oct 2026)**: PDFs are now read by `mail_pdf.py`, which lays
  text out in visual lines (tab between columns) instead of one fragment per line; spaces in
  terms/boundaries match any whitespace run unless `exact=true`; boundary captures skip leading
  whitespace, are trimmed, and stop at the end of the cell without `right`; empty captures are
  not counted (`emptyMatches`); results carry `page`; `in=metadata|all`; `password`/`password64`
  (422 when missing or wrong). `relaxedCount`/`relaxedResults` replaced by `caseInsensitiveCount`
  + `hint`. New `attachments/text`. Pure-Python AES runs at ~0.85 MB/s, enough for text streams;
  if the `cryptography` package is present in the package it is used instead.
* **attachments/upload/url**: `maxKb`, `expiresInSeconds` (were `maxKB`, `expiresIn`).
* **Uploader page**: no api-key handling; calls absolute `BASE_URL` v1 URLs.
* **Receive**: RFC 2047-encoded `Subject`/`From` headers are now decoded; S3 event keys are
  URL-decoded; the attachment list is stored with `ref` (not `fileref`). No other change.
* API-key auth removed.

## Open questions

1. Should message rows get a TTL (e.g. 30 days) to bound table growth? It is safe only if the
   pop "newest claim, no row yet → pending" branch also treats a row older than a few minutes as
   gone (e.g. compare the header's last-claim time). Not done.
2. `from` subdomains: SES domain verification covers subdomains, and the old IAM policy allowed
   `*.<domain>`, so subdomains are accepted. Tighten to the apex only if preferred.
3. Should `mail/pop` get its own 128 MB function (see trade-off above)?
4. Recipient selection on receive still reads To/Cc/Delivered-To/X-Original-To. A BCC'd copy
   with no matching header lands under the first header address rather than the envelope
   recipient (SES's S3 action does not add the envelope recipient as a header). Unchanged
   behaviour; switching the receipt rule to an SNS/Lambda action would expose the envelope.
