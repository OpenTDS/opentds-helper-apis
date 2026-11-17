# Test Helper API v1 — conventions

These rules apply to every v1 endpoint. They exist so that a person or an LLM
who has learned one service can guess the shape of any other.

## 1. URLs

```
https://api.<domain>/v1/<service>/<action>
https://api.<domain>/v1/<service>/<resource>/<action>
```

* Everything is lower case. Path segments are single words — no hyphens,
  underscores or camelCase.
* Services: `lists`, `names`, `lock`, `mail`, `sms`, `webhook`, `totp`, `qr`, `perf`, `voice`.
* `GET /v1` returns an index of services; `GET /v1/<service>` returns that
  service's actions. An unknown action is a 404 whose body lists the valid ones.
* The version lives in the path. A future v2 is a separate API with its own
  storage, mapped at `/v2`; v1 URLs never change meaning.

## 2. Methods

* **Every endpoint answers GET.** An LLM can call the whole API with nothing
  but URLs.
* Endpoints that take a request body (`lists/import`, `mail/send`,
  `mail/attachments/save`, `voice/say`, `voice/save`) also accept POST; the
  query-string parameters are the same. (`voice/say` also takes them as fields
  of a JSON body, where it adds `segments` and `pronounce`.)
* `webhook/receive` accepts any method, because it is the target a system
  under test calls.
* Other endpoints ignore the method, so a POST with query parameters behaves
  the same as the GET. `OPTIONS` returns 204 with CORS headers.
* No authentication for callers. CORS is open (`Access-Control-Allow-Origin: *`). The one
  exception is `sms/receive`, which only capture handsets call: it requires the
  `X-Device-Key` header, so stored real texts are known to come from configured devices.

## 3. Query parameters

* Names are lower case, single words, and **matched case-insensitively**
  (`Key=` works the same as `key=`) on every service.
* One name per concept — no aliases.
* A parameter whose value is base64 ends in `64` (`text64`, `regex64`,
  `left64`, `prepend64`). Standard or URL-safe alphabet, padding optional.
  Use these for values containing `+ & % #`, which do not survive a query
  string.
* Booleans accept `true/false`, `1/0`, `yes/no`, `on/off`.
* Out-of-range numbers are clamped, not rejected, where a sensible clamp exists
  (`wait`, `scale`, `limit`, `context`). Malformed values are a 400.

### Shared parameter vocabulary

| Parameter | Meaning everywhere it appears |
|---|---|
| `key` | Caller-chosen namespace or item key. 1–128 characters: letters, digits and `. _ - : @ + =`, starting with a letter, digit or `+`. Case-sensitive. (`+` must be sent as `%2B` — a bare `+` in a query string arrives as a space.) |
| `address` | An email address (the mail catcher's namespace). Case-insensitive. |
| `name` | A named item within a key (a variable, a counter). |
| `user` | A user within a key (TOTP). |
| `ref` | A handle the API issued and you pass back: an import, a received attachment, a lock, a generated utterance. |
| `value` | The value being stored. |
| `text` / `text64` | Literal text (QR payload, search term, words to speak). |
| `wait` | Seconds to hold the request open for something to happen, 0–20 (import: 0–25). 0 = return immediately. |
| `since` / `until` | Time window, UTC epoch seconds. |
| `limit` | Maximum items to return. |
| `start` | First index to return (1-based). |
| `format` | Output or input format (`json`, `csv`, `svg`, `html`; for `voice`, the audio: `mp3`, `wav`, ...). |
| `seed` | Makes a random result repeatable. |
| `url` / `view` / `download` | On an action that returns a file (`mail/attachments/get`, `voice/say`, `voice/get`): a presigned URL instead of inline base64, or a 302 to the file itself (shown / saved by a browser). `voice` adds `play`: an HTML page that plays the audio from script, with no player. |

## 4. Action vocabulary

The same verb means the same thing in every service.

| Action | Meaning |
|---|---|
| `push` | Append an item to the end of a queue. |
| `pop` | Take the next item and advance the pointer (consuming). |
| `random` | Return a random item without consuming it. |
| `get` | Read one thing. No side effects. |
| `set` | Create or overwrite a named value. |
| `add` | Add to an accumulator (counter increment, performance data point). |
| `list` | Read many things, with totals. |
| `reset` | Rewind a pointer. Data is kept. |
| `clear` | Delete every item of that collection under the namespace (`lists/clear` records, `mail/clear` messages, `webhook/clear` events, `perf/clear` results). |
| `delete` | Delete one item. |
| `acquire` / `release` | Take / give back a lock. |
| `send` / `receive` | Outbound / inbound message. |
| `save` | Store a file. |
| `say` | Turn text into spoken audio. The same request always returns the same stored audio. |
| `find` | Search inside stored content. |
| `import` / `status` | Bulk load, and its progress. |
| `upload` | Browser page for humans (HTML). `upload/url` returns a presigned S3 POST. |

Read-only nouns (`codes`, `regions`, `stats`, `series`, `keys`, `export`,
`voices`, `tags`) return the thing named.

## 5. Responses

* JSON (`application/json; charset=utf-8`) unless the action is explicitly a
  page (`text/html`), an image (`image/svg+xml`) or an export (`text/csv`).
* **Field names are camelCase.** User data you stored (list record fields,
  webhook bodies) is returned exactly as stored.
* Every response echoes the namespace it acted on (`key` or `address`).
* Times are UTC epoch **seconds**, in fields ending `Time` (`savedTime`,
  `createdTime`, `receivedTime`). Other units are in the name: `waitedMs`,
  `ageSeconds`, `timestampMs`, `bytes`, `sizeKb`.
* An empty queue is **not** an error: `pop` returns 200 with
  `exhausted: true` and a null item, so load-test tools do not count it as a
  failed request.
* A named thing that does not exist (`vars/get`, `totp/get`,
  `mail/attachments/get`) is a 404.
* A password-protected PDF opened without the right `password` is a 422
  (`passwordRequired` / `passwordIncorrect`).
* Contention is not an error: `lock/acquire` returns 200 with
  `locked: false`.
* A request that asked to be refused rather than degraded is a 4xx:
  `perf/assert` with `strict=true` is a 412 when a check fails; `voice/say`
  with `strict=true` is a 422 when a tag would not be heard. Without `strict`
  both are 200 and the body says what happened (`pass`, `tags[].rendered`).
* Over a deployment's spend limit is a 429 with a named flag
  (`voice/say`: `budgetExhausted`); what is already stored still works.
* Errors always have the shape

  ```json
  { "error": "human-readable message", "status": 400 }
  ```

  with extra fields when they help (`hint`, `allowed`, `received`). The only
  exception is an error raised by API Gateway itself before the request reaches
  a service (throttling 429, 504 timeout): `{"error": "...", "gatewayError": "THROTTLED"}`.
* `Cache-Control: no-store` on everything except discovery documents.

## 6. Limits

* API Gateway gives up at 29 s, so `wait` is capped at 20 s (25 s for
  `lists/import`).
* A synchronous request body is capped at about 4 MB raw (6 MB Lambda payload,
  base64 overhead). Larger files go straight to S3 via an `upload/url`
  presigned POST.
* One `voice/say` utterance is at most 3000 characters (the speech engine's
  limit for one call).
