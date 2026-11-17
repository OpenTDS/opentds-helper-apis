# Test Helper API v1

> A free, no-login HTTP API that gives software tests the things they usually have to fake: queues of test data, throwaway email inboxes, SMS capture, webhook capture, locks, TOTP codes, QR codes, realistic personas, performance statistics and spoken utterances for voice interfaces. Every endpoint is a plain URL you can open in a browser.

Base URL: `{BASE_URL}`

This file is written for AI assistants and code generators. It is deliberately explicit and repetitive. If you are an assistant helping someone write a test, you should be able to construct a correct URL from this file alone, without guessing.

---

## 0. How to use this file

1. Read **Section 1 (Universal rules)** once. It applies to every endpoint.
2. Find the service you need in **Section 2 (Which endpoint do I need?)**.
3. Copy the example URL for that endpoint from **Section 3** and change the parameter values.
4. If you are chaining several calls, look at **Section 4 (Recipes)** for the standard patterns.
5. If a call fails, check **Section 5 (Errors)**.

Every example in this file is a complete URL. Replace the values after `=` with your own.

**Before writing anything, decide whether this is a one-off run or a test cycle.**

* Use the **cycle pattern** (recipe 4.12) when the request mentions more than one script, parallel or virtual users, load generators, CI, or data reused across runs. One initiation script creates a namespace and the starting data, and only then marks the cycle ready. Every other script waits until the cycle is ready, reads the namespace from a well-known variable, and runs independently, on any machine, started at the same time as initiation or at any later time.
* Use a **single per-run key** from `lists/codes` when one script creates and consumes everything itself and nothing survives the run.
* When neither is clear, build the cycle pattern and say so in one sentence. Ask the person only if the answer changes what must be created up front, and then ask exactly one question: "Should I create the namespace and seed data once in a separate initiation step, so the test scripts can run independently and repeatedly, or should each run create its own?"

**Key naming convention used in the recipes.** `<project>` is the fixed control key for the system under test (e.g. `verdant-7QK2`): every script hard-codes it, and it holds the cycle's two control variables, `namespace` (the source of truth for the current cycle's namespace) and `setupReady` (`0` while initiation runs, `1` when the data is ready). This API is open and shared, so make `<project>` distinctive — a plain word such as `regression` or `test` may be used by someone else. `<ns>` is the namespace for one test cycle — a `rand10` code from `lists/codes` (e.g. `AN672Q1V2B`). Data pools are `<ns>_<kind>` or `<ns>_<kind>_<qualifier>` (e.g. `AN672Q1V2B_users_gold`). Underscores, hyphens, dots and colons are all legal in a key; pick one separator and keep it.

---

## 1. Universal rules (apply to EVERY endpoint)

### 1.1 URL shape

```
{BASE_URL}/<service>/<action>
{BASE_URL}/<service>/<resource>/<action>
```

* Everything in the path is lower case, single words, no hyphens, no underscores, no camelCase.
* Services: `lists`, `names`, `lock`, `mail`, `sms`, `webhook`, `totp`, `qr`, `perf`, `voice`.
* `GET /v1` returns a JSON index of all services.
* `GET /v1/<service>` returns a JSON list of that service's actions.
* Path segments are matched case-insensitively too (`/v1/lists/LIST` works), but always write them lower case.
* An unknown action returns 404 with the valid actions in an `actions` array, e.g. `{ "error": "Unknown action 'peek' for service 'lists'.", "status": 404, "actions": ["cleanup/list", "cleanup/pop", …] }`. Use this to self-correct.
* The version is in the path (`/v1`). v1 URLs will never change meaning.

### 1.2 HTTP methods

* **Every endpoint answers GET.** You can drive the whole API with nothing but URLs. When in doubt, use GET.
* Five endpoints also accept a request body with POST: `lists/import`, `mail/send`, `mail/attachments/save`, `voice/say`, `voice/save`. For these the query-string parameters are identical in GET and POST; POST just lets you send a large body.
* `webhook/receive` accepts ANY method (GET, POST, PUT, PATCH, DELETE…) because it is the target your system-under-test calls.
* `sms/receive` is POST only and needs a secret header (see Section 3.5). Normal callers never use it.
* All other endpoints ignore the method: a POST with query parameters behaves exactly like the GET.
* `OPTIONS` returns 204 with CORS headers.

### 1.3 Authentication

* **There is no authentication.** No API key, no token, no login. Just call the URL.
* CORS is open (`Access-Control-Allow-Origin: *`), so browser code can call it directly.
* The single exception is `sms/receive`, which requires the `X-Device-Key` header. Only registered capture handsets have that key.
* Because there is no login, anyone who guesses your `key` can read what is under it. Use a random key per run (see `lists/codes`) and never store real personal data or real credentials.

### 1.4 Query parameters

* Parameter names are lower case single words and are **matched case-insensitively**: `key=`, `Key=` and `KEY=` all work. Prefer lower case.
* One name per concept. There are no aliases. If this file says the parameter is `address`, do not try `email`.
* Booleans accept `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`.
* Out-of-range numbers are **clamped**, not rejected, where that makes sense (`wait`, `scale`, `limit`, `context`). Asking for `wait=60` gives you `wait=20`.
* Malformed values (letters where a number is expected, etc.) return 400.

### 1.5 Base64 parameters (the `64` suffix)

Any parameter whose name ends in `64` takes a base64-encoded value: `text64`, `regex64`, `left64`, `right64`, `password64`, `prepend64`, `overwrite64`, `append64`.

* Standard or URL-safe base64 alphabet; padding (`=`) optional.
* **Use the `64` version whenever your value contains `+`, `&`, `%`, `#`, `?`, `=` or a newline.** Those characters do not survive a query string.
* Example: the text `A&B+C` as `text=A&B+C` is broken. As `text64=QSZCK0M` it works.

### 1.6 Shared parameter vocabulary

These names mean the same thing in every service.

| Parameter | Meaning |
|---|---|
| `key` | A namespace or item name YOU choose. 1–128 characters. Allowed: letters, digits and `. _ - : @ + =`. Must start with a letter, digit or `+`. **Case-sensitive** (`Users` and `users` are different keys). A `+` must be sent as `%2B`, because a bare `+` in a URL becomes a space. |
| `address` | An email address. This is the mail catcher's namespace. Case-insensitive. |
| `name` | A named item inside a key: a variable or a counter. |
| `user` | A user inside a key (TOTP only). |
| `ref` | A handle the API gave you earlier that you pass back: an import reference, a received-attachment reference, a lock reference, a generated utterance. Never invent one. |
| `value` | The thing being stored. |
| `text` / `text64` | Literal text (a QR payload, a search term, the words to speak). |
| `wait` | Seconds to hold the request open waiting for something to happen. Range 0–20 (0–25 for `lists/import`). `wait=0` returns immediately. |
| `since` / `until` | A time window as UTC epoch **seconds** (e.g. `1760054400`). `sms/get` and `webhook/list` also take epoch milliseconds in `since`. |
| `limit` | Maximum number of items to return. |
| `start` | First item to return, **1-based** (`start=1` is the first item). |
| `format` | Output or input format: `json`, `csv`, `svg`, `html`, `text`; for `voice`, the audio format (`mp3`, `wav`, …). |
| `seed` | Any string or number; the same seed gives the same "random" result every time. |
| `url` / `view` / `download` | Booleans on the actions that return a file (`mail/attachments/get`, `voice/say`, `voice/get`): a presigned URL instead of base64, or a redirect to the file itself. `voice` also has `play`: a web page that plays the audio by itself. |

### 1.7 Action vocabulary

The same verb means the same thing in every service.

| Action | Meaning |
|---|---|
| `push` | Append an item to the END of a queue. |
| `pop` | Take the NEXT item and advance the pointer. **Consuming**: each item is handed out once. |
| `cyclepop` | Like `pop`, but after the last item it starts again at the first instead of running out. Each item is handed out once per pass. |
| `random` | Return a random item WITHOUT consuming it. |
| `get` | Read one thing. No side effects. |
| `set` | Create or overwrite a named value. |
| `add` | Add to an accumulator (increment a counter, add a performance data point). |
| `list` | Read many things, with totals. |
| `reset` | Rewind the pointer to the start. Data is KEPT. (`pop` will hand out the same items again.) |
| `clear` | Delete every item of that collection under the namespace. Data is GONE. |
| `delete` | Delete one item. |
| `acquire` / `release` | Take / give back a lock. |
| `send` / `receive` | Outbound / inbound message. |
| `save` | Store a file. |
| `say` | Turn text into spoken audio (`voice` only). The same request always returns the same stored audio. |
| `find` | Search inside stored content. |
| `text` | Extract the text of a stored file. |
| `import` / `status` | Bulk load, and its progress. |
| `upload` | A browser page for humans (HTML). `upload/url` returns a presigned S3 POST for programs. |

Read-only nouns (`codes`, `regions`, `stats`, `series`, `keys`, `export`, `voices`, `tags`) simply return the thing they are named after.

### 1.8 Responses

* Content type is `application/json; charset=utf-8` unless the action is explicitly a page (`text/html`), an image (`image/svg+xml`), plain text (`format=text`) or an export (`text/csv`).
* **Response field names are camelCase** (`savedTime`, `receivedTime`, `waitedMs`).
* User data you stored (list record fields, webhook bodies) comes back **exactly as you stored it**, including its original casing.
* Every response echoes the namespace it acted on: `key` or `address`.
* Times are UTC epoch **seconds** in fields ending `Time` (`savedTime`, `createdTime`, `receivedTime`). Other units are spelled out in the name: `waitedMs`, `ageSeconds`, `timestampMs`, `bytes`, `sizeKb`.
* `Cache-Control: no-store` on everything except discovery documents. Never cache a response.

### 1.9 Things that are NOT errors (HTTP 200)

Load-test tools count non-2xx responses as failures, so these common situations return 200:

* An empty queue: `lists/pop` returns `200` with `"exhausted": true` and `"record": null` (`lists/cyclepop` never runs out; it reports `exhausted` only for a key with no records at all); `lists/random` returns `200` with `"empty": true`; `mail/pop` returns `200` with `"exhausted": true` and `"body": null`; `sms/get` returns `200` with `"found": false`.
* A counter that was never used: `lists/counters/get` returns `200` with `"value": 0` and `"exists": false`.
* A variable that was never set, read with `wait=` or `equals=`: `lists/vars/get` returns `200` with `"exists": false` and `"value": null` (a plain `vars/get` without them is still a 404).
* Resetting or clearing an already-empty key: `200` with `"reset": false` / `"cleared": false`.
* A lock that is already held: `lock/acquire` returns `200` with `"locked": false`.
* Waiting with `wait=` and nothing arrived: `200` with an empty result.
* A failed `perf/assert` gate: `200` with `"pass": false` (unless you add `strict=true`, which makes it 412).

**Always check the body, not just the status code.**

### 1.10 Things that ARE errors

* A named thing that does not exist (`lists/vars/get`, `totp/get`, `mail/attachments/get`, an unknown `names` region) → **404**.
* Not permitted (`mail/send` with a `from` outside the mail domain or a `to` that is not allowed; `sms/send` to an unregistered number; `sms/mocksend` to a registered one) → **403**, with the allowed values or a `hint` in the body.
* Too fast (`sms/send` more than once per 10 s per handset) → **429** with `retryAfterSeconds`.
* A password-protected PDF opened without the right password → **422** with `passwordRequired` or `passwordIncorrect`.
* `voice/say` with `strict=true` and a tag the engine cannot render → **422** with the `tags` that would not be heard.
* `voice/say` needing new synthesis after the deployment's daily budget is used up → **429** with `budgetExhausted: true` (audio already stored still works).
* An import whose file yields no records or is not parseable → **422** with `state: "failed"`; the key is left unchanged.
* `perf/assert` with `strict=true` whose checks fail → **412**.
* Bad or missing parameters → **400**, e.g. `{ "error": "Missing required query parameter 'key'.", "status": 400 }` or `{ "error": "'start' must be a whole number.", "status": 400, "received": "abc" }`.
* Error body shape is always:

```json
{ "error": "human-readable message", "status": 400 }
```

with extra fields when they help: `hint` (what to do instead), `allowed` (the valid values), `received` (what you sent), `reserved` (names you may not use). Read `hint` and `allowed` first; they usually tell you exactly what to change.

* The one exception is an error raised by the gateway before the request reaches the service (throttling or timeout): `{ "error": "...", "gatewayError": "THROTTLED" }`. Retry after a short pause.

### 1.11 Limits

* The gateway gives up at 29 seconds, so `wait` is capped at 20 s (25 s for `lists/import`).
* A request body is capped at about 4 MB. Anything larger (up to 50 MB for list imports, 100 MB for mail attachments) must go to S3 via an `upload/url` presigned POST.
* Received email and its attachments are kept 7 days; uploaded import files 90 days; raw `perf` points 7 days; locks at most 60 s; generated `voice` utterances 30 days after their last use. Lists, variables, counters, the cleanup register, TOTP secrets, saved attachments and saved audio do not expire.
* One `voice/say` utterance is at most 3000 characters.

### 1.12 Where the answer is in the response

Different services put the thing you asked for in different fields. Read these, not `item`:

| Endpoint | The payload is in | Empty / not-found signal |
|---|---|---|
| `lists/pop` | `record` (object) | `exhausted: true`, `record: null` (200) |
| `lists/cyclepop` | `record` (object), plus `pass` | `exhausted: true`, `empty: true` only when the key has no records (200) |
| `lists/random` | `record` (object) | `empty: true`, `record: null` (200) |
| `lists/list` | `records` (array) | `returned: 0`, `records: []` (200) |
| `lists/vars/get` | `value` (string) | 404 |
| `lists/counters/get` | `value` (number) | `exists: false`, `value: 0` (200) |
| `lists/cleanup/pop` | `value` (string) + `note` | `exhausted: true`, `value: null` (200) |
| `lists/import` | `loaded`, `warnings`, `sample` | `state: "failed"` (422) |
| `names/get` | the female and male persona fields | 404 for an unknown region |
| `lock/acquire` | `ref` (string) | `locked: false`, `freeInSeconds` (200) |
| `mail/pop` | `body` (string), `subject`, `from`, `attachments[].ref` | `exhausted: true`, `body: null` (200) |
| `mail/attachments/text` | `pages[].text` | 404 / 422 |
| `mail/attachments/find` | `count`, `values`, `results[]` | `count: 0` with `hint` (200) |
| `mail/attachments/compare` | `same` (boolean), `verdict` | 404 |
| `sms/get` | `body`, `sender`, `mocked` | `found: false`, `body: null` (200) |
| `webhook/list` | `events` (array) | `events: []` (200) |
| `totp/get` | `code` (string), plus `nextCode`, `previousCode`, `secondsRemaining` | 404 |
| `qr/get` | `png` / `dataUri` (json) or the image (svg) | — |
| `perf/stats` | count, mean, `p<nn>` fields | `count: 0` |
| `voice/say`, `voice/get` | `data` (base64 audio) or `url`; plus `ref`, `text`, `durationMs`, `tags[]` | 404 for an unknown `ref`/`key` |
| `perf/assert` | `pass` (boolean), `checks[]` | `pass: false` (200, or 412 with `strict=true`) |

