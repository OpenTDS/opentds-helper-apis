# Test Helper API v1 — guide for LLM-assisted test scripting

This API exists to make the hard parts of end-to-end test automation easy to script: catching
the email a system sends, answering its MFA prompt, sharing test data between virtual users,
proving a webhook fired, serialising access to a shared resource, and gating a pipeline on
performance results. Every action is a plain HTTPS GET, so any HTTP client — a test framework,
Postman, LoadRunner, curl, or an LLM tool call — can use it with nothing but a URL.

`{BASE}` below means `{BASE_URL}`. The machine-readable spec is `{BASE}/openapi.json`.

## Conventions (the same for every service)

* **URLs:** `{BASE}/<service>/<action>` or `{BASE}/<service>/<resource>/<action>`, all lower
  case. `GET {BASE}` lists the services; `GET {BASE}/<service>` lists its actions.
* **Methods:** every action answers **GET**. Actions that take a body (`lists/import`,
  `mail/send`, `mail/attachments/save`) also accept POST; `webhook/receive` accepts any method.
* **No authentication** for callers. CORS is open. Do not put real personal data or real credentials in it.
  (The one key in the system is the device key that capture handsets send to `sms/receive`.)
* **Parameters:** lower-case single words, **case-insensitive names**. A name ending in `64`
  takes base64 (use it for values containing `+ & % #`). Booleans: `true/false`, `1/0`,
  `yes/no`. Always URL-encode values; `+` must be sent as `%2B`.
* **Shared vocabulary:** `key` = your namespace (1–128 chars: letters, digits,
  `. _ - : @ + =`); `address` = an email address; `ref` = a handle the API gave you, passed
  back; `wait` = seconds (≤ 20) to hold the request open for something to happen;
  `since`/`until` = epoch seconds; `limit`, `start`, `format`, `seed`.
* **Verbs mean the same thing everywhere:** `push` append, `pop` take-next (consuming),
  `random` any-without-consuming, `get` read one, `set` write one, `add` accumulate,
  `list` read many, `reset` rewind (data kept), `clear` delete that collection for the key,
  `delete` delete one.
* **Responses:** JSON with camelCase fields; the namespace is echoed back; times are UTC epoch
  seconds in fields ending `Time`; other units are in the field name (`waitedMs`,
  `ageSeconds`).
* **Branch on flags, not status codes, for "nothing yet":** an empty queue is HTTP 200 with
  `exhausted: true`; a busy lock is 200 with `locked: false`. A missing named item
  (`vars/get`, `totp/get`, `mail/attachments/get`) is 404.
* **Errors:** `{"error": "message", "status": 400}` (400 bad input, 403 not permitted,
  404 not found, 412 perf gate failed with `strict=true`, 422 PDF password missing or wrong,
  429 rate limited (sms send), 5xx server/upstream).

## Isolating test runs

Everything is namespaced, and nothing needs creating in advance. Start each run with

```
GET {BASE}/lists/codes        -> {"rand3": "...", "rand4": "...", "rand6": "...", "rand8": "...", "rand10": "7Q2K9ZX4M1"}
```

and use the `rand10` in every key and address for that run (`key=run-7Q2K9ZX4M1`,
`address=signup+7q2k9zx4m1@{MAIL_DOMAIN}`). Concurrent runs then never collide.

## Choosing a service

| The test needs to… | Use |
|---|---|
| give each virtual user its own account / record, load a CSV of test data | `lists` (`push`, `pop`, `import`) |
| pass a value between scripts, generate a sequence number | `lists/vars`, `lists/counters` |
| remember what it created so teardown can delete it | `lists/cleanup` |
| invent a realistic person's name for a region | `names` |
| receive the email the app sends (verification code, link, PDF) | `mail/pop` |
| read the text message the app sends, or simulate one | `sms/get`, `sms/mocksend` |
| send an email into the app, with an attachment | `mail/send` |
| read or search inside an emailed PDF / Office file | `mail/attachments/find` (`password=` for protected PDFs), `mail/attachments/text` |
| answer an authenticator-app MFA prompt | `totp` |
| prove the app called a webhook, with the right payload | `webhook` |
| stop two scripts using the same account/device at once | `lock` |
| show or scan a QR code | `qr` |
| record response times and fail the build on a p90 threshold | `perf` |

## End-to-end recipe: sign-up with email verification and MFA

```
1. GET {BASE}/lists/codes                                   -> run = rand10
2. GET {BASE}/names/get?region=AU&seed=<run>                -> femaleFirstName / femaleLastName
3. address = "signup+<run>@{MAIL_DOMAIN}"                   (lower case; send + as %2B)
4. Submit the app's sign-up form with the name and address.
5. GET {BASE}/mail/pop?address=<address>&wait=20            -> exhausted must be false
   extract the verification link or code from body with a regex; follow / submit it.
6. On the MFA enrolment screen, capture the base32 secret.
   GET {BASE}/totp/set?key=run-<run>&user=<address>&secret=<secret>
   GET {BASE}/totp/get?key=run-<run>&user=<address>         -> submit code
7. GET {BASE}/lists/push?key=users-pool&email=<address>&totpKey=run-<run>
   (bank the account so a load test can pop it later)
8. GET {BASE}/lists/cleanup/push?key=run-<run>&value=<userId>   (teardown deletes it)
```

## Load-testing notes

* Use `wait=0` in high-volume virtual users; `wait` holds a Lambda for its whole duration and is
  meant for functional tests and pipelines.
* `lists/pop` hands out each record exactly once across all callers; `lists/random` is for
  read-only reuse.
* Time helper calls in their own transactions so they are not counted in the application's
  response times.
* Record timings with `perf/add` (comma-separated batches of up to 100 values per call are
  cheapest) and gate with `perf/assert`.
