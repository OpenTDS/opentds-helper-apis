# Test Helper API v1 — endpoint map

Base: `https://api.<domain>/v1`. All endpoints answer GET; `(+POST)` marks
those that also take a request body. Parameter names in the "Old" column are
the pre-v1 names, for anyone porting a script.

## lists — keyed test-data queues, run variables, counters, cleanup register

| v1 | Params | Old |
|---|---|---|
| `lists/push` | `key`*, any other params become record fields | `/data/new` |
| `lists/pop` | `key`* | `/data/pop` |
| `lists/cyclepop` | `key`* | — (new: pop that starts again at record 1) |
| `lists/random` | `key`* | `/data/get` |
| `lists/list` | `key`*, `start`, `limit` | `/data/list` (variables moved to `vars/list`) |
| `lists/reset` | `key`* | `/data/reset` |
| `lists/clear` | `key`* (records only; vars, counters and cleanup register are kept) | `/data/clear` |
| `lists/codes` | — | `/data/random` |
| `lists/vars/set` | `key`*, `name`*, `value`* | `/data/savevariable` |
| `lists/vars/get` | `key`*, `name`*, `equals`, `wait` | `/data/getvariable` |
| `lists/vars/list` | `key`* | new (was inside `/data/list`) |
| `lists/counters/add` | `key`*, `name` (default `default`), `increment` (default 1) | `/data/updatecount` |
| `lists/counters/get` | `key`*, `name` | `/data/getcount` |
| `lists/cleanup/push` | `key`*, `value`*, `note` | `/data/savecleanup` |
| `lists/cleanup/pop` | `key`* | `/data/getcleanup` |
| `lists/cleanup/list` | `key`*, `status` (all/pending/removed) | `/data/listcleanup` |
| `lists/upload` | `format` (csv/json, preselects the page) | `/data/uploadcsv`, `/data/uploadjson` |
| `lists/upload/url` | `key`*, `format`*, `size` | `/data/uploadurl` |
| `lists/import` (+POST) | `key`*, `format`* (csv/json), `ref` (GET form), `wait` (0–25, default 25), `delimiter` | `/data/importcsv`, `/data/importjson` |
| `lists/import/status` | `key`*, `ref` | `/data/importstatus` |

## names — regional personas

| v1 | Params | Old |
|---|---|---|
| `names/get` | `region` \| `notregion`, `seed` | `/name/get` |
| `names/regions` | — | `/name/regions` |

## lock — short-lived mutual exclusion

| v1 | Params | Old |
|---|---|---|
| `lock/acquire` | `key`*, `wait` (0–20) | `/lock/lock` |
| `lock/release` | `ref`* | `/lock/release?lockid=` |

## mail — inbound catcher, outbound send, attachment store

| v1 | Params | Old |
|---|---|---|
| `mail/pop` | `address`*, `wait` (0–20), `grace` | `/email/pop?email=` |
| `mail/reset` | `address`* | `/email/reset` |
| `mail/clear` | `address`* | `/email/clear` |
| `mail/send` (+POST; JSON body can carry several attachments) | `from`*, `to`*, `subject`*, `body`* (GET; POST body is the message), `attachmentkb`, `filename`, `key` (saved attachment), `ref` (received attachment), `prepend64`, `overwrite64`, `append64` | `/email/send` (`fromaddress`, `toaddress`, `message`, `attachmentsize`, `name`/`attachmentname`, `injectstart`/`injectmiddle`/`injectend`) |
| `mail/attachments/save` (+POST) | `key`*, `filename`, `ref` (promote a received attachment; POST body = raw bytes as application/octet-stream, or base64 text) | `/email/saveattachment` |
| `mail/attachments/get` | `key` \| `ref`, `url`, `download`, `view`, `password`/`password64` | `/email/getattachment`, `/email/getreceivedattachment` |
| `mail/attachments/delete` | `key`* | `/email/deleteattachment` |
| `mail/attachments/find` | `key` \| `ref`, one of `text`/`text64`, `regex`/`regex64`, `left`(+`right`)/`left64`/`right64`; `password`/`password64`, `in` (`text`\|`metadata`\|`all`), `exact`, `context`, `ignorecase`, `dotall`, `limit` | `/email/findinattachment` (`search`, `leftboundary`/`lb`, `contextchars`, `maxresults`) |
| `mail/attachments/compare` | `key` \| `ref`, `otherkey` \| `otherref`, `password`/`password64`, `otherpassword`/`otherpassword64` | — (new) |
| `mail/attachments/text` | `key` \| `ref`, `password`/`password64`, `page`, `format` (`json`\|`text`) | — (new) |
| `mail/attachments/upload` | — | `/email/upload` |
| `mail/attachments/upload/url` | `key`*, `filename`, `size` | `/email/uploadurl` |