---

## 2. Which endpoint do I need?

| I want to… | Call |
|---|---|
| Store rows of test data (users, orders, accounts) and hand each row out once | `lists/push` then `lists/pop` |
| Hand rows out one at a time, but go round again instead of running out (reusable data that must not be in use by two scripts at once) | `lists/cyclepop` — see recipe 4.14 |
| Pick a random row without using it up | `lists/random` |
| Start handing out the same rows again from the beginning | `lists/reset` |
| Load rows from a CSV or JSON file (replaces the queue) | small file: POST `lists/import`; big file: `lists/upload/url` → S3 POST → GET `next` |
| Get a random string to build a unique key or run id | `lists/codes` |
| Store a single value by name (a token, an id, a URL) for later test steps | `lists/vars/set` / `lists/vars/get` |
| Make many scripts wait until test setup has created the data they need, then all use the same namespace | `lists/vars/set` (`setupReady`, `namespace`) / `lists/vars/get?…&name=setupReady&equals=1&wait=20` — see recipe 4.12 |
| Count things across many test runs | `lists/counters/add` / `lists/counters/get` |
| Remember records that must be deleted after the test | `lists/cleanup/push` / `lists/cleanup/pop` / `lists/cleanup/list` |
| Get realistic fake names for a region (a female and a male persona sharing a surname, with ASCII forms) | `names/get` |
| Stop two parallel tests touching the same thing at once | `lock/acquire` / `lock/release` |
| Receive an email my application sent (verification links, OTP codes) | `mail/pop` |
| Send an email INTO my application (email-to-case, inbound parsing) | `mail/send` (the recipient must be at `{MAIL_DOMAIN}` or allow-listed) |
| Send an email with an attachment of a given size or content | `mail/send` with `attachmentkb`, `key` or `ref` |
| Store a file to attach to emails later | `mail/attachments/save` or `mail/attachments/upload/url` |
| Store a file my app produced (e.g. a PDF downloaded from an order screen) so it can be checked | POST `mail/attachments/save?key=…&filename=….pdf` |
| Check that two files are the same document (the emailed PDF vs the one my app serves) | `mail/attachments/compare` |
| Open or download an attachment in a browser (one URL) | `mail/attachments/get?ref=<ref>&view=true` / `&download=true` (add `&password=…` for a protected PDF) |
| Read or search the text inside an attachment (PDF, Word, Excel…) | `mail/attachments/text` / `mail/attachments/find` |
| Receive a real SMS my application sent to a test phone | `sms/get` |
| Send a real SMS to a test phone | `sms/send` |
| Simulate an inbound SMS without a real phone | `sms/mocksend` then `sms/get` |
| Give my application a URL to call, then see what it sent | `webhook/receive` (the target) then `webhook/list` |
| Generate a 2FA / authenticator code | `totp/get` |
| Generate a QR code image | `qr/get` |
| Record response times and get percentiles | `perf/add` then `perf/stats` |
| Fail a pipeline if performance is too slow | `perf/assert` |
| Get spoken audio to play into a voice chatbot, phone menu or talk button | `voice/say` — see recipe 4.15 |
| Make the speech sound anxious, angry, slow, quiet… | `voice/say` with `emotion=` or `[tags]` in the text; then read `tags[].rendered` |
| Replay exactly the audio a failed test used | `voice/get?ref=<ref>` |
| Let a person hear an utterance in a browser with no player on screen | `voice/say?text=…&play=true` (or `voice/get?ref=<ref>&play=true`) |
| Keep an utterance, or a recording of a real voice, for good | `voice/save` |
| See which voices exist and what each engine can render | `voice/voices` / `voice/tags` |

---

## 3. Services and endpoints

Each endpoint below shows: what it does, the parameters (`*` = required), a full example URL, a minimal example response, and notes. The `key` in every example is just an example; pick your own.

### 3.1 `lists` — keyed test-data queues, variables, counters, cleanup register

**Concept.** A `key` names a queue. You `push` records onto it (or `import` a CSV/JSON file); each record is a bag of fields you choose. `pop` hands records out one at a time in order and never hands the same record out twice until you `reset`. Alongside the queue, the same `key` also owns a set of named **variables**, a set of named **counters**, and a **cleanup register**. `lists/clear` deletes only the queue records; variables, counters and the cleanup register under the same key survive.

**Every record has three reserved fields** added by the API: `key`, `index` (1-based position in the queue) and `createdTime`. You cannot push a field with one of those names (any letter case) — it is a 400.

Pick a unique key per project or run. `lists/codes` gives you random strings to build one from.

#### `lists/codes` — random strings (no key needed)

No parameters. Returns five random upper-case alphanumeric codes of different lengths. Use one to make a unique key or run id.

```
GET {BASE_URL}/lists/codes
```

Response (200):
```json
{ "rand3": "FUS", "rand4": "J7YW", "rand6": "EIPN0K", "rand8": "8Z30CBOE", "rand10": "AN672Q1V2B" }
```

Pattern: `runKey = "myproject-" + rand10`, then use `runKey` (or `runKey + "-users"`, `runKey + "-orders"`) as the `key` in every later call.

#### `lists/push` — append a record

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue name |
| anything else | | Every other query parameter becomes a field of the record, name and value exactly as sent. An empty value (`middle=`) is stored as `""`. |

```
GET {BASE_URL}/lists/push?key=customers&email=ann@example.com&FirstName=Ann&plan=gold
```

Response (200):
```json
{
  "key": "customers",
  "index": 1,
  "createdTime": 1791667461,
  "count": 1,
  "record": {
    "FirstName": "Ann",
    "email": "ann@example.com",
    "plan": "gold",
    "key": "customers",
    "index": 1,
    "createdTime": 1791667461
  }
}
```

Notes: field names keep the case you send (`FirstName` stays `FirstName`). All pushed values are strings. URL-encode spaces as `%20`, `+` as `%2B`, `&` as `%26`, `%` as `%25`, `#` as `%23` — the stored value is decoded (`note=A%2BB%20%26%20C` is stored as `A+B & C`). `count` is the total records now in the queue. Pushing `index=`, `key=` or `createdTime=` as a field is a 400 with a `reserved` list.

#### `lists/pop` — take the next record (consuming)

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue name |

```
GET {BASE_URL}/lists/pop?key=customers
```

Response (200, record available):
```json
{
  "key": "customers",
  "exhausted": false,
  "index": 1,
  "count": 5,
  "remaining": 4,
  "record": { "FirstName": "Ann", "email": "ann@example.com", "plan": "gold", "key": "customers", "index": 1, "createdTime": 1791667461 }
}
```

Response (200, queue empty or fully consumed — NOT an error):
```json
{
  "key": "customers",
  "exhausted": true,
  "index": null,
  "record": null,
  "message": "No more records to pop. Use lists/reset to start over."
}
```

Notes: read `record`, not `item`. Check `exhausted` before using `record`. `remaining` is how many pops are left before exhaustion. Each pop advances a pointer; `lists/reset` moves it back to 1. Records pushed after the pointer has moved are still popped in order — the pointer is not affected by pushes. Safe to call from many parallel virtual users: no record is ever handed to two callers. If the data may be reused once every record has had its turn, use `lists/cyclepop` instead of resetting by hand.

#### `lists/cyclepop` — take the next record, starting again at the first after the last

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue name |

```
GET {BASE_URL}/lists/cyclepop?key=accounts
```

Response (200):
```json
{
  "key": "accounts",
  "exhausted": false,
  "index": 1,
  "count": 3,
  "pass": 2,
  "wrapped": true,
  "remaining": 2,
  "record": { "username": "tester01", "password": "Pa55-1", "key": "accounts", "index": 1, "createdTime": 1791667461 }
}
```

Response (200, the key has no records at all):
```json
{ "key": "accounts", "exhausted": true, "empty": true, "index": null, "count": 0, "pass": null, "record": null, "message": "No records for this key. Push or import some first; cyclepop never runs out after that." }
```

What it does: the same as `pop` — the next record, handed out in order, one atomic step per call, so parallel callers never receive the same record at the same moment — except that after the last record it hands out record 1 again, then 2, and so on. A record comes round again only after every other record has been handed out.

Fields: `pass` = how many times round the list this call is (1 on the first pass); `wrapped` = true on the first record of every pass after the first; `remaining` = records left in this pass; `index` and `count` as for `pop`.

Notes:
* `exhausted` is never true while the key has records — you do not need to call `reset`.
* It shares `pop`'s pointer: `lists/reset` and `lists/import` start it at record 1 again, and `lists/list` shows the pointer as `nextPop` (it keeps counting past `count`; the next cyclepop returns record `((nextPop - 1) mod count) + 1`). After cyclepop has gone round, a plain `pop` on the same key reports `exhausted` until a `reset`. Use one or the other on a key, not both.
* Records pushed while cycling make the cycle longer from the next call on. The position is always `((calls so far) mod count) + 1`, so it can jump: after 7 calls on 3 records (pointer at record 2), a fourth record makes the 8th call return record 4.
* **It does not lock anything.** It guarantees order, not exclusivity: a record that a slow script is still using comes round again after `count` more calls. Size the list as recipe 4.14 explains. For data that must never be used twice, use `pop`.

#### `lists/random` — read a random record (non-consuming)

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue name |

```
GET {BASE_URL}/lists/random?key=customers
```

Response (200, records exist):
```json
{ "key": "customers", "empty": false, "count": 5, "index": 2, "record": { "FirstName": "Bob", "email": "bob@example.com", "plan": "silver", "key": "customers", "index": 2, "createdTime": 1791667462 } }
```

Response (200, no records):
```json
{ "key": "customers", "empty": true, "count": 0, "index": null, "record": null, "message": "No records for this key." }
```

Notes: this one uses `empty`, not `exhausted`. The pointer does not move; the same record may come back twice. Records already handed out by `pop` can still be returned by `random`.

#### `lists/list` — read many records with totals

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue name |
| `start` | | First record index to return, 1-based. Default 1. Must be a whole number |
| `limit` | | Maximum records to return |

```
GET {BASE_URL}/lists/list?key=customers&start=2&limit=2
```

Response (200):
```json
{
  "key": "customers",
  "count": 5,
  "nextPop": 1,
  "start": 2,
  "returned": 2,
  "truncated": false,
  "nextStart": 4,
  "importRef": null,
  "records": [
    { "FirstName": "Bob", "email": "bob@example.com", "plan": "silver", "key": "customers", "index": 2, "createdTime": 1791667462 },
    { "email": "carol@example.com", "note": "A+B & C 100% #1", "key": "customers", "index": 3, "createdTime": 1791667462 }
  ]
}
```

Fields: `count` = total records in the queue; `nextPop` = the index the next `pop` will return (`null` when the queue is empty, `count + 1` when fully consumed); `nextStart` = the `start` value for the next page, or `null` when there are no more; `truncated` = the response hit its size limit (about 4 MB) before `limit`, so fetch the rest from `nextStart`; `importRef` = the ref of the import that loaded these records, or `null` if they were pushed. A `start` beyond `count` returns `returned: 0` and an empty `records` array, not an error. Variables are NOT included here; use `lists/vars/list`.

#### `lists/reset` — rewind the pop pointer, keep the data

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue name |

```
GET {BASE_URL}/lists/reset?key=customers
```

Response (200): `{ "key": "customers", "reset": true, "count": 5, "nextPop": 1, "message": "Pointer reset; the next pop returns index 1." }`
Response (200, empty queue): `reset: false`, `count: 0`, `nextPop: null` — not an error.

#### `lists/clear` — delete all records under the key

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue name |

```
GET {BASE_URL}/lists/clear?key=customers
```

Response (200): `{ "key": "customers", "cleared": true, "deleted": 6, "backgroundDelete": 0, "message": "All records for this key removed." }`

Notes: deletes queue records only; variables, counters and the cleanup register are kept. Pushed records are deleted immediately (`deleted`); records that came from an import are removed in the background (`backgroundDelete`) but the key reads as empty straight away. An already-empty key returns `cleared: false` — not an error.

#### `lists/vars/set` — store a named value

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |
| `name` | * | Variable name, up to 256 characters. **Case-sensitive** (`orderId` and `OrderId` are different) |
| `value` | * | Value to store (overwrites). May be empty (`value=`) but the parameter must be present |

```
GET {BASE_URL}/lists/vars/set?key=run42&name=orderId&value=SO-1001
GET {BASE_URL}/lists/vars/set?key=run42&name=baseUrl&value=https%3A%2F%2Fshop.example.com%2Fa%3Fb%3D1%26c%3D2
```

Response (200): `{ "key": "run42", "name": "orderId", "value": "SO-1001", "savedTime": 1791667474 }`

URL-encode the value; it is stored decoded (the second example stores `https://shop.example.com/a?b=1&c=2`).

#### `lists/vars/get` — read a named value, optionally waiting for it

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |
| `name` | * | Variable name (case-sensitive) |
| `equals` | | Value to compare with — exact and case-sensitive. Adds `matched` to the response |
| `wait` | | Seconds to hold the request open, 0–20 (clamped). With `equals`: until the value equals it, or changes to something else. Without `equals`: until the variable exists |

```
GET {BASE_URL}/lists/vars/get?key=run42&name=orderId
GET {BASE_URL}/lists/vars/get?key=verdant-7QK2&name=setupReady&equals=1&wait=20
GET {BASE_URL}/lists/vars/get?key=run42&name=orderId&wait=20
```

Response (200, plain): `{ "key": "run42", "name": "orderId", "value": "SO-1001", "savedTime": 1791667474 }`
Response (404, plain, never set): `{ "error": "Variable not found.", "status": 404, "key": "run42", "name": "orderId" }`

Response (200, `equals=1&wait=20`, set to 1 after 3.4 s):
`{ "key": "verdant-7QK2", "name": "setupReady", "value": "1", "savedTime": 1791667474, "exists": true, "equals": "1", "matched": true, "waitedMs": 3412 }`

Response (200, `equals=1&wait=20`, never set and nothing happened):
`{ "key": "verdant-7QK2", "name": "setupReady", "value": null, "savedTime": null, "exists": false, "equals": "1", "matched": false, "waitedMs": 20004 }`

Notes:
* `value` is always a string (or `null` when never set). Compare with `"1"`, not the number 1.
* With `wait` or `equals`, a variable that was never set is `200` with `exists: false`, not a 404 — so a wait loop only ever sees 200.
* With `equals` and `wait`, the request returns early in two cases: the value now equals `equals` (`matched: true`), or the value changed to something else while you waited (`matched: false`, e.g. `0` → `failed`, or a variable that did not exist being set to `0`). Branch on `matched` and `value`, then call again if you still need to wait.
* Without `equals`, `wait` holds the request until the variable exists (useful for "wait until the namespace has been published").
* Reads are strongly consistent: a value written just before is always seen.
* One call waits at most 20 seconds (the gateway gives up at 29 s). Wait longer by repeating the call in a loop with your own overall time limit.