## sms — text messages (real capture handsets, real sends, simulated sends)

| v1 | Params | Old |
|---|---|---|
| `sms/receive` (POST, **`X-Device-Key` header required**) | `number`*; JSON body `{sender, time, body}` | `POST /sms/save?service=` |
| `sms/get` | `number`*, `wait` (0–20), `since`, `real` | `/sms/latest?service=` |
| `sms/list` | `seconds` (default 300), `number`, `real` | `/sms/recent` |
| `sms/send` | `number`* (registered handsets only), `code`, `body` | `/sms/send?service=&message=` |
| `sms/mocksend` | `number`* (not a registered handset), `code`, `body`, `from` | new |

## webhook — capture inbound webhooks

| v1 | Params | Old |
|---|---|---|
| `webhook/receive` (any method) | `key`*, `status` (HTTP status to answer with, default 200) | `POST /webhook/webhook` |
| `webhook/list` | `key`*, `since`, `wait` (0–20), `limit` | `/webhook/list` |
| `webhook/clear` | `key`* | `/webhook/clear` |

## totp — RFC 6238 codes

| v1 | Params | Old |
|---|---|---|
| `totp/set` | `key`*, `user`*, `secret`*, `digits`, `period`, `algorithm` | `/totp/savesecret` |
| `totp/get` | `key`* + `user`*, or `secret` (stateless); `digits`, `period`, `algorithm` with `secret` | `/totp/getcode` |

## qr — QR codes

| v1 | Params | Old |
|---|---|---|
| `qr/get` | `text` \| `text64`, `format` (json default, svg, html), `ecc`, `scale`, `border`, `version` | `/qr/get`, `/qr/svg`, `/qr/show` |

## perf — performance results and statistics

| v1 | Params | Old |
|---|---|---|
| `perf/add` | `key`*, `value`*, `ok`, `time`, `raw` | `/perf/add` (`ts`) |
| `perf/stats` | `key`*, `p`, `since`, `until` | `/perf/stats` (`from`, `to`) |
| `perf/series` | `key`*, `interval`, `p`, `since`, `until` | `/perf/series` |
| `perf/assert` | `key`*, `<stat><op>=<n>` checks, `strict`, `since`, `until` | `/perf/assert` |
| `perf/keys` | `prefix` | `/perf/keys` |
| `perf/clear` | `key`* | `/perf/clear` |
| `perf/export` | `key`*, `format` (csv/json), `limit`, `since`, `until` | `/perf/export` |

## voice — spoken utterances for voice interfaces

New in v1 (no pre-v1 equivalent). `ref` = a generated utterance (kept 30 days after its last
use); `key` = saved audio (kept until deleted) — the same two words as mail attachments.

| v1 | Params |
|---|---|
| `voice/say` (+POST; JSON body adds `segments`, `pronounce`) | `text` \| `text64` (may carry `[tags]`), `emotion`, `voice` (name \| `random`), `language`, `engine` (`neural` default \| `standard` \| `generative`), `format` (`mp3` default \| `ogg` \| `wav` \| `pcm` \| `mulaw` \| `alaw`), `hz`, `seed`, `strict`, `url`, `view`, `download`, `play` (a page that plays it, no player); or `ref` \| `key` in place of text to say a stored utterance again |
| `voice/get` | `ref` \| `key`, `url`, `view`, `download`, `play` |
| `voice/save` (+POST) | `key`*, `ref` (pin a generated utterance; POST body = a recording, raw bytes as application/octet-stream or base64 text), `filename` |
| `voice/delete` | `key`* |
| `voice/voices` | `language`, `engine` |
| `voice/tags` | — |

## discovery

| URL | What |
|---|---|
| `/v1` | Service index (JSON) |
| `/v1/<service>` | Actions for one service (JSON) |
| `/v1/openapi.json`, `/.well-known/openapi.json` | OpenAPI 3.2.1 |
| `/v1/openapi-3.0.json` | The same in OpenAPI 3.0.3 |
| `/v1/llms.txt`, `/llms.txt`, `/.well-known/llms.txt` | LLM guide (Markdown) |
| `/.well-known/api-catalog` | RFC 9727 API catalog |

## Removed from v1

The API-key authorizer.