#### `lists/vars/list` — all variables under a key

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |

```
GET {BASE_URL}/lists/vars/list?key=run42
```

Response (200):
```json
{ "key": "run42", "count": 2, "variables": [ { "name": "baseUrl", "value": "https://shop.example.com/a?b=1&c=2", "savedTime": 1791667476 }, { "name": "orderId", "value": "SO-1001", "savedTime": 1791667474 } ] }
```

Sorted by name. Empty: `count: 0`, `variables: []`.

#### `lists/counters/add` — increment a counter

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |
| `name` | | Counter name. Default `default`. 1–64 characters: letters, digits, `.`, `_`, `-` |
| `increment` | | Whole number to add. Default 1. Negative allowed (`-2`), zero allowed (reads the value), fractions not. Range ±1,000,000,000,000,000. A leading `+` must be sent as `%2B` |

```
GET {BASE_URL}/lists/counters/add?key=run42&name=orders
GET {BASE_URL}/lists/counters/add?key=run42&name=orders&increment=5
```

Response (200): `{ "key": "run42", "name": "orders", "increment": 1, "value": 1, "updatedTime": 1791667481 }`

`value` is the new total after the add. Safe to call from many parallel virtual users; each call is atomic. A counter is created on first `add`. One counter handles roughly 1,000 updates per second; for unique values at higher rates use `lists/codes`.

#### `lists/counters/get` — read a counter

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |
| `name` | | Counter name. Default `default` |

```
GET {BASE_URL}/lists/counters/get?key=run42&name=orders
```

Response (200, counter exists): `{ "key": "run42", "name": "orders", "value": 4, "exists": true, "updatedTime": 1791667482 }`
Response (200, counter never used): `{ "key": "run42", "name": "orders", "value": 0, "exists": false, "updatedTime": null }`

Notes: a missing counter is a 200 with `value: 0` and `exists: false`, NOT a 404 (unlike `vars/get`).

#### `lists/cleanup/push` — register something to delete later

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |
| `value` | * | The identifier of the thing to clean up (an order id, a username…). Must not be empty. Up to 1,024 characters |
| `note` | | Free text, e.g. what kind of thing it is or which virtual user made it |

```
GET {BASE_URL}/lists/cleanup/push?key=run42&value=ORD-10021&note=order
```

Response (200): `{ "key": "run42", "index": 1, "value": "ORD-10021", "note": "order", "savedTime": 1791667485, "registered": true }`

The same `value` can be registered more than once (each is its own entry). `note` is `null` when omitted.

#### `lists/cleanup/pop` — take the next pending cleanup item

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |

```
GET {BASE_URL}/lists/cleanup/pop?key=run42
```

Response (200, item pending): `{ "key": "run42", "exhausted": false, "index": 1, "value": "ORD-10021", "note": "order", "savedTime": 1791667485, "removedTime": 1791667487 }`
Response (200, nothing pending): `{ "key": "run42", "exhausted": true, "index": null, "value": null, "message": "Nothing left to clean up for this key." }`

Items come out oldest first. Each pop marks the item removed (sets `removedTime`) and hands it out once, even to parallel callers. Delete the real thing in your system using `value`, then pop the next. Note it is marked removed when it is handed out, not when you confirm the delete.

#### `lists/cleanup/list` — see the cleanup register

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |
| `status` | | `all` (default), `pending` or `removed`. Anything else is a 400 |

```
GET {BASE_URL}/lists/cleanup/list?key=run42&status=pending
```

Response (200):
```json
{
  "key": "run42",
  "saved": 4,
  "removed": 1,
  "pending": 3,
  "nextPop": 2,
  "filter": "pending",
  "returned": 3,
  "truncated": false,
  "records": [
    { "index": 2, "value": "ORD-2", "note": null, "savedTime": 1791667485, "removedTime": null },
    { "index": 3, "value": "ORD-1", "note": "vu2", "savedTime": 1791667485, "removedTime": null },
    { "index": 4, "value": "cust/42?x=1&y=2", "note": null, "savedTime": 1791667486, "removedTime": null }
  ]
}
```

`saved` = total ever registered, `removed` = popped so far, `pending` = still to pop. A pending record has `removedTime: null`.

#### `lists/import` (GET or POST) — bulk load records from CSV or JSON

**Import REPLACES everything in the key.** Whatever records the key held before are deleted and the file's records become the new queue (the response's `replaced` field tells you how many were replaced). To add records one at a time, use `lists/push` instead. If the import fails, the key is left exactly as it was.

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue to load into |
| `format` | * | `csv` or `json` |
| `ref` | | An import reference (GET form). Either the `ref` from `lists/upload/url` after you have posted the file to S3, or the `ref` of a previous import to reload the same file. Do NOT send both a `ref` and a body |
| `wait` | | Seconds to wait for the import to finish, 0–25 (or `true`/`false`). Default 25. `wait=0` returns 202 immediately with `state: "queued"`; poll `lists/import/status` |
| `delimiter` | | CSV only. One of the WORDS `comma`, `semicolon`, `tab`, `pipe` — not the character itself. Leave it out to auto-detect |

POST form (file is the body; fine up to ~4 MB):
```
POST {BASE_URL}/lists/import?key=customers&format=csv
Content-Type: text/csv

email,name,tier
ann@example.com,Ann Lee,gold
bob@example.com,"Ng, Bob",silver
```

For JSON send `Content-Type: application/json`.

GET form (file already uploaded via `lists/upload/url`, or reloading an earlier import):
```
GET {BASE_URL}/lists/import?key=customers&format=csv&ref=20261010T212514Z-pnnwps
```

Response (200, finished):
```json
{
  "key": "customers",
  "ref": "20261010T212450Z-uc5agl",
  "format": "csv",
  "state": "done",
  "source": "body",
  "fileBytes": 138,
  "options": {},
  "queuedTime": 1791667490,
  "updatedTime": 1791667491,
  "startedTime": 1791667490,
  "phase": "done",
  "total": 3,
  "loaded": 3,
  "shards": 1,
  "finishedTime": 1791667491,
  "durationMs": 239,
  "firstIndex": 1,
  "lastIndex": 3,
  "skippedCount": 0,
  "skipped": [],
  "warnings": [],
  "encoding": "utf-8",
  "delimiter": ",",
  "columns": ["email", "name", "tier"],
  "blankRows": 0,
  "replaced": null,
  "sample": { "email": "ann@example.com", "name": "Ann Lee", "tier": "gold", "key": "customers", "index": 1, "createdTime": 1791667491 },
  "next": {
    "pop": "{BASE_URL}/lists/pop?key=customers",
    "random": "{BASE_URL}/lists/random?key=customers",
    "reset": "{BASE_URL}/lists/reset?key=customers",
    "list": "{BASE_URL}/lists/list?key=customers&limit=10",
    "reload": "{BASE_URL}/lists/import?key=customers&format=csv&ref=20261010T212450Z-uc5agl"
  }
}
```

Read these fields: `state` (`queued`, `running`, `done` or `failed`), `source` (`body`, `upload` or `reload`), `loaded` (records now in the queue), `warnings` (always read them — they explain anything the importer changed), `skipped` (bad lines, with line number and reason), `replaced` (`null` on an empty key, otherwise `{count, importRef}` of what was thrown away), `sample` (the first record, so you can see the field names) and `next` (ready-made URLs for the follow-up calls). JSON imports report `shape` instead of `delimiter`.

Response (202, `wait=0` or still running when `wait` ran out): `{ "key": "...", "ref": "...", "format": "csv", "state": "queued", "source": "body", "fileBytes": 10, "options": {}, "queuedTime": ..., "updatedTime": ... }` — poll `lists/import/status?key=&ref=`.

Response (422, nothing usable in the file): `state: "failed"` with `error` (e.g. `No records found. The key was left unchanged.` or `Invalid JSON at line 3, column 1: …`) and `message: "Nothing was changed: the key still holds whatever it held before this import."`

**CSV rules.** First row is the header; each column becomes a field. All values are strings (`"0800"` stays `"0800"`). Quoted fields, embedded commas, doubled quotes (`""`) and CRLF line endings are handled; UTF-8 and Windows-1252 (Excel's "CSV") are both read correctly. The delimiter is auto-detected (a warning says what was chosen). If a header is a reserved name (`key`, `index`, `createdTime`) it is renamed `key_2`, `index_2`, … with a warning. A file with a header and no rows is a 422. If only one column is found, a warning suggests passing `delimiter=`.

**JSON rules.** The file may be: an array of objects (each object = one record); an object with exactly one array property (that array is loaded, other properties ignored with a warning); JSON Lines (one object per line — an invalid line is skipped and listed in `skipped`, the rest load); a single object (loaded as ONE record, with a warning). Nested objects and arrays are kept as-is in the record. Booleans and numbers keep their JSON type. Integers too large for a JavaScript number are stored as exact strings, with a warning. An array of scalars (`[1, "two", null]`) is stored as records of the shape `{ "value": 1 }`. Invalid JSON that is not JSON Lines is a 422.

**Refs.** A `ref` looks like `20261010T212514Z-pnnwps`. It belongs to one key AND one format: using it with a different key or format is a 404. Uploaded files are kept for 90 days, so the `reload` URL in `next` re-imports the same file any time in that window. A malformed ref is a 400 with a `hint`.

#### `lists/import/status` — progress or result of an import

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue |
| `ref` | | The import ref. If omitted, reports the most recent upload for the key and adds a `recent` array of up to 20 refs, newest first (refs carry a one-second timestamp, so uploads within the same second have no defined order) |

```
GET {BASE_URL}/lists/import/status?key=customers&ref=20261010T212514Z-pnnwps
```

Responses: the same body as `lists/import` with `state` one of `uploaded` (file is in S3 but `lists/import` has not been called yet — the body includes a `next` URL to call), `queued`, `running`, `done` or `failed`. `queued`/`running` answer 202, `done` and `uploaded` 200, `failed` 422. A key with no uploads at all, or an unknown ref, is a 404.

#### `lists/upload` — browser page for humans

| Param | Required | Meaning |
|---|---|---|
| `format` | | `csv` or `json`; preselects the page. Anything else is a 400 |

```
{BASE_URL}/lists/upload?format=csv
```

Returns HTML. Not for programs; programs use `lists/upload/url`.

#### `lists/upload/url` — presigned S3 POST for a large file (up to 50 MB)

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Queue the file will be imported into |
| `format` | * | `csv` or `json` |
| `size` | | Expected size as a whole number of bytes (not `10MB`). Over 52,428,800 is a 400 |

```
GET {BASE_URL}/lists/upload/url?key=customers&format=csv&size=52428800
```

Response (200):
```json
{
  "key": "customers",
  "ref": "20261010T212514Z-pnnwps",
  "format": "csv",
  "method": "POST",
  "url": "https://testhelper-v1-imports-…amazonaws.com/",
  "fields": { "key": "imports/customers/20261010T212514Z-pnnwps.csv", "success_action_status": "201", "x-amz-algorithm": "…", "x-amz-credential": "…", "x-amz-date": "…", "x-amz-security-token": "…", "policy": "…", "x-amz-signature": "…" },
  "maxKb": 51200,
  "expiresInSeconds": 900,
  "expiresTime": 1791668414,
  "next": "{BASE_URL}/lists/import?key=customers&format=csv&ref=20261010T212514Z-pnnwps",
  "note": "POST multipart/form-data to url: every entry of fields first, the file LAST as the part named \"file\". S3 answers 201; then GET next."
}
```

How to use it, exactly:
1. Build a `multipart/form-data` POST to `url`.
2. Add every entry of `fields` as a form part, in any order, BEFORE the file. Copy the values verbatim.
3. Add the file as the LAST part, named `file`.
4. S3 answers `201` (XML body). Anything else means the form was wrong or the 15-minute (`expiresInSeconds`) window has passed — ask for a new URL.
5. `GET` the `next` URL. That runs the import and returns the normal `lists/import` response.

Until step 5, `lists/import/status?key=&ref=` reports `state: "uploaded"` with `message: "Uploaded but not imported yet."`.

---

### 3.2 `names` — regional personas

**Concept.** Returns a pair of realistic fake personas for a region: a female and a male name (first, middle, last and full name) sharing a surname — or the gendered form of it where the language has one, e.g. Polish `-ska`/`-ski` — in the region's name order (`familyNameFirst`, e.g. Japan), with ASCII-folded copies in `ascii` and `hasDiacritics`. Names only: no address, date of birth or other details. Not real people.

#### `names/get` — one persona

| Param | Required | Meaning |
|---|---|---|
| `region` | one of `region` / `notregion` | Region code to draw from (see `names/regions`), case-insensitive. `region=ANY` = no preference |
| `notregion` | | Any region EXCEPT these (one code or a comma-separated list) |
| `seed` | | Same seed → same persona every time |

```
GET {BASE_URL}/names/get?region=au
GET {BASE_URL}/names/get?notregion=us,gb&seed=12
```

Use `seed` when a test must be repeatable. Omit `seed` for a fresh persona each call. An unknown region code is a 404 whose `allowed` lists the valid ones; passing both `region` and `notregion` is a 400.

#### `names/regions` — list valid region codes

No parameters.

```
GET {BASE_URL}/names/regions
```

Call this first if you are not sure which region codes exist.

---

### 3.3 `lock` — short-lived mutual exclusion

**Concept.** Prevents parallel test threads from using the same shared thing at the same time. Acquire a named lock, do the work, release it. A lock lasts at most **60 seconds** (there is no renew), so a crashed test cannot hold one forever. Keep the work inside the lock shorter than that.

#### `lock/acquire`

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Lock name |
| `wait` | | Seconds to keep trying, 0–20. `0` = try once and return |

```
GET {BASE_URL}/lock/acquire?key=sharedAccount1&wait=10
```

Response (200, got it):
```json
{ "locked": true, "key": "sharedAccount1", "ref": "…", "maxSeconds": 60 }
```

Response (200, someone else holds it and `wait` ran out):
```json
{ "locked": false, "key": "sharedAccount1", "ref": null, "freeInSeconds": 41 }
```

Notes: `locked: false` is NOT an error. Keep the `ref`; you need it to release.

#### `lock/release`

| Param | Required | Meaning |
|---|---|---|
| `ref` | * | The `ref` returned by `lock/acquire` |

```
GET {BASE_URL}/lock/release?ref=<ref from lock/acquire>
```

Response (200): `{ "released": true, "key": "sharedAccount1", "ref": "…", "heldSeconds": 3 }`. If the lock had already expired or been taken by someone else: `released: false` with a `reason`.

Always release in a `finally` block. Note you release by `ref`, not by `key`.

---

### 3.4 `mail` — inbound email catcher, outbound send, attachment store

**Concept.** Three things:

1. **Catcher.** Any email sent to any address at `{MAIL_DOMAIN}` is stored. Your test reads it with `mail/pop`. Use this to receive verification links, OTP codes, receipts, etc. The `address` is the namespace; you make it up (e.g. `test-run42@{MAIL_DOMAIN}`). Plus-addressing works (`alice.gallo+run42@{MAIL_DOMAIN}`); send the `+` as `%2B` in URLs.
2. **Sender.** `mail/send` sends an email FROM the API INTO your system, optionally with attachments. Use this to test inbound email processing. Because anyone can call it, `from` must be an address at `{MAIL_DOMAIN}`, and `to` must be at `{MAIL_DOMAIN}` or on this deployment's allow-list — anything else is a 403 whose body lists what is allowed.
3. **Attachment store.** Files stored under a `key` so they can be attached to sent emails, and files received by the catcher (referenced by `ref`) that can be read, searched, saved or re-sent.

#### `mail/pop` — read the next received email

| Param | Required | Meaning |
|---|---|---|
| `address` | * | The inbox address to read |
| `wait` | | Seconds to wait for a message to arrive, 0–20 |
| `grace` | | Seconds (0–60) during which the most recently delivered message is offered again, flagged `repeat: true`, when nothing new has arrived. Default 2 when `wait` is used, otherwise 0. It covers a poll that lands just after another one consumed the message. Use `grace=0` in drain loops |

```
GET {BASE_URL}/mail/pop?address=run42@{MAIL_DOMAIN}&wait=20
```

Response (200, message available):
```json
{
  "address": "run42@{MAIL_DOMAIN}",
  "exhausted": false,
  "repeat": false,
  "serial": 1,
  "remaining": 0,
  "waitedMs": 1830,
  "receivedTime": 1791667500,
  "ageSeconds": 2,
  "subject": "Verify your email",
  "from": "no-reply@yourapp.com",
  "body": "Hi Ann, your code is 482913 ...",
  "bodyTruncated": false,
  "attachments": [ { "filename": "invoice.pdf", "ref": "rb0q4ipfcu", "bytes": 90941 } ]
}
```

`body` is the plain-text part of the email (the HTML part if there is no plain-text part). Extract codes and links from it with a regex. `bodyTruncated: true` means it was over 300 KB and was clipped. Each attachment's `ref` is what you pass to `mail/attachments/text|find|get|save` and to `mail/send?ref=`.

Response (200, nothing yet — this is NOT an error):
```json
{
  "address": "run42@{MAIL_DOMAIN}",
  "exhausted": true,
  "body": null,
  "remaining": 0,
  "waitedMs": 20003,
  "comment": "no more messages"
}
```

Notes: consuming, like `lists/pop`. Check `exhausted`; if true, loop with `wait=20` a few more times when the email may take longer than 20 s. If you count messages, ignore any with `repeat: true`. `remaining` tells you how many more pops will succeed without waiting. Use `mail/reset` to read the same messages again.

#### `mail/reset` — rewind the inbox, keep the messages

| Param | Required | Meaning |
|---|---|---|
| `address` | * | Inbox |

```
GET {BASE_URL}/mail/reset?address=run42@{MAIL_DOMAIN}
```

#### `mail/clear` — delete all messages in the inbox

| Param | Required | Meaning |
|---|---|---|
| `address` | * | Inbox |

```
GET {BASE_URL}/mail/clear?address=run42@{MAIL_DOMAIN}
```

Call this at the start of a test (or use a fresh address per run) so old messages are not mistaken for new ones.

#### `mail/send` (GET or POST) — send an email

| Param | Required | Meaning |
|---|---|---|
| `from` | * | Sender address. **Must be at `{MAIL_DOMAIN}`** |
| `to` | * | Recipient address. **Must be at `{MAIL_DOMAIN}` or on the deployment's allow-list** |
| `subject` | * | Subject line |
| `body` | * (GET) | Message text. With POST, the request body is the message instead |
| `attachmentkb` | | Generate a dummy attachment of this many KB |
| `filename` | | File name to give the attachment |
| `key` | | Attach a file previously saved under this key |
| `ref` | | Attach a file previously RECEIVED by the catcher (by its ref) |
| `prepend64` | | Base64 content to add at the START of the attachment |
| `overwrite64` | | Base64 content to write over the MIDDLE of the attachment |
| `append64` | | Base64 content to add at the END of the attachment |

`attachmentkb`, `key` and `ref` are mutually exclusive.

Simple (sent to the catcher, so the test can pop it straight back):
```
GET {BASE_URL}/mail/send?from=qa@{MAIL_DOMAIN}&to=inbox-run42@{MAIL_DOMAIN}&subject=Test%20case&body=Please%20help
```

Into your application (its inbound address must be on the allow-list):
```
GET {BASE_URL}/mail/send?from=qa@{MAIL_DOMAIN}&to=support@yourapp.example&subject=Test%20case&body=Please%20help
```

With a 2 MB generated attachment:
```
GET {BASE_URL}/mail/send?from=qa@{MAIL_DOMAIN}&to=inbox-run42@{MAIL_DOMAIN}&subject=Big%20file&body=see%20attached&attachmentkb=2048&filename=report.bin
```

With a saved attachment:
```
GET {BASE_URL}/mail/send?from=qa@{MAIL_DOMAIN}&to=inbox-run42@{MAIL_DOMAIN}&subject=Invoice&body=attached&key=invoice-template
```

POST with several attachments: `Content-Type: application/json` and a body of
```json
{ "body": "Please process the attached.", "attachments": [ { "key": "a.pdf" }, { "ref": "rb0q4ipfcu", "filename": "b.pdf" } ] }
```
(`from`, `to` and `subject` stay in the query string). Sent as `text/plain` instead, the JSON goes out as the literal message text with no attachments, and the response carries a `warning`.

Response (200): `result: "sent"`, `messageId`, `sentTime` and `attachments` `[{filename, bytes}]`. A 200 means the mail service accepted the message, not that it was delivered. Attachments are capped at 28,000 KB in total, and real mailboxes usually refuse far less (20–25 MB).

Notes: `prepend64` / `overwrite64` / `append64` let you inject specific bytes into a generated attachment — useful for testing virus scanners or content filters. Long bodies: use POST.

#### `mail/attachments/save` (GET or POST) — store a file under a key

Saved files can be attached to `mail/send`, read with `mail/attachments/text`, searched with `mail/attachments/find`, compared with `mail/attachments/compare`, and downloaded with `mail/attachments/get`.

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Name to store the file under (overwrites) |
| `filename` | | The file's name **including its extension**, e.g. `INV-261010-2AW8T.pdf`. It decides the content type the file is served with (`.pdf` → `application/pdf`, so `get?view=true` opens it as a PDF), the name a browser saves it under, and the default attachment name in `mail/send`. Characters other than letters, digits, `.`, `_`, `-` become `_`. Without it the file is named after its `key` and served as `application/octet-stream` |
| `ref` | | Promote a RECEIVED attachment (by ref) into the store under `key`. With `filename` it is renamed; without, it keeps the name it arrived with |

Promote a received attachment (GET):
```
GET {BASE_URL}/mail/attachments/save?key=customer-pdf&ref=<ref>
```

Upload a file (POST, up to 4,096 KB). The body is the file, in ONE of two forms:

* **Raw bytes** with `Content-Type: application/octet-stream` — the simplest; this is what a browser `fetch` with a `Blob` body or `curl --data-binary` sends:
```
POST {BASE_URL}/mail/attachments/save?key=order-VD-261010-2AW8T&filename=INV-261010-2AW8T.pdf
Content-Type: application/octet-stream

%PDF-1.4 … (the file's bytes)
```
* **Base64 text** of the file with any other content type, e.g. `text/plain`:
```
POST {BASE_URL}/mail/attachments/save?key=invoice-template&filename=invoice-template.pdf
Content-Type: text/plain

JVBERi0xLjQKJ...
```

Do NOT send raw bytes with `Content-Type: application/pdf` (or any type other than `application/octet-stream`): the gateway would corrupt them, so the API refuses with a 400 that says which header to use.

Response (200): `{ "result": "saved", "key": "order-VD-261010-2AW8T", "bytes": 7412, "filename": "INV-261010-2AW8T.pdf", "contentType": "application/pdf", "source": "upload", "bodyFormat": "binary", "sha256": "a90b…" }`. Without `filename` the response carries a `hint`.

**Saving a file your application serves, from inside a browser.** When the file comes from a page of the application under test (e.g. a "Download invoice" link on an order screen), run this in that page so the request carries the signed-in session, then POST the bytes on (CORS is open, so the browser allows it):
```js
const pdf = await (await fetch('/orders/VD-261010-2AW8T/invoice.pdf')).blob();   // the link's href
const r = await fetch('{BASE_URL}/mail/attachments/save?key=order-VD-261010-2AW8T&filename=INV-261010-2AW8T.pdf',
                      { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: pdf });
console.log(await r.json());   // result: "saved", contentType: "application/pdf"
```
From a script or a file on disk:
```
curl --data-binary @INV-261010-2AW8T.pdf -H "Content-Type: application/octet-stream" "{BASE_URL}/mail/attachments/save?key=order-VD-261010-2AW8T&filename=INV-261010-2AW8T.pdf"
```

For files over ~4 MB use `mail/attachments/upload/url`. Saved files do not expire; received ones (`ref`) expire 7 days after the email arrived, so save any you need to keep.

#### `mail/attachments/get` — download a stored or received file

| Param | Required | Meaning |
|---|---|---|
| `key` | one of | A saved attachment |
| `ref` | one of | A received attachment |
| `url` | | `true` → return a short-lived download URL instead of the content |
| `download` | | `true` → redirect (302) straight to the file; a browser saves it under its filename |
| `view` | | `true` → redirect (302) straight to the file, shown in the browser (a PDF opens in a tab) |
| `password` / `password64` | | Password of a protected PDF (open or owner password). The file is served **unlocked** — an unprotected copy — so the browser shows it without asking for the password. Works with every form: `data`, `url`, `download`, `view` |

```
GET {BASE_URL}/mail/attachments/get?key=invoice-template
GET {BASE_URL}/mail/attachments/get?ref=<ref>&url=true
{BASE_URL}/mail/attachments/get?ref=<ref>&view=true        ← paste into a browser: the PDF opens
{BASE_URL}/mail/attachments/get?ref=<ref>&download=true    ← paste into a browser: the file is saved
{BASE_URL}/mail/attachments/get?ref=<ref>&view=true&password=627026   ← a protected PDF opens without a password prompt
```

Response (200): `filename`, `bytes`, `contentType`, `source` (`saved` or `received`) and either `data` (base64 of the file) or `url` + `expiresInSeconds` (15 minutes). With `download=true` or `view=true` the response is a 302 to the file instead, for use in a browser or with an HTTP client that follows redirects. 404 if the key or ref does not exist.

With `password`, a protected PDF is decrypted into an unprotected copy (kept for one day) and that copy is what you get, in whichever form you asked for; the response adds `unlocked: true`, `encrypted: true` and `originalBytes`. A PDF that is not encrypted is served as it is (`unlocked: false`). A wrong password is 422 with `passwordIncorrect`; a non-PDF file with `password` is 400. PDFs up to 10,240 KB can be unlocked.

#### `mail/attachments/delete` — delete a saved file

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Saved attachment to delete |

```
GET {BASE_URL}/mail/attachments/delete?key=invoice-template
```

#### `mail/attachments/text` — extract the text of a file

| Param | Required | Meaning |
|---|---|---|
| `key` | one of | Saved attachment |
| `ref` | one of | Received attachment |
| `password` / `password64` | | Password for a protected PDF (the open password or the owner password) |
| `page` | | Return only this page number (1-based) |
| `format` | | `json` (default) or `text` (plain text body) |

```
GET {BASE_URL}/mail/attachments/text?ref=<ref>&page=1
GET {BASE_URL}/mail/attachments/text?ref=<ref>&format=text
```

Response (200, JSON): `pages` `[{page, text}]`, `metadata` (the PDF document properties, e.g. `Title`, `Author`, and custom ones such as `InvoiceNumber`), `document` (`pages`, `characters`, `encrypted`…) and `truncated`. This is exactly the text `mail/attachments/find` searches: each visual line of a PDF page is one line, with a **tab** between columns (a label and its value) and a single space between words, e.g. `Total including GST\t$2,972.00`. Use it to work out a search term.

422 with `passwordRequired` or `passwordIncorrect` for a protected PDF. `document.characters` near 0 means the PDF is a scan with no text layer (there is no OCR). Handles PDF, Word, Excel, PowerPoint and text files; only PDFs can be opened with a password.

#### `mail/attachments/compare` — is this the same document?

Compares two stored files: typically the PDF that arrived by email (`ref`) and the copy downloaded from the application and saved with `mail/attachments/save` (`otherkey`).

| Param | Required | Meaning |
|---|---|---|
| `key` / `ref` | one of | The first file (saved / received) |
| `otherkey` / `otherref` | one of | The second file (saved / received) |
| `password` / `password64` | | Password for a protected PDF; used for both files unless `otherpassword` is given |
| `otherpassword` / `otherpassword64` | | Password for the second file |

```
GET {BASE_URL}/mail/attachments/compare?ref=e1pv4cugg4&otherkey=order-VD-261010-2AW8T
```

Response (200):
```json
{
  "same": true,
  "sameBytes": false,
  "sameText": true,
  "sameProperties": false,
  "verdict": "same content: the text is identical but the files differ byte-for-byte (normal for a PDF generated twice - creation date and internal IDs change)",
  "first":  { "ref": "e1pv4cugg4", "source": "received", "filename": "INV-261010-2AW8T.pdf", "bytes": 7410, "sha256": "3f1c…", "pages": 1, "characters": 1774, "encrypted": false },
  "second": { "key": "order-VD-261010-2AW8T", "source": "saved", "filename": "INV-261010-2AW8T.pdf", "bytes": 7412, "sha256": "a90b…", "pages": 1, "characters": 1774, "encrypted": false },
  "lines": { "first": 41, "second": 41 },
  "differenceCount": 0,
  "differences": [],
  "truncated": false,
  "propertyDifferences": { "CreationDate": { "first": "D:20261010094512Z", "second": "D:20261010101233Z" } }
}
```

How to read it:
* **`same`** is the answer to "is it the same document?" It equals `sameText`: the extracted text is identical line for line (runs of spaces/tabs and blank lines are ignored).
* `sameBytes` is stricter: the files are byte-for-byte identical (compare the `sha256` values too). A PDF generated a second time usually has a new creation date and internal IDs, so `sameBytes: false` with `sameText: true` is normal and still the same document. If your test requires the very same file, assert `sameBytes`.
* `verdict` says which case applies, in words: `identical…`, `same content…` or `different…`.
* When the text differs, each entry of `differences` is `{ "change": "changed" | "onlyInFirst" | "onlyInSecond", "firstPage", "secondPage", "first": [lines], "second": [lines] }`, e.g. `{ "change": "changed", "firstPage": 1, "secondPage": 1, "first": ["Total including GST $114.00"], "second": ["Total including GST $141.00"] }`. Up to 50 are listed; `differenceCount` is the total.
* `propertyDifferences` lists PDF document properties (Title, CreationDate, custom keys…) that differ. They are reported but do not affect `same`.

Works for any file type `mail/attachments/text` reads (PDF, Word, Excel, PowerPoint, text). 404 if either file does not exist; 422 for a protected PDF without the right password.

#### `mail/attachments/find` — search inside a file

| Param | Required | Meaning |
|---|---|---|
| `key` | one of | Saved attachment |
| `ref` | one of | Received attachment |
| `text` / `text64` | one search type | Literal text to find |
| `regex` / `regex64` | one search type | Regular expression (Python syntax) to find |
| `left` (+ `right`) / `left64` / `right64` | one search type | Return the text between a left boundary and a right boundary (boundaries are literal text, not regex). Without `right`, captures to the end of the cell (next tab or line end) |
| `password` / `password64` | | Password for a protected PDF |
| `in` | | Where to search: `text` (default), `metadata` (the PDF document properties, one `Name<TAB>value` line each), or `all` |
| `exact` | | Boolean. By default a space in `text`, `left` or `right` matches any run of spaces, tabs or line breaks, and boundary captures are trimmed. `exact=true` matches whitespace character for character and does not trim |
| `context` | | Characters of surrounding text to return with each match. 0–500, default 50 |
| `ignorecase` | | Boolean |
| `dotall` | | Boolean; let `.` match newlines. Default `true` for boundaries, `false` for `regex` |
| `limit` | | Maximum matches to return. 1–50, default 20. `count` always reports the true total |

Find a phrase that spans a label and its value (the default flexible whitespace matches the tab between them):
```
GET {BASE_URL}/mail/attachments/find?ref=<ref>&text=Invoice%20number%20INV-261002-1TFJP
```

Find an invoice number with a regex:
```
GET {BASE_URL}/mail/attachments/find?ref=<ref>&regex=INV-%5Cd%7B6%7D&limit=1
```

Capture the value next to a label (returns `$2,972.00` from `Total including GST\t$2,972.00`):
```
GET {BASE_URL}/mail/attachments/find?ref=<ref>&left=Total%20including%20GST
```

Extract the value between two boundaries (returns what is between `Total:` and `AUD`):
```
GET {BASE_URL}/mail/attachments/find?ref=<ref>&left=Total%3A&right=AUD
```

Search a password-protected PDF, or its document properties:
```
GET {BASE_URL}/mail/attachments/find?ref=<ref>&text=Visa%20ending%201111&password=secret
GET {BASE_URL}/mail/attachments/find?ref=<ref>&in=metadata&left=InvoiceNumber
```

Response (200): `count` (true total), `results[]` `{offset, page, matched, context, contextEscaped, value}`, `values` (the flat list of captures, for `regex` and boundaries), `emptyMatches` (captures that came out empty; not counted), `truncated`, `document`. When `count` is 0 you also get `caseInsensitiveCount` and a `hint`.

Notes: if the regex contains `+`, `&`, `%` or `#`, use `regex64`. Provide exactly ONE search type per call. If a search unexpectedly finds nothing, look at `mail/attachments/text?ref=<ref>&format=text` to see the text exactly as it is searched.

#### `mail/attachments/upload` — browser page for humans

No parameters. Returns HTML.

```
{BASE_URL}/mail/attachments/upload
```

#### `mail/attachments/upload/url` — presigned S3 POST for a large file

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Name the file will be saved under |
| `filename` | | The file's name with its extension, as for `mail/attachments/save` |
| `size` | | Expected size in bytes (up to 100 MB) |

```
GET {BASE_URL}/mail/attachments/upload/url?key=big-scan&size=20971520
```

Same procedure as `lists/upload/url`: every entry of `fields` first (with `filename` they include its name and `Content-Type`), the file LAST as the part named `file`; S3 answers 201 and the file is then saved under `key`.

---

### 3.5 `sms` — text messages

**Concept.** There are **registered handsets** (real phones running a capture app) and **everything else**.

* Real inbound texts to a registered handset are captured by the phone and posted to `sms/receive`. Your test reads them with `sms/get`.
* `sms/send` sends a REAL text, but only to a registered handset. Any other number is refused (403) with a ready-made `mockUrl` for `sms/mocksend`.
* `sms/mocksend` SIMULATES an inbound text to any non-registered number. `sms/get` returns it flagged `mocked: true`. Use this when you have no real handset.
* Real sends are rate-limited to one per 10 seconds per handset.

A `number` is the phone number, preferably in international format, e.g. `+61400000000`. **Send `+` as `%2B`**: `number=%2B61400000000`. Numbers are matched on their last nine digits, so `0400000000` and `%2B61400000000` are the same handset.

#### `sms/receive` (POST, capture handsets only)

Requires the `X-Device-Key` header. Only the capture app calls this. **Do not call it from a test.** Listed for completeness.

| Param | Required | Meaning |
|---|---|---|
| `number` | * | The handset's own number |
| JSON body | * | `{ "sender": "...", "time": <epoch MILLISECONDS>, "body": "..." }` |

#### `sms/get` — read the latest text for a number

| Param | Required | Meaning |
|---|---|---|
| `number` | * | Phone number (registered or mocked) |
| `wait` | | Seconds to wait for a NEW text (one arriving after the call) to arrive, 0–20 |
| `since` | | Only return a text received at or after this time: epoch seconds, or epoch **milliseconds** for sub-second precision (any value over 100,000,000,000 is read as milliseconds) |
| `real` | | Boolean; `true` = only real captured texts, ignore mocked ones |

```
GET {BASE_URL}/sms/get?number=%2B61400000000&wait=20&since=1760054400
```

Response (200, found): `found: true`, `number`, `sender`, `body`, `receivedTime`, `mocked` (true for a simulated text) and `handsetTime`.
Response (200, nothing): `found: false`, `body: null`, `waitedMs` — not an error.

Notes: always pass `since` = the time just before your application sent the text, so an older message is not returned by mistake.

#### `sms/list` — recent texts

| Param | Required | Meaning |
|---|---|---|
| `seconds` | | Look back this many seconds. Default 300 |
| `number` | | Limit to one number |
| `real` | | Boolean; `true` = real only |

```
GET {BASE_URL}/sms/list?seconds=600&number=%2B61400000000
```

Returns `count`, `numbers` and `messages` (newest first, each with `mocked`).

#### `sms/send` — send a real text (registered handsets only)

| Param | Required | Meaning |
|---|---|---|
| `number` | * | A registered handset number |
| `code` | | 1–12 letters or digits. Default: a random 6-digit code. Returned in the response |
| `body` | | Message text; `{code}` in it is replaced by the code |

```
GET {BASE_URL}/sms/send?number=%2B61400000000&code=483921
```

Response (200): `sent: true`, `mocked: false`, `to`, `code`, `messageId` and a ready-made `getUrl` to read it back. A number that is not a registered handset gets a 403 with a `hint` and a `mockUrl` for `sms/mocksend`. More than one send per 10 s to the same handset is a 429 with `retryAfterSeconds`.

#### `sms/mocksend` — simulate an inbound text

| Param | Required | Meaning |
|---|---|---|
| `number` | * | Any number that is NOT a registered or capture handset (those are a 403) |
| `code` | | Code to embed. Default: a random 6-digit code |
| `body` | | Message text; `{code}` in it is replaced by the code |
| `from` | | Simulated sender |

```
GET {BASE_URL}/sms/mocksend?number=%2B61411111111&body=Your%20code%20is%20%7Bcode%7D&from=YourApp
```

Response (200): `sent: true`, `mocked: true`, `code`, `body` and a `getUrl`. Then `sms/get?number=%2B61411111111` returns it, flagged `mocked: true`. At most one per second per number (429 otherwise).

---

### 3.6 `webhook` — capture inbound webhooks

**Concept.** Give your application the `webhook/receive` URL as its callback / notification URL. Whatever it sends is stored under the `key`. Your test then reads the captured events with `webhook/list`.

#### `webhook/receive` (ANY method) — the target your application calls

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace to store events under |
| `status` | | HTTP status to answer the caller with, 200–599. Default 200. Use 500 or 503 to test the caller's retry logic |

Configure in your application:
```
{BASE_URL}/webhook/receive?key=order-events
{BASE_URL}/webhook/receive?key=order-events&status=503
```

Stores the method, headers, query string and body exactly as received.

#### `webhook/list` — read captured events

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |
| `since` | | Only events received at or after this time: epoch seconds, or epoch **milliseconds** for sub-second precision (any value over 100,000,000,000 is read as milliseconds). With seconds, earlier events in the same second are included; use milliseconds to exclude them |
| `wait` | | Seconds to wait for at least one new event, 0–20 |
| `limit` | | Maximum events to return |

```
GET {BASE_URL}/webhook/list?key=order-events&since=1760054400&wait=20
```

Returns `events`, newest first; each includes `receivedTime`, `receivedTimeMs`, `method`, `headers`, `query` (every parameter except `key` and `status`), `body` (exactly as sent; JSON bodies also parsed into `json`; non-UTF-8 bodies come back as `bodyBase64`) and `sourceIp`. `count`, plus `more: true` when `limit` cut the list short.

#### `webhook/clear` — delete captured events

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace |

```
GET {BASE_URL}/webhook/clear?key=order-events
```

---

### 3.7 `totp` — RFC 6238 one-time codes

**Concept.** Generates the same 6-digit (by default) code an authenticator app would show. Either store the secret once with `totp/set` and ask for codes by `key` + `user`, or pass the `secret` directly each time (stateless).

#### `totp/set` — store a secret

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Namespace (e.g. the application name) |
| `user` | * | The user the secret belongs to |
| `secret` | * | The base32 secret from the enrolment screen / QR code |
| `digits` | | Code length. Default 6 |
| `period` | | Seconds per code, 10–300. Default 30 |
| `algorithm` | | `sha1` (default), `sha256`, `sha512` |

```
GET {BASE_URL}/totp/set?key=myapp&user=ann&secret=JBSWY3DPEHPK3PXP
```

#### `totp/get` — current code

| Param | Required | Meaning |
|---|---|---|
| `key` + `user` | one form | Use a stored secret |
| `secret` | other form | Stateless: compute from this secret directly |
| `digits`, `period`, `algorithm` | | Only with `secret`; stored form uses the values from `totp/set` |

Stored:
```
GET {BASE_URL}/totp/get?key=myapp&user=ann
```

Stateless:
```
GET {BASE_URL}/totp/get?secret=JBSWY3DPEHPK3PXP
```

Response (200):
```json
{
  "key": "myapp",
  "user": "ann",
  "code": "378339",
  "nextCode": "985104",
  "previousCode": "632053",
  "secondsRemaining": 16.06,
  "period": 30,
  "digits": 6,
  "algorithm": "sha1"
}
```

Use `code`. If `secondsRemaining` is below about 5, either wait and call again, or submit `nextCode` instead — most servers accept the adjacent period. `previousCode` is there for the same reason in the other direction. 404 if `key`/`user` is unknown.

Note: `user` is often an email address. If it contains `+`, send it as `%2B`: `user=alice.gallo%2Bqa%40{MAIL_DOMAIN}`.

---

### 3.8 `qr` — QR codes

#### `qr/get`

| Param | Required | Meaning |
|---|---|---|
| `text` / `text64` | * | The payload to encode (one or the other, not both) |
| `format` | | `json` (default: the image as base64 PNG in `png`, a ready `dataUri`, plus size details), `svg` (image), `html` (a page showing it) |
| `ecc` | | Error correction level: `L`, `M` (default), `Q`, `H` |
| `scale` | | Pixels per module, 1–40. Default 8 |
| `border` | | Quiet-zone modules. Default 4 |
| `version` | | Minimum QR version 1–40 (size); omit to auto-size |

```
GET {BASE_URL}/qr/get?text=https%3A%2F%2Fexample.com%2Fjoin%3Fcode%3D1234&format=svg
GET {BASE_URL}/qr/get?text64=aHR0cHM6Ly9leGFtcGxlLmNvbS9qb2luP2NvZGU9MTIzNA
```

Use `text64` whenever the payload has `?`, `&`, `+` or `#` — which URLs usually do. `format=svg` returns `image/svg+xml`. To show the JSON form in a page, put `dataUri` in an `<img src>`.

---

### 3.9 `perf` — performance results and statistics

**Concept.** Push individual timings with `perf/add`; read back statistics (`perf/stats`), a time series (`perf/series`), or a pass/fail check (`perf/assert`). The `key` is the transaction or test name.

#### `perf/add` — record one result

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Transaction name, e.g. `login` |
| `value` | * | The measurement, normally milliseconds. Several values may be sent comma-separated (`value=412,398,455`) |
| `ok` | | Boolean; `false` marks the sample(s) as failures. Default `true` |
| `time` | | Epoch seconds when it happened. Default now |
| `raw` | | Boolean; `true` also keeps each individual point (for 7 days) so `perf/export` can return it. Statistics never need it |

```
GET {BASE_URL}/perf/add?key=login&value=412&ok=true
GET {BASE_URL}/perf/add?key=login&value=5021&ok=false&raw=true
```

#### `perf/stats` — summary statistics

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Transaction name |
| `p` | | Percentiles to include, comma-separated, e.g. `90,95,99` (decimals allowed: `99.5`) |
| `since` / `until` | | Epoch-seconds window |

```
GET {BASE_URL}/perf/stats?key=login&p=90,95,99
```

Returns count, errors and error rate, min, max, mean, standard deviation, throughput, and the requested percentiles (named `p90`, `p95`, `p99_5`…).

#### `perf/series` — statistics per time bucket

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Transaction name |
| `interval` | | Bucket width in seconds (e.g. `60`) |
| `p` | | Percentiles per bucket |
| `since` / `until` | | Window |

```
GET {BASE_URL}/perf/series?key=login&interval=60&p=95
```

#### `perf/assert` — pass/fail check for pipelines

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Transaction name |
| `<stat><op>=<n>` | * (one or more) | A check: a statistic name immediately followed by an operator, equals a number. Statistics: `count`, `errors`, `errorrate` (a fraction: `0.01` = 1%), `mean`, `min`, `max`, `stddev`, `sum`, `throughput`, `p<nn>` (e.g. `p95`, `p99_5`). Operators: `lt`, `le`, `gt`, `ge`, `eq` |
| `strict` | | Boolean; `true` = answer HTTP 412 (instead of 200) when any check fails, so a pipeline step fails on the status code alone |
| `since` / `until` | | Window |

```
GET {BASE_URL}/perf/assert?key=login&p95lt=800&meanlt=500&errorratelt=0.01
GET {BASE_URL}/perf/assert?key=login&p95lt=800&strict=true
```

Response: `pass` (true only if every check passed), `failed` (how many did not) and `checks` (each with its actual value, operator, threshold and `pass`). A key with no data always fails. Without `strict` the HTTP status is 200 either way, so load tools do not count it as a transport failure — read `pass`. Note the operators are `le`/`ge`, not `lte`/`gte`, and the average is `mean`, not `avg`; an unknown check name is a 400 whose `hint` lists the valid ones.

#### `perf/keys` — list transaction names

| Param | Required | Meaning |
|---|---|---|
| `prefix` | | Only keys starting with this |

```
GET {BASE_URL}/perf/keys?prefix=checkout
```

Returns `count`, `prefix` and `keys` `[{key, firstTime, lastTime}]`. `firstTime`/`lastTime` are the earliest and latest point times (a back-dated `time=` counts); without `time=`, `lastTime` here can lag by up to a minute — `perf/stats` is always exact.

#### `perf/clear` — delete all results for a key

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Transaction name |

```
GET {BASE_URL}/perf/clear?key=login
```

#### `perf/export` — download raw samples

| Param | Required | Meaning |
|---|---|---|
| `key` | * | Transaction name |
| `format` | | `csv` or `json` |
| `limit` | | Maximum samples |
| `since` / `until` | | Window |

```
GET {BASE_URL}/perf/export?key=login&format=csv
```

Only points recorded with `raw=true` are exported (they are kept 7 days); without them the export is empty and says so. `format=csv` returns `text/csv`.

---

### 3.10 `voice` — spoken utterances for voice interfaces

**Concept.** `voice/say` turns text into speech so a test can talk to a voice interface: a voice chatbot, a phone menu, a "press to talk" button. The audio is **stored by its content**: the whole request (words, tags, voice, engine, format) is hashed, the hash is the `ref`, and the audio is kept under it.

* **The same request always returns the same audio**, byte for byte, with `"cached": true`. Nothing is generated twice. A test that failed can be run again on exactly the sound it failed on, and a thousand virtual users saying the same words cost one synthesis.
* **`ref`** is the handle for one generated utterance. It is kept 30 days after its last use. **`key`** is a name YOU choose for audio that must be kept: pin a `ref` with `voice/save`, or upload a recording. These two words mean what they mean for mail attachments.
* **Tags** in square brackets direct the delivery: `[anxious] I've been waiting. [pause] Is the doctor late?`. Tags are never spoken.
* **The response says what really happened to each tag.** No AWS voice takes emotion as an instruction today, so an emotion tag is *approximated* (speaking rate, pitch, volume) or *inferred* from the wording, and a sound such as `[sighs]` is not audible at all. Read `tags[].rendered` before you assert on tone.

#### `voice/say` — text to audio (GET or POST)

| Param | Required | Meaning |
|---|---|---|
| `text` / `text64` | * | The words to say, with optional `[tags]`. Up to 3000 characters. Use `text64` if your client will not encode brackets, `+`, `&` or `#` |
| `emotion` | | One emotion word for the whole utterance: `anxious`, `angry`, `sad`, `relaxed`, `happy`, `excited`, `frustrated`, `upset`, `tired`, `confused`, `surprised`… Exactly the same as that tag at the very start of `text` |
| `voice` | | A voice name from `voice/voices`, or `random`. Default `Olivia` (Australian English) |
| `language` | | `en-GB`, or just `en`. Picks the voice when `voice` is not given; narrows `voice=random` |
| `engine` | | `neural` (default), `standard` or `generative` — see the table below |
| `format` | | `mp3` (default), `ogg`, `wav`, `pcm` (raw 16-bit mono), `mulaw`, `alaw` |
| `hz` | | Sample rate. `mp3`/`ogg`: 8000, 16000, 22050, 24000 (default 24000). `wav`/`pcm`: 8000 or 16000 (default 16000). `mulaw`/`alaw`: 8000 |
| `seed` | | With `voice=random`: the same seed picks the same voice every time |
| `strict` | | Boolean. `true` = refuse (422) if any tag would not be heard, instead of leaving it out |
| `url` | | Boolean. `true` = return a presigned `url` instead of base64 `data` |
| `view` / `download` | | Boolean. `true` = answer with a redirect to the audio itself (played / saved by a browser) |
| `play` | | Boolean. `true` = answer with a small web page that plays the audio by itself, with no player on screen |
| `ref` / `key` | | Instead of `text`: say a stored utterance again with another `voice`, `engine`, `format` or `hz` |

```
GET {BASE_URL}/voice/say?text=When%20is%20my%20next%20appointment%3F
GET {BASE_URL}/voice/say?text=I%20have%20been%20waiting%20forty%20minutes.&emotion=anxious&format=wav&hz=16000
GET {BASE_URL}/voice/say?text64=W2FueGlvdXNdIElzIHRoZSBkb2N0b3IgcnVubmluZyBsYXRlPyBbcGF1c2UgODAwbXNd&format=wav
GET {BASE_URL}/voice/say?text=Hello&view=true
GET {BASE_URL}/voice/say?text=Hello&play=true
GET {BASE_URL}/voice/say?text=Hello&voice=random&seed=run42
```

The third example is the base64 of `[anxious] Is the doctor running late? [pause 800ms]`. The fourth redirects straight to the audio, so that URL works as the `src` of an `<audio>` element (opened in a browser tab it shows the browser's player). The fifth returns a web page (`text/html`) that plays the audio from script, showing only the words: use it when a person should hear the utterance without a player appearing. A browser lets a page make sound only after the visitor has clicked something on the site, so when the browser refuses, the page shows one **Play** button; an automated Chromium started with `--autoplay-policy=no-user-gesture-required` always plays. Pass only one of `play`, `view`, `download`.

Response (200):
```json
{ "ref": "v7k2m9q4x1ab", "cached": false,
  "text": "I've been waiting forty minutes. Is the doctor running late?",
  "voice": "Olivia", "language": "en-AU", "engine": "neural",
  "format": "wav", "hz": 16000, "bytes": 148524, "durationMs": 4640, "characters": 60,
  "tags": [
    { "tag": "anxious", "kind": "emotion", "at": 0, "rendered": "approximated", "how": "rate 112%" },
    { "tag": "sighs", "kind": "sound", "at": 32, "rendered": "none", "how": "no AWS voice makes this sound yet; kept in the stored script" } ],
  "ssml": "<speak><prosody rate=\"112%\">I've been waiting forty minutes. Is the doctor running late?</prosody></speak>",
  "filename": "v7k2m9q4x1ab.wav", "contentType": "audio/wav", "createdTime": 1794614400,
  "getUrl": "{BASE_URL}/voice/get?ref=v7k2m9q4x1ab",
  "data": "UklGRiQ…" }
```

* `data` — the audio as base64. With `url=true` (or audio over 4 MB) you get `url` and `expiresInSeconds` instead.
* `text` — the words actually spoken, tags removed. **This is the transcript to expect** when you assert on what the system under test heard.
* `ref` — log it with the test run. `voice/get?ref=…` replays this exact audio.
* `cached` — `true` when it came from the store. The audio is identical either way.
* `durationMs` — how long the audio lasts. Use it to time "end of speech → first response".
* `tags[]` — one entry per tag: `rendered` is `native`, `approximated`, `inferred` or `none`, and `how` says what was done.
* `ssml` — exactly what the speech engine was asked to say.

**Tags.** Write a tag in square brackets where the delivery should change. A tag holds until the next tag of the same kind (emotion, pace or volume) or the end of the text; `[neutral]` clears all three. An unknown tag is accepted, never spoken, and reported as `rendered: "none"`. `voice/tags` returns the full list.

| Kind | Tags (examples) | `standard` | `neural` | `generative` |
|---|---|---|---|---|
| pause | `[pause]` (500 ms), `[short pause]`, `[long pause]`, `[pause 800ms]`, `[pause 1.5s]` | native | native | native |
| pace | `[slowly]`, `[quickly]`, `[rushed]`, `[drawn out]` | native | native | approximated (whole sentences) |
| volume | `[softly]`, `[loudly]`, `[shouts]` | native | native | approximated (whole sentences) |
| whisper | `[whispers]` | native | approximated (very quiet) | approximated (very quiet) |
| emotion | `[anxious]`, `[angry]`, `[sad]`, `[relaxed]`, `[excited]`, `[frustrated]`… | approximated (rate, pitch, volume) | approximated (rate, volume) | inferred from the wording |
| sound | `[sighs]`, `[laughs]`, `[clears throat]` | none | none | none |

**Which engine?** `neural` (default) sounds natural, is stable, and takes rate and volume. `standard` also takes pitch and a true whisper, so emotion tags are most audible, but it sounds synthetic. `generative` sounds the most human and reads emotion **from the words themselves** — write the worry into the sentence — but it cannot be directed, and its voices change when the engine is upgraded (which is why replay comes from the stored audio).

**POST.** The same names as a JSON body, plus two things a URL cannot carry:

```
POST {BASE_URL}/voice/say
{ "voice": "Olivia", "format": "wav", "hz": 16000, "strict": true,
  "pronounce": { "ACME": "ack me" },
  "segments": [
    { "text": "Hi, it's Jo from ACME.", "emotion": "relaxed" },
    { "pause": 600 },
    { "text": "Actually, I'm quite worried about my results.", "emotion": "anxious" } ] }
```

* `segments` — the utterance in pieces: `{"text", "emotion"}` or `{"pause": milliseconds}`. Each piece starts from the utterance's own `emotion` again (a tag inside a piece ends with that piece). Send `text` or `segments`, not both.
* `pronounce` — `{"word or phrase": "how to say it"}`, for names and acronyms the voice gets wrong. Whole words, any letter case.
* A POST body that is not JSON is taken as the text itself (a form body such as `text=Hello&voice=Amy` is read as fields).
* A `text` that is a complete `<speak>…</speak>` SSML document is passed to the engine exactly as written (no tags parsed, `emotion` and `pronounce` refused). Use it for `<say-as interpret-as="digits">` and other fine control.

Notes: in `wav` and `pcm`, a pause at the very start or end of the text is exact digital silence — end with `[pause 800ms]` when the system under test needs silence to notice you have stopped talking. To say numbers digit by digit without SSML, write them spaced: `4 8 3 9 2 1`. New synthesis is limited per day on each deployment: when the budget is used up `voice/say` returns 429 with `budgetExhausted: true` for anything not already stored, and stored audio keeps working.

#### `voice/get` — stored audio by ref or key (replay)

| Param | Required | Meaning |
|---|---|---|
| `ref` or `key` | * | `ref` from `voice/say`, or the `key` audio was saved under. One, not both |
| `url` | | Boolean. `true` = presigned `url` instead of `data` |
| `view` / `download` | | Boolean. `true` = redirect to the audio |
| `play` | | Boolean. `true` = a web page that plays the audio by itself, with no player |

```
GET {BASE_URL}/voice/get?ref=v7k2m9q4x1ab
GET {BASE_URL}/voice/get?key=anxious.waiting&url=true
GET {BASE_URL}/voice/get?key=anxious.waiting&play=true
```

Returns the same fields as `voice/say` (without `cached`), plus `source`: `generated` (found by `ref`) or `saved` (found by `key`, with `savedFrom`: `generated` or `upload`). Nothing is synthesised: these are the stored bytes. 404 when the ref has expired or the key does not exist.

#### `voice/save` — keep audio under a key (GET to pin, POST to upload)

| Param | Required | Meaning |
|---|---|---|
| `key` | * | The name to keep it under (overwrites) |
| `ref` | | Pin this generated utterance |
| `filename` | | For an upload: the recording's name, with its extension |
| POST body | | Instead of `ref`: the recording. Raw bytes with `Content-Type: application/octet-stream`, or the base64 of the file. Max 4096 KB |

```
GET  {BASE_URL}/voice/save?key=anxious.waiting&ref=v7k2m9q4x1ab
POST {BASE_URL}/voice/save?key=human.anxious&filename=take1.wav     (body = the WAV file)
```

Response (200): `key`, `saved: true`, `source` (`generated` or `upload`), `format`, `bytes`, `durationMs`, `filename`, `savedTime` and a ready-made `getUrl`. Saved audio never expires. Use it for a regression set of stimuli, and for **recordings of a real voice** — the only way to get real emotion into a test today. Anyone who knows a key can fetch it: save synthetic voices, or recordings of people who agreed.

#### `voice/delete` — delete saved audio

```
GET {BASE_URL}/voice/delete?key=anxious.waiting
```

Response (200): `{ "key": "anxious.waiting", "deleted": true }`. `deleted: false` if there was nothing there — not an error.

#### `voice/voices` — the voices, and what each engine can do

| Param | Required | Meaning |
|---|---|---|
| `language` | | Only voices speaking this language: `en-AU`, or `en` |
| `engine` | | Only voices on this engine |

```
GET {BASE_URL}/voice/voices?language=en&engine=neural
```

Returns `default` (the voice and engine used when you name none), `voices[]` (`voice`, `language`, `gender`, `engines`, `languages`) and `engines`: for each engine, how pauses, pace, volume, whisper, emotion and sounds are rendered. When an engine gains real emotion control, `engines.<engine>.emotion` becomes `native` — check it rather than assuming.

#### `voice/tags` — the tag vocabulary

```
GET {BASE_URL}/voice/tags
```

No parameters. Returns every known tag with its `kind`, the other words that mean the same (`also`), and how each engine renders it.

---

### 3.11 Discovery documents

| URL | What |
|---|---|
| `{BASE_URL}` | Service index (JSON) |
| `{BASE_URL}/<service>` | Actions for one service (JSON) |
| `{BASE_URL}/openapi.json` | OpenAPI 3.2.1: every parameter, response schema and error |
| `{BASE_URL}/openapi-3.0.json` | The same in OpenAPI 3.0.3, for tools that do not read 3.2 yet (lists/push and perf/assert free-form parameters appear as examples) |
| `{ROOT_URL}/.well-known/openapi.json` | OpenAPI 3.2.1 (well-known location) |
| `{BASE_URL}/llms.txt` | This file |
| `{ROOT_URL}/llms.txt`, `{ROOT_URL}/.well-known/llms.txt` | This file |
| `{ROOT_URL}/.well-known/api-catalog` | RFC 9727 API catalog |
| `{ROOT_URL}/.well-known/endpoints` | Every service, action, method and summary, plus these discovery documents (JSON) |
| `{ROOT_URL}/robots.txt` | Crawlers, AI assistants and AI agents are welcome everywhere |

If this file and the live `openapi.json` disagree, `openapi.json` wins.

---

## 4. Recipes (standard multi-step patterns)

### 4.1 Seed test data once, consume it from many parallel tests

```
1. Setup (once):
   GET /v1/lists/clear?key=accounts
   GET /v1/lists/push?key=accounts&user=u001&password=Pa55
   GET /v1/lists/push?key=accounts&user=u002&password=Pa55
   ...
   — or load them all at once (this replaces the queue, so no clear needed):
   POST /v1/lists/import?key=accounts&format=csv   with the CSV as the body
2. Each virtual user / test:
   GET /v1/lists/pop?key=accounts
   → read record.user and record.password
   → if exhausted is true: stop, or call lists/reset and pop again
     (or use lists/cyclepop, which goes round again by itself — recipe 4.14)
3. Teardown: GET /v1/lists/reset?key=accounts   (keep data for next run)
```

### 4.2 Receive an email verification link

```
1. GET /v1/mail/clear?address=run42@{MAIL_DOMAIN}
2. Register in your app using run42@{MAIL_DOMAIN}
3. GET /v1/mail/pop?address=run42@{MAIL_DOMAIN}&wait=20
   → repeat up to 3 times if exhausted is true
4. Extract the link from the body field with a regex
5. Open the link in your app
```

### 4.3 Receive an SMS one-time code

```
With a registered handset (+61400000000):
1. now = current epoch seconds
2. Trigger the SMS in your app to +61400000000
3. GET /v1/sms/get?number=%2B61400000000&since=<now>&wait=20
4. If found is true, extract the digits from body

With no handset (simulated):
1. GET /v1/sms/mocksend?number=%2B61411111111&code=123456
2. GET /v1/sms/get?number=%2B61411111111   → body contains 123456, mocked is true
```

### 4.4 Capture a webhook your app sends

```
1. Configure your app's callback URL: {BASE_URL}/webhook/receive?key=run42
2. GET /v1/webhook/clear?key=run42
3. now = current epoch seconds
4. Trigger the event in your app
5. GET /v1/webhook/list?key=run42&since=<now>&wait=20
6. Assert on the captured body
```

### 4.5 Log in with two-factor authentication

```
1. Once: GET /v1/totp/set?key=myapp&user=ann&secret=<base32 from enrolment>
2. Each login: GET /v1/totp/get?key=myapp&user=ann
   → if secondsRemaining < 5, wait and call again
3. Submit the code
```

### 4.6 Protect a shared resource in a parallel test

```
1. GET /v1/lock/acquire?key=adminConsole&wait=20
   → if locked is false: retry or skip
2. do the work (well under 60 s — the lock expires then)
3. finally: GET /v1/lock/release?ref=<ref from step 1>
```

### 4.7 Record timings and fail the build if too slow

```
During the test, per request:
   GET /v1/perf/add?key=checkout&value=<ms>&ok=<true|false>
After the test:
   GET /v1/perf/stats?key=checkout&p=95,99
   GET /v1/perf/assert?key=checkout&p95lt=1000&errorratelt=0.01
   → fail the pipeline if pass is false
   (or add strict=true and fail on HTTP 412)
```

### 4.8 Send an email with an attachment and check it was processed

```
1. GET /v1/mail/send?from=qa@{MAIL_DOMAIN}&to=<your app's inbound address>&subject=Claim&body=see%20attached&attachmentkb=512&filename=claim.pdf
   (the inbound address must be on this deployment's allow-list; a 403 lists what is allowed)
2. Wait for your app to create the case/record
3. Register it for cleanup: GET /v1/lists/cleanup/push?key=run42&value=<case id>&note=case
```

### 4.9 Load a large CSV of test data

```
1. GET /v1/lists/upload/url?key=customers&format=csv&size=<bytes>
   → keep url, fields, ref and next from the response
2. POST multipart/form-data to url: every entry of fields first, then the file as the LAST part named "file"
   → S3 answers 201
3. GET the next URL (= /v1/lists/import?key=customers&format=csv&ref=<ref>)
   → state "done": finished. state "queued" or "running": poll step 4
4. GET /v1/lists/import/status?key=customers&ref=<ref> until state is "done" or "failed"
5. Read warnings and loaded in the result, then GET /v1/lists/list?key=customers&limit=5 as a sanity check
Remember: the import REPLACES whatever the key held before.
```

### 4.10 Check the content of an emailed PDF

```
1. GET /v1/mail/pop?address=run42@{MAIL_DOMAIN}&wait=20   → attachments[0].ref
2. GET /v1/mail/attachments/find?ref=<ref>&left=Invoice%20number   → values[0] is the invoice number
3. GET /v1/mail/attachments/find?ref=<ref>&text=Total%20including%20GST%20%242%2C972.00   → count is 1
   (add &password=... for a protected PDF; on a surprise 0, look at mail/attachments/text?ref=<ref>&format=text)
```

### 4.11 Check the emailed PDF, then check the application serves the same PDF

```
1. Trigger the purchase in your app using run42@{MAIL_DOMAIN} as the customer's email.
2. GET /v1/mail/pop?address=run42@{MAIL_DOMAIN}&wait=20
   → attachments[0].ref (e.g. e1pv4cugg4) and attachments[0].filename
3. Check the content (one call per field; values[0] is the captured value):
   GET /v1/mail/attachments/find?ref=<ref>&left=Total%20including%20GST
   GET /v1/mail/attachments/find?ref=<ref>&text=<customer name>              → count is 1
   GET /v1/mail/attachments/find?ref=<ref>&text=<street address>            → count is 1
   (call GET /v1/mail/attachments/text?ref=<ref>&format=text first to see how the labels are laid out)
4. Open the order screen in the app and get its copy of the PDF. In the browser, run in that page:
     const pdf = await (await fetch('<the download link href>')).blob();
     await fetch('{BASE_URL}/mail/attachments/save?key=run42-order-pdf&filename=<attachments[0].filename>',
                 { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: pdf });
   (or POST the downloaded file with curl --data-binary, Content-Type: application/octet-stream)
5. GET /v1/mail/attachments/compare?ref=<ref>&otherkey=run42-order-pdf
   → same is true: it is the same document (sameBytes says whether it is also the very same file)
   → same is false: read differences
6. To show either file: {BASE_URL}/mail/attachments/get?ref=<ref>&view=true
                        {BASE_URL}/mail/attachments/get?key=run42-order-pdf&view=true
```

### 4.12 A test cycle: initiate once, run many independent scripts, tear down

Use this whenever more than one script, machine, virtual user or run shares the same data. The initiation script is the only thing that knows how the data was made; every other script needs nothing but `<project>`.

Two variables under `<project>` control the cycle:

| Variable | Set by | Meaning |
|---|---|---|
| `namespace` | initiation | The source of truth for this cycle's namespace `<ns>`. Scripts never hard-code `<ns>`; they read it here. |
| `setupReady` | initiation, teardown | `0` = initiation is running (or the cycle is being torn down): wait. `1` = every record the tests depend on exists: go. `failed` = initiation gave up: stop. |

The order of the steps below matters. Initiation sets `setupReady=0` **before** it changes `namespace`, and scripts read `namespace` only **after** they have seen `setupReady=1`, then check `setupReady` once more. That way a script can never combine the ready flag of the previous cycle with the namespace of a new, half-built one, even when every script is started at the same moment as initiation.

```
INITIATION (one script, run once per cycle):
   GET /v1/lists/vars/set?key=<project>&name=setupReady&value=0
      → FIRST: close the gate, so no script starts on a half-built cycle
   GET /v1/lists/codes
      → ns = rand10                                       (e.g. AN672Q1V2B)
   GET /v1/lists/vars/set?key=<project>&name=namespace&value=<ns>
      → the hand-off: every other script reads this once the gate opens
   create the data in your system (users, accounts, products…), and for each one:
   GET /v1/lists/push?key=<ns>_<kind>_<qualifier>&email=…&password=…&id=…
      → pools of created data, one pool per kind (and qualifier if it matters, e.g. _users_gold)
   GET /v1/lists/cleanup/push?key=<ns>&value=<id in your system>&note=<kind>
      → register everything that will need deleting at the end
   GET /v1/lists/counters/add?key=<ns>&name=seeded&increment=<n>
      → optional: record how much was created
   GET /v1/lists/list?key=<ns>_<kind>_<qualifier>&limit=1     for each pool the tests depend on
      → check count >= the minimum the test needs (e.g. 10); if not, create more or fail
   GET /v1/lists/vars/set?key=<project>&name=setupReady&value=1
      → LAST: open the gate, only when every minimum is met
   if initiation cannot finish:
   GET /v1/lists/vars/set?key=<project>&name=setupReady&value=failed
      → waiting scripts stop at once instead of waiting until they time out

EVERY TEST SCRIPT (any machine, started with initiation or at any time later, any number in parallel):
   wait for the gate — in the script's setup section (e.g. vuser_init), not in the measured iterations:
   loop: GET /v1/lists/vars/get?key=<project>&name=setupReady&equals=1&wait=20
      → the server holds each call up to 20 s and answers the moment the flag changes
      → matched true: continue below
      → value "failed": stop with a clear message
      → otherwise (value "0", or exists false because initiation has not started yet): call again
        at once — no sleep is needed, the wait is on the server
      → still not matched after the maximum wait (allow about twice the normal initiation time,
        e.g. 15 minutes): stop with a clear message; never wait forever
   GET /v1/lists/vars/get?key=<project>&name=namespace
      → ns
   GET /v1/lists/vars/get?key=<project>&name=setupReady&equals=1
      → matched true: the gate and the namespace belong to the same cycle; go on
      → matched false: a new cycle started in between — go back to the wait loop
   GET /v1/lists/random?key=<ns>_<kind>_<qualifier>
      → a shared identity that many scripts may use at once (non-consuming)
   GET /v1/lists/pop?key=<ns>_<kind>
      → one-use data that must not be handed to two scripts (consuming); check exhausted
   GET /v1/lists/counters/add?key=<ns>&name=<event>
      → cycle-wide tallies that add up correctly across all load generators
   GET /v1/lock/acquire?key=<ns>_lock_<item id>&wait=0
      → when parallel users must not touch the same record at the same time
      → locked true: do the work (well under 60 s), then GET /v1/lock/release?ref=<ref>
      → locked false: pick a different record, or retry with wait=5; never proceed unlocked
   when the script creates something new that must be removed later:
   GET /v1/lists/cleanup/push?key=<ns>&value=<id>&note=<kind>

TEARDOWN (one script, once per cycle):
   GET /v1/lists/vars/set?key=<project>&name=setupReady&value=0
      → FIRST: close the gate, so no script starts on data that is being deleted
   GET /v1/lists/vars/get?key=<project>&name=namespace
      → ns of the cycle being torn down
   loop: GET /v1/lists/cleanup/pop?key=<ns>
      → exhausted false: delete `value` in your system, then loop
      → exhausted true: done
   GET /v1/lists/counters/get?key=<ns>&name=<event>   for each tally you want in the report
   GET /v1/lists/vars/set?key=<project>&name=previous_namespace&value=<ns>
      → keeps a pointer to the finished cycle for debugging
   optional: GET /v1/lists/clear?key=<ns>_<kind>_<qualifier> for each pool, if the data must not linger
```

Example — a load test needs at least 10 logins before the measured run may start (control key `regression-7QK2`):
```
initiation:  {BASE_URL}/lists/vars/set?key=regression-7QK2&name=setupReady&value=0
             {BASE_URL}/lists/codes                       → rand10 = RJX3K0UD7P
             {BASE_URL}/lists/vars/set?key=regression-7QK2&name=namespace&value=RJX3K0UD7P
             10 × {BASE_URL}/lists/push?key=RJX3K0UD7P_logins&username=…&password=…
             {BASE_URL}/lists/list?key=RJX3K0UD7P_logins&limit=1    → count 10
             {BASE_URL}/lists/vars/set?key=regression-7QK2&name=setupReady&value=1
each script: {BASE_URL}/lists/vars/get?key=regression-7QK2&name=setupReady&equals=1&wait=20
                 → repeat until matched is true (stop if value is "failed")
             {BASE_URL}/lists/vars/get?key=regression-7QK2&name=namespace    → RJX3K0UD7P
             {BASE_URL}/lists/vars/get?key=regression-7QK2&name=setupReady&equals=1   matched true → start
             each iteration: {BASE_URL}/lists/cyclepop?key=RJX3K0UD7P_logins
```

Rules the scripts must follow:
* The only shared secret is `<project>`. Hard-code it; never hard-code `<ns>`. `namespace` under `<project>` is the single source of truth for the current cycle.
* Only initiation and teardown write `namespace` and `setupReady`. Test scripts only read them.
* `setupReady=1` is a promise that the minimum data exists. Set it last, after checking the pool counts, never straight after creating the namespace.
* Wait for the gate once, in the script's setup, before timing starts, with `equals=1&wait=20` in a loop. Do not check it in every iteration, and always have a maximum wait.
* `vars/get` returns `value` as a string: compare with `"1"`, not the number 1.
* Run one cycle at a time per `<project>`. Two initiation scripts running at once would overwrite each other's `namespace`; use a different `<project>` for each independent environment or team (e.g. `verdant-uat-7QK2`, `verdant-perf-7QK2`).
* A script that creates data registers it with `cleanup/push` in the same breath, so a crash later still leaves a record of what to delete.
* Use `random` for identities many scripts share; use `pop` for anything that is wrong to use twice; use `cyclepop` for reusable data that must not be in use by two scripts at once (recipe 4.14). Recipe 4.13 covers data that is created rarely but consumed often.
* Counters are the only safe way to total events across load generators; do not sum local counts.

### 4.13 Data that is created rarely but consumed often: prefer pop, fall back to random

Some entities are created by a test script now and then (a purchase, a booking, an appointment, a support case) but read or acted on far more often. Two scripts popping the same queue will drain it quickly, and then every later script would get `exhausted`.

Pattern:
```
   GET /v1/lists/pop?key=<ns>_<kind>
      → exhausted false: use record (a fresh, never-used one)
      → exhausted true:
           GET /v1/lists/random?key=<ns>_<kind>
              → empty false: use record (a previously used one, shared)
              → empty true: create one in your system, lists/push it, lists/cleanup/push its id, use it
   when the script creates a new <kind>:
   GET /v1/lists/push?key=<ns>_<kind>&id=…&…
      → it goes to the end of the queue, so the next pop anywhere will hand it out fresh
```

When this is and is not safe:
* **Safe** when reuse is harmless: the entity can be viewed, updated or acted on more than once (an account that can place another order, a booking many users may look at).
* **Not safe** when the entity is consumed by its nature: a single-use voucher, a one-time invite token, an appointment slot that becomes unavailable once taken. For those, do NOT fall back to `random`; on `exhausted` create a new one or fail the step clearly.

Write the choice into the script as a comment, with the reason, so that whoever debugs the run later knows the fallback is intentional, e.g. `// pop then random: bookings tolerate reuse; see llms.txt 4.13`.

### 4.14 Reusable data that must not be in use by two scripts at once: cyclepop

The situation: a fixed pool of test data that may be used again and again, but only by one script at a time. Typical examples:
* test logins — two sessions of the same user log each other out, share a basket, or trip "already signed in" checks;
* accounts whose balance, limit or status a test changes and then checks;
* phone numbers, mailboxes, devices or licensed seats that one script must have to itself while it runs.

What you want is `pop` (nobody else is handed this record while I have it) with an automatic reset when the pool runs out, so long, soak and endurance runs never stop on `exhausted`. That is `lists/cyclepop`.

Choose by what the data tolerates:

| The data… | Use | When the pool is used up |
|---|---|---|
| may be used by many scripts at the same time (a shared read-only identity, a product to view) | `lists/random` | never — it is not consumed |
| may be reused, but not by two scripts at the same time | `lists/cyclepop` | it goes round again by itself |
| must be used at most once, ever (a voucher, a one-time invite, an appointment slot, a registration email address) | `lists/pop` | `exhausted: true` — create more or stop; never reset |

**If a record may only be used once, use `pop`, not `cyclepop`.** cyclepop would quietly hand the used record out again on the next pass.

Pattern:
```
INITIATION (once per test cycle — see recipe 4.12 for the namespace hand-off):
   POST /v1/lists/import?key=<ns>_logins&format=csv        with one row per login (N rows)
      — or lists/push each one
   N must be larger than the number of records that can be in use at the same moment (see "Sizing")

EVERY TEST SCRIPT / VIRTUAL USER, each iteration:
   GET /v1/lists/cyclepop?key=<ns>_logins
      → exhausted true: the pool is empty — initiation has not run; stop with a clear message
      → otherwise: use record.username / record.password for this iteration
      → pass > 1 only means the pool has gone round; carry on
   do the work, then simply take the next record on the next iteration (nothing to release)

OPTIONAL HARD GUARANTEE (when the pool cannot be sized safely, and a script holds a record for under 60 s):
   GET /v1/lock/acquire?key=<ns>_login_<record.username>&wait=0
      → locked true: use the record; finally GET /v1/lock/release?ref=<ref>
      → locked false: someone still has it — call cyclepop again for the next record
```

Sizing — the one rule that makes it safe. cyclepop guarantees order, not exclusivity: a record comes round again after N more calls, whether or not the script that had it has finished. It is safe when the pool is bigger than the number of records in use at once:
* **Simple rule:** N > the peak number of scripts or virtual users that hold a record at the same time. 50 concurrent virtual users → at least 51 records; allow headroom.
* **When iteration times vary:** N > concurrent users × (slowest iteration ÷ typical iteration). 50 users, iterations usually 30 s but up to 90 s → N > 150.
* A script that keeps a record across several iterations counts once per record it holds.
* An undersized pool shows up as two scripts on the same record. If that would be hard to notice, add the lock guard above.

Rules:
* One pool per kind of data, and per qualifier if it matters (`<ns>_logins_admin`, `<ns>_logins_customer`).
* Use either `pop` or `cyclepop` on a key, never both — they share one pointer.
* `cyclepop` hands out the same row; it does not restore your system. If an iteration changes the record's state (balance, password, status), either make the iteration put it back, or have the next user of the record expect whatever state it finds.
* No reset is needed between passes. Call `lists/reset` at the start of a run only if you want every run to begin at record 1 (repeatable runs).
* Records pushed during a run lengthen the cycle from the next call — a safe way to grow an undersized pool without stopping the test.

### 4.15 Speak to a voice interface, check what it heard, replay on failure

```python
import base64, requests
r = requests.get("{BASE_URL}/voice/say", params={
    "text": "I've been waiting forty minutes. Is the doctor running late? [pause 800ms]",
    "emotion": "anxious", "format": "wav", "hz": 16000}).json()
open("utterance.wav", "wb").write(base64.b64decode(r["data"]))
print("stimulus", r["ref"])            # log the ref with the run

# Browser test: start Chromium with a fake microphone that plays the file once
#   --use-fake-ui-for-media-stream --use-fake-device-for-media-stream
#   --use-file-for-fake-audio-capture=utterance.wav%noloop
# API test: send the bytes to the voice stream (for raw samples ask for format=pcm).

# ... press the talk button / open the stream, wait for the reply ...
assert r["text"].lower() in transcript_shown_by_the_app.lower()
```

* Assert on `r["text"]` (the words without the tags), not on what you sent.
* Before asserting that the application noticed the *tone*, check how the tone was produced: `[t["rendered"] for t in r["tags"]]`. `approximated` is a nudge to rate and volume, not an acted voice. For a tone test, upload a real recording with `voice/save` and play that.
* To re-run a failure on the identical sound: `GET {BASE_URL}/voice/get?ref=<the logged ref>`. To keep it for the regression set: `GET {BASE_URL}/voice/save?key=<name>&ref=<ref>`.

### 4.16 The same utterances for many virtual users

Generate each utterance once, in the initiation script of a test cycle (recipe 4.12), and hand the refs out as test data. Virtual users then only ever read stored audio.

```
INITIATION (once per cycle)
  for each utterance:
    GET /v1/voice/say?text=<words>&format=wav&hz=16000&url=true      → ref
    GET /v1/lists/push?key=<ns>_utterances&ref=<ref>&text=<words>
EACH VIRTUAL USER
  GET /v1/lists/random?key=<ns>_utterances                            → record.ref, record.text
  GET /v1/voice/get?ref=<record.ref>&url=true                         → url (fetch the audio from it)
```

For variety, generate each line with several voices (`voice=random&seed=1`, `seed=2`, …): each is a different `ref`, and each is repeatable.

---

## 5. Errors and troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 404 with a list of actions in the body | Wrong action name | Use one of the listed actions |
| 404 on `vars/get`, `totp/get`, `attachments/get` | The named thing does not exist | Check `key`/`name`/`user`/`ref`; create it first |
| 400 `error` mentions a parameter | Missing or malformed parameter | Read `hint`; check spelling; check required `*` params |
| 400 about `key` | Invalid characters, or starts with the wrong character | Letters, digits, `. _ - : @ + =` only; start with letter/digit/`+`; `+` as `%2B` |
| 403 from `mail/send` | `from` not at `{MAIL_DOMAIN}`, or `to` not allowed | Send from `@{MAIL_DOMAIN}`; send to `@{MAIL_DOMAIN}` or an allow-listed address (the body lists them) |
| 403 from `sms/send` | Number is not a registered handset | Use the `mockUrl` in the response (`sms/mocksend`) |
| 429 from `sms/send` or `sms/mocksend` | Sent again too soon to the same number | Wait `retryAfterSeconds` |
| 422 `passwordRequired` / `passwordIncorrect` | Protected PDF | Pass `password` or `password64` |
| 412 from `perf/assert` | `strict=true` and a check failed | Read `checks` to see which |
| 422 from `voice/say` | `strict=true` and a tag cannot be rendered on this engine | Read `tags`; remove them, drop `strict`, or change `engine` |
| 429 from `voice/say` with `budgetExhausted` | The deployment's daily synthesis budget is used up | Reuse stored audio (`voice/get`, or the identical `voice/say` request); new text works again after `retryAfterSeconds` |
| `voice/say` returns the tag words in `text`, or speaks them | The brackets did not survive the URL | Send the text as `text64`, or use `emotion=` |
| 400 from `voice/say` naming a voice and `allowed` engines | That voice does not exist on the engine asked for | Pick an `allowed` engine, or another `voice` (`voice/voices?engine=…`) |
| The `play=true` page shows a Play button instead of playing | The browser blocks sound until the visitor clicks something on the site | Click Play once; in automation start Chromium with `--autoplay-policy=no-user-gesture-required` |
| 404 from `voice/get?ref=…` | The utterance expired (30 days after last use) | Call `voice/say` again; pin what must be kept with `voice/save` |
| 400 from `perf/assert` naming a check | Unknown statistic or operator (e.g. `avglt`, `p95lte`) | Use `mean`, and `lt`/`le`/`gt`/`ge`/`eq` |
| 429 with `gatewayError: THROTTLED` | Too many requests | Back off and retry |
| 504 / timeout | Gateway 29 s limit | Keep `wait` ≤ 20 (≤ 25 for import) |
| 200 but `exhausted: true` (or `empty: true` from `random`, `found: false` from `sms/get`) | Queue/inbox empty or fully consumed | Push/receive more, or `reset`; for reusable data use `cyclepop` (recipe 4.14) |
| Two scripts end up on the same record with `cyclepop` | The pool is smaller than the number of records in use at once | Push more records (takes effect immediately) or add the lock guard — recipe 4.14 |
| `pop` says `exhausted` on a key that has records | `cyclepop` has been used on the same key (shared pointer) | Use one or the other on a key; `lists/reset` to start again |
| 400 `cannot be a record field` with a `reserved` list | Pushed a field named `key`, `index` or `createdTime` | Rename the field |
| 400 `'delimiter' must be comma, semicolon, tab or pipe` | Sent the character (`;`) instead of the word | Use `delimiter=semicolon` |
| 400 `Send the file as the request body OR pass ref, not both` | Import called with a body and a `ref` | Send one or the other |
| 404 `No csv upload '<ref>' for key` | Ref used with a different key or format, or older than 90 days | Use the ref with the key and format it was issued for |
| 422 `No records found. The key was left unchanged.` | CSV had a header but no rows, or JSON was empty | Fix the file; the queue is untouched |
| Import loaded fewer rows than expected | Rows skipped or the file was treated as one object | Read `skipped`, `warnings` and `shape` in the import response |
| Records vanished after an import | Import replaces the whole queue | Use `lists/push` to add, or import into a different key |
| S3 POST not 201 | Form parts out of order, `file` not last, or presigned URL older than 15 min | Fields first, file last; request a fresh `upload/url` |
| 200 but `locked: false` | Lock held by someone else | Retry with `wait` |
| Value arrives corrupted / with spaces | `+`, `&`, `%`, `#` in a query value | Use the `64` parameter or URL-encode |
| Search in attachment finds nothing | Pattern chars mangled by the URL, or the text is laid out differently | Use `regex64` / `text64` / `left64`; look at `mail/attachments/text` |
| 400 `The body is not base64` from `mail/attachments/save` | Raw file bytes sent with `Content-Type: application/pdf` (or `text/plain`) | Send raw bytes with `Content-Type: application/octet-stream`, or base64 the file |
| Browser asks for the PDF's password after `get?view=true` | The PDF is protected | Add `&password=<password>`: the API serves an unlocked copy |
| Saved PDF downloads as a nameless binary / `view=true` saves instead of opening | Saved without `filename` | Save again with `filename=<name>.pdf` |
| `compare` says `same: true` but `sameBytes: false` | The PDF was generated again (new creation date / IDs) | Normal: same content. Assert `sameBytes` only if it must be the very same file |
| `mail/pop` returns the same message twice | The second one has `repeat: true` (grace re-offer) | Ignore `repeat: true`, or pass `grace=0` |

---

## 6. Old → v1 name mapping (for porting existing scripts)

| Old | v1 |
|---|---|
| `/data/new` | `lists/push` |
| `/data/pop` | `lists/pop` |
| `/data/get` | `lists/random` |
| `/data/list` | `lists/list` (variables now in `lists/vars/list`) |
| `/data/reset` | `lists/reset` |
| `/data/clear` | `lists/clear` |
| `/data/random` | `lists/codes` |
| `/data/savevariable` / `/data/getvariable` | `lists/vars/set` / `lists/vars/get` |
| `/data/updatecount` / `/data/getcount` | `lists/counters/add` / `lists/counters/get` |
| `/data/savecleanup` / `/data/getcleanup` / `/data/listcleanup` | `lists/cleanup/push` / `pop` / `list` |
| `/data/uploadcsv`, `/data/uploadjson` | `lists/upload?format=` |
| `/data/uploadurl` | `lists/upload/url` |
| `/data/importcsv`, `/data/importjson` | `lists/import?format=` |
| `/data/importstatus` | `lists/import/status` |
| `/name/get`, `/name/regions` | `names/get`, `names/regions` |
| `/lock/lock` | `lock/acquire` |
| `/lock/release?lockid=` | `lock/release?ref=` |
| `/email/pop?email=` | `mail/pop?address=` |
| `/email/reset`, `/email/clear` | `mail/reset`, `mail/clear` |
| `/email/send` (`fromaddress`, `toaddress`, `message`, `attachmentsize`, `name`/`attachmentname`, `injectstart`/`injectmiddle`/`injectend`) | `mail/send` (`from`, `to`, `body`, `attachmentkb`, `filename`, `prepend64`/`overwrite64`/`append64`) |
| `/email/saveattachment` | `mail/attachments/save` |
| `/email/getattachment`, `/email/getreceivedattachment` | `mail/attachments/get?key=` / `?ref=` |
| `/email/deleteattachment` | `mail/attachments/delete` |
| `/email/findinattachment` (`search`, `leftboundary`/`lb`, `contextchars`, `maxresults`) | `mail/attachments/find` (`text`, `left`, `context`, `limit`) |
| `/email/upload`, `/email/uploadurl` | `mail/attachments/upload`, `mail/attachments/upload/url` |
| `POST /sms/save?service=` | `POST sms/receive?number=` (device key required) |
| `/sms/latest?service=` | `sms/get?number=` |
| `/sms/recent` | `sms/list` |
| `/sms/send?service=&message=` | `sms/send?number=&body=` |
| `POST /webhook/webhook` | `webhook/receive` (any method) |
| `/webhook/list`, `/webhook/clear` | `webhook/list`, `webhook/clear` |
| `/totp/savesecret`, `/totp/getcode` | `totp/set`, `totp/get` |
| `/qr/get`, `/qr/svg`, `/qr/show` | `qr/get?format=json` / `svg` / `html` |
| `/perf/add` (`ts`) | `perf/add` (`time`) |
| `/perf/stats` (`from`, `to`) | `perf/stats` (`since`, `until`) |
| `/perf/series`, `/perf/assert`, `/perf/keys`, `/perf/clear`, `/perf/export` | same names |
| API-key authorizer | removed — no authentication in v1 |

---

## 7. Glossary

* **Namespace** — the `key` (or `address` for mail) that keeps your data separate from everyone else's. Choose something unique to your project or run, e.g. `acme-checkout-run42`.
* **Consuming** — an action that hands an item out once and moves on (`pop`). `cyclepop` hands each item out once per pass, then goes round again. Non-consuming actions (`random`, `get`, `list`) can be repeated.
* **Pointer** — the position in a queue that `pop` and `cyclepop` read next (`nextPop` in `lists/list`). `reset` moves it back to 1; `clear` deletes the queue; `import` replaces the queue and resets the pointer.
* **Ref** — an opaque handle issued by the API (an import ref looks like `20261010T212514Z-pnnwps`, an utterance ref like `v7k2m9q4x1ab`). Copy it exactly; never construct one. Import refs are tied to one key and one format.
* **Utterance** — one piece of generated speech from `voice/say`, stored under its `ref`. The same request is always the same utterance.
* **Tag** — a direction in square brackets inside `voice/say` text, e.g. `[anxious]` or `[pause]`. Never spoken.
* **Registered handset** — a real phone running the capture app, able to receive real texts and post them to `sms/receive`.
* **Mocked** — an SMS that was simulated with `sms/mocksend` rather than received by a handset.
* **Epoch seconds** — whole seconds since 1970-01-01 UTC. `1760054400` is 2025-10-10 00:00:00 UTC. Not milliseconds.
* **Presigned POST** — a one-time S3 upload form returned by an `upload/url` action, for files too large to send in a request body.
* **Base64 (`64` suffix)** — safe encoding for values that would be mangled in a URL.
