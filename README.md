# OpenTDS Helper APIs (Test Helper API v1)

> **Open source (MIT), not accepting contributions at this time.** Fork freely; see [CONTRIBUTING.md](CONTRIBUTING.md).

**Status (v1.3.0):** deployed from a fresh clone into a brand-new AWS account and
checked end to end with [`scripts/test-deployment.ps1`](scripts/test-deployment.ps1);
the offline test suite runs on every push. Deploying from GitHub Actions
(`scripts/setup-github-deploy.ps1`) is new and has not yet been run against a real
account. See [CHANGELOG.md](CHANGELOG.md).

Public helper endpoints that make the hard parts of end-to-end and load test
automation easy to script — especially by an LLM. Every action is a plain
HTTPS GET.

```
https://api.<domain>/v1/<service>/<action>?param=value
```

| Service | What it's for |
|---|---|
| `lists` | Shared test-data queues (`push`/`pop`/`random`), CSV/JSON bulk import, run variables, atomic counters, a cleanup register |
| `names` | Realistic female/male personas for 15 regions, repeatable with `seed` |
| `lock` | 60-second mutual exclusion between concurrent scripts |
| `mail` | Catch inbound email (`pop` with `wait`), send email via SES, store attachments, search inside PDF/Office attachments |
| `sms` | Read texts caught by registered capture handsets, send real texts to those handsets, or simulate texts to any other number (`mocksend`) |
| `webhook` | Capture webhooks from the system under test and wait for them |
| `totp` | RFC 6238 codes for MFA-protected test accounts |
| `qr` | QR codes as JSON (base64 PNG), SVG or an HTML page |
| `perf` | Record response times, get percentiles and time series, gate a pipeline |
| `recon` | Reconcile what a test submitted (`expect`) with what it saw processed (`confirm`), matched by count per key (`suffix=N` for full values against masked ones): outstanding / unexpected records, short / over / reconciled-by-count groups with their candidates, processing lag, and a gate |
| `voice` | Both halves of a spoken conversation, for testing voice interfaces. Say: text with `[tags]` for emotion, pace and pauses becomes audio (Amazon Polly), stored by content so every request replays byte for byte. Listen: audio becomes words (Amazon Transcribe), with `find` and `compare` to assert on what was said |

Start here once deployed:

* `https://<domain>/` (and `https://api.<domain>/`) — the landing page for
  people, generated from `docs/site/index.html` plus the service list in the
  OpenAPI spec. Browsers get HTML; scripts asking for JSON get the index.

* `https://api.<domain>/v1/llms.txt` — the guide to point an AI assistant at
* `https://api.<domain>/v1/openapi.json` — OpenAPI 3.2.1, with response schemas
* `https://api.<domain>/v1/openapi-3.0.json` — the same in OpenAPI 3.0.3, for tools that
  don't read 3.2 yet (Postman: Import → Link)
* `https://api.<domain>/v1` — JSON index of every service and action

## Repository layout

```
template.yaml            CloudFormation: domain, APIs, Lambdas, tables, buckets, SES
deploy.ps1               Package + deploy with the full parameter set and safety checks
terraform/               The same stack as Terraform (alternative to the two above) - see terraform/README.md
devweb/                  DevWeb regression/performance suite: init + one script per service + multi - see devweb/README.md
src/                     ONE Lambda code package shared by every function
  common.py / common.js  Request parsing, routing, errors (Python / Node)
  lists*.js              lists (Node.js)
  mail*.py               mail (API, inbox, send, attachments, extraction, S3 receive)
  webhook.py totp.py lock.py names.py names_data.py qr.py perf.py recon.py
  voice*.py              voice (API, tags -> script -> SSML, audio helpers, S3/Polly store, play page;
                         voice_listen + voice_words: Transcribe jobs, and finding/comparing what was heard)
  discovery.py           /v1 index, openapi.json, openapi-3.0.json, llms.txt, /.well-known/*
  health.py              health/get (the build) and health/check (probes every service end to end)
  trace_view.py          trace/get: an X-Ray trace of this stack, cleaned for the public (JSON or a page)
  version.json           The API contract version (semver) - see "Build number and health check"
  discovery_openapi.json discovery_openapi30.json discovery_llms.md   <- BUILT, do not edit (see below)
  ses_activator.py       Custom resource that activates the SES receipt rule set
docs/
  CONVENTIONS.md         The naming and behaviour rules every endpoint follows
  ENDPOINTS.md           Every v1 endpoint, with the pre-v1 name it replaces
  openapi/<service>.json OpenAPI fragments (source, written in 3.0 style; the build
                         publishes 3.2.1 and a 3.0.3 copy)
  guide/_intro.md, guide/<service>.md   LLM guide sections (source)
  notes/<service>.md     Integration notes: sizing, IAM, storage, behaviour changes
tools/build_docs.py      Builds the discovery documents from docs/
tests/                   pytest (moto) for every service + template/route cross-checks
```

## Deploy

**New to this?** Follow [docs/deployment.md](docs/deployment.md) — it goes from an empty AWS
account to a working API, from your own machine or from GitHub Actions. Afterwards,
`.\scripts\test-deployment.ps1 -Domain <domain>` checks every service and prints a pass/fail table.

Two equivalent ways: CloudFormation with `deploy.ps1` (below), or Terraform in
[`terraform/`](terraform/README.md). Both build the same stack from the same `src/`;
`tests/test_terraform_parity.py` keeps them in step. Use one or the other in an
account, not both.

Prerequisites: AWS CLI v2, credentials for the target account, PowerShell 7
(or Windows PowerShell 5.1), Python 3 (for the docs freshness check), and a
Route 53 hosted zone for the domain **in the same account**.

```powershell
.\deploy.ps1 -Domain example.com
```

The script:

1. checks `src/discovery_*` are up to date with `docs/`;
2. resolves the hosted zone;
3. refuses to continue if `api.<domain>` is already a custom domain owned by
   something else;
4. inspects SES and picks safe values — it will not create an identity, MX or
   SPF/DMARC record that already exists, and if another receipt rule set is
   **active** in the region it offers to add this stack's rule to that set
   (recommended) instead of replacing it;
5. packages `src/` into a staging bucket and deploys with **every** parameter.
   Settings you don't pass keep the stack's current value (allow-lists, SMS
   handsets and region, throttles, retention); pass `''` to clear a list;
6. prints the outputs and warns if SES is still in the sandbox.

First deploy takes ~5–10 minutes (ACM DNS validation). Useful options:
`-StackName`, `-Region` (default `ap-southeast-2`; must support SES
receiving), `-ApiHostLabel`, `-EmailSendAllowedTo`, `-EmailSendAllowedDomains`,
`-ThrottleRateLimit`, `-ThrottleBurstLimit`, `-Yes` (no prompts).

### SES production access (one manual step)

A new account's SES is in the sandbox: `mail/send` can only reach verified
addresses, 200/day. Request production access in the deploy region:

```powershell
aws sesv2 put-account-details --region ap-southeast-2 `
  --production-access-enabled --mail-type TRANSACTIONAL `
  --website-url "https://api.example.com/v1" `
  --use-case-description "Test-automation API. Sends synthetic test emails to its own catch-all domain and a short allow-list of test domains."
```

While in the sandbox, sends to allow-listed recipients outside the domain fail
with SES AccessDenied (the API returns 502).

## Text messages (SMS)

Three kinds of traffic, deliberately kept apart:

* **Capture handsets** (test SIMs running the forwarding app) POST every text they receive to
  `sms/receive?number=<own number>` with the header `X-Device-Key: <key>`. Without a configured
  key the call is refused and nothing is stored, so every real message came from a known device.
  The keys live in `sms-device-keys.txt` beside `deploy.ps1` (git-ignored, one per line). The
  first deploy creates it with a random key and prints it; build that key into the app. To rotate:
  add a second line, deploy, update the phones, remove the old line, deploy.
* **Real sends** (`sms/send`) only go to the numbers in `-SmsSendAllowedNumbers`, at most one per
  `-SmsSendIntervalSeconds` (10) per handset, through SNS in `-SmsRegion` (default
  `ap-southeast-2`, where the account's SMS production access and spend limit are). Any other
  number is refused with a ready `mockUrl`.
* **Simulated texts** (`sms/mocksend`) go to any other number, are returned with `mocked: true`,
  and are refused for registered and capture handsets, so a real handset's inbox only ever holds
  real texts.

```powershell
.\deploy.ps1 -Domain testdata.help -HostedZoneId <zone> -Region us-west-1 `
  -SmsSendAllowedNumbers "+61400000001,+61400000002"
```

Android capture app: post to
`https://api.<domain>/v1/sms/receive?number=<handset number>` with `Content-Type:
application/json`, the `X-Device-Key` header, and a body `{"sender": ..., "time": <epoch ms>,
"body": ...}` (the old `Sender`/`Time`/`Message` names are also accepted).

## Voice

`voice/say` turns text into speech with Amazon Polly and stores the audio under a `ref` that
is a hash of the whole request, so the same request is the same audio, byte for byte, and is
synthesised once. Design and decisions: [`docs/notes/voice.md`](docs/notes/voice.md).

* **`-VoiceRegion`** (default `ap-southeast-2`) is the region whose Polly is used. It need not be
  the stack's region, and for a stack in `us-west-1` it cannot be: that region has neither the
  neural nor the generative engine. The audio itself is stored in the stack's region.
* **`-VoiceName`** (`Olivia`) and **`-VoiceEngine`** (`neural`) are what `voice/say` uses when the
  caller names neither.
* **`-VoiceDailyCharacters`** (25000) caps the characters sent to Polly per UTC day. Every cache
  miss is billed per character and the endpoint is open, so this is the spend limit: when it is
  used up, new text gets a 429 and stored audio keeps working. `0` switches synthesis off. At
  Polly's list prices (USD 16 per million characters neural, USD 30 generative, USD 4 standard)
  the default is at most about USD 0.40-0.75 a day; raise it for a private deployment.

```powershell
.\deploy.ps1 -Domain testdata.help -HostedZoneId <zone> -Region us-west-1 -VoiceRegion us-west-2
```

Check on the first deploy (none of this can be exercised without the real service): one
`voice/say` per engine and per format, a tagged utterance on `generative` (its `<prosody>` is
only allowed around whole sentences), and that a trailing `[pause 1s]` in `mp3` is audible.

`voice/listen` turns audio into words with an Amazon Transcribe **batch** job in the stack's own
region (the job reads its audio from the voice bucket, which has to be in the region it runs
in). `voice/find` and `voice/compare` assert on what was heard.

* **`-VoiceLanguage`** (`en-AU`) is the language listened for when the caller names none; `auto`
  has each recording's language identified.
* **`-VoiceDailyListenSeconds`** (1800) caps the seconds of audio sent to Transcribe per UTC day.
  Transcription is billed by the second and the endpoint is open, so this is the spend limit:
  when it is used up new audio gets a 429 and audio already heard keeps working. `0` switches
  listening off. Multiply by Transcribe's per-second price in your region for the most a day
  can cost.
* Only audio whose length this service can count for itself is passed on: WAV, MP3 frames and
  raw samples. Ogg, FLAC, WebM, M4A and AMR uploads are refused, because those containers only
  declare a duration and can hold hours of silence in a few kilobytes - which would make the
  budget above meaningless.
* A finished transcription job is deleted from Transcribe at once; the audio and its transcript
  live under `heard/` in the voice bucket and expire after 30 days.

Check on the first deploy: POST a short WAV and an MP3 to `voice/listen` and time them (expect
several seconds; a reply that returns `pending: true` is normal), `voice/compare?ref=<a say
ref>` for a round trip, a one-word clip, `language=auto`, an utterance made with `format=ogg`
(Polly's Ogg is Vorbis; whether Transcribe takes it is unverified), and that the Transcribe
console shows no jobs left behind.

## Build number and health check

Every deploy is stamped with a **build**: `deploy.ps1` passes `BuildId` (the UTC
deploy time as `yyyymmdd.hhmm`; `-BuildId` to choose your own), `BuildCommit`
(`git rev-parse --short HEAD` when the folder is a git working copy, with a `+`
for uncommitted changes in `src/` or `template.yaml`) and `DeployedTime`. Every
function carries the same three values, so:

* every response has an `X-Build` header — log it with test results;
* `GET /v1/health/get` returns the build, the contract `version`
  (`src/version.json`), region and services — and calls nothing;
* `GET /v1/health/check` sends a real request with a known answer to every
  service through the public API (lists, lock, names, qr, totp with the RFC 6238
  vectors, webhook, perf, a real mail round trip through SES inbound with an
  attachment search, a mock SMS, Polly voices, a stored utterance and `voice/find`
  listening to it through Transcribe) and reads
  every function's configured build, flagging any on another build.
  `status` is `ok`, `degraded` or `down`; `strict=true` turns anything but `ok`
  into a 503 for uptime monitors.

The check is public, so its result is cached for 60 s in the small health table
(`fresh=true` forces a new run once the last one is 30 s old) and a lease stops
two running at once. Each run costs about 40 API requests and one email to the
stack's own domain; the voice probe reuses one stored utterance and its stored
transcript, so Polly and Transcribe are only called when those copies have
expired (about 2 s of Transcribe a month). On the first check after a deploy the
voice probe may report `timeout` while that first transcription runs; the next
check reads it. With `VoiceDailyListenSeconds=0` the listening step reports
that it is switched off and does not count as a fault. Nothing is scheduled: it runs only when
called, so it adds no standing cost. To monitor it, point an uptime checker at
`/v1/health/check?strict=true` every few minutes.

**Tracing.** With `Tracing=true` (the default; `-Tracing false` to switch it off)
the v1 API stage and every function are traced with AWS X-Ray, and an X-Ray
sampling rule (`<stack>-health`) keeps every health call. `health/get` and
`health/check` return `traceUrl`: their own trace as a public page,
`/v1/trace/get?ref=<traceId>&format=html`, which needs no sign-in and appears a
few seconds after the response (the page waits for it). `traceDataUrl` is the
same as JSON and `traceConsoleUrl` the AWS console view for you as operator.
`health/check` also returns `checkTraceUrl`, the trace of the run behind the
result. `trace/get` (its own small function, `trace_view.py`, allowed only
`xray:BatchGetTraces`) shows only this stack's segments - names starting with
the stack name - and only an allow-list of fields: paths without hosts or query
strings, methods, statuses, function and API names, timings and fault flags.
It never shows account ids, ARNs, request ids or caller IPs, and a trace from
any other application in the account is "not found". `health/check` passes
its trace header on its probe requests, so that trace shows the health function,
each API Gateway request and each service function with their timings. Without
an X-Ray SDK in the code package, a function's trace shows its invocation,
initialisation and overhead, not the DynamoDB or S3 calls inside it. X-Ray is
billed per trace recorded, with no standing cost: other requests follow the
account's default sampling rule (1 a second plus 5% of the rest), which matters
only for long, heavy load tests.

The probes use a private namespace (random, made on first use, never returned)
and clear up after themselves. The health function's role can read the
configuration of functions named `<stack>-*`, nothing else.

## Operating a public, unauthenticated API

There is deliberately no authentication. What limits abuse:

* **Stage throttling** — `ThrottleRateLimit` / `ThrottleBurstLimit` (default
  500 rps / 1000 burst across the v1 API). Raise them for big load tests.
* **Mail send allow-list** — `from` must be at the domain; `to` must be at the
  domain or in `EmailSendAllowedTo` / `EmailSendAllowedDomains`. Keep those
  short: anyone can call the endpoint.
* **Size caps in code** — 4 MB request bodies, 50 MB imports, 100 MB stored
  attachments, 28 MB outbound attachments, 3000 characters per utterance.
* **Voice synthesis budget** — `VoiceDailyCharacters` characters to Polly per UTC
  day; repeats of a stored utterance are free and never refused.
* **Voice listening budget** — `VoiceDailyListenSeconds` seconds of audio to
  Transcribe per UTC day, counted from the audio itself (formats whose length
  cannot be counted are refused); audio already heard is free and never refused.
* **Expiry** — received mail and its attachments 7 days, import files 90 days,
  webhooks `WebhookRetentionDays` (30), raw perf points 7 days, locks 60 s,
  generated utterances 30 days after their last use, heard audio 30 days,
  recon keys 30 days after their first write.
  Lists, variables, counters, the cleanup register, TOTP secrets, saved
  attachments, saved audio and the mail table do not expire.
* `robots.txt` asks crawlers to stay out of `/v1/`.

Anyone can read anything under a key they can guess — tell users to use a
random key per run (`lists/codes` → `rand10`) and never to put real personal
data or real credentials into it.

Tables and buckets have `DeletionPolicy: Retain`: deleting the stack keeps the
data; delete those resources by hand if you mean to.

## Changing things

* **Docs** — edit `docs/llms.md` (the published llms.txt, hand-written),
  `docs/openapi/<service>.json` or `docs/site/index.html`, then
  `python tools/build_docs.py`. In `docs/llms.md` write `{BASE_URL}`,
  `{ROOT_URL}` and `{MAIL_DOMAIN}` rather than the real domain;
  `tests/test_discovery.py` fails if an action in the OpenAPI document is
  missing from it. Every JSON response has a schema: when a response gains or
  changes a field, update its schema in the fragment, or
  `tests/test_zz_contract.py` fails (it checks every response the suite produces
  against both documents). `docs/guide/*.md` is the older per-service guide and is no
  longer published. The documents ship in the code package, so a
  normal deploy publishes them (no API Gateway redeployment needed).
* **A new action on an existing service** — code only (routing is by proxy
  resource), plus its OpenAPI fragment and guide section.
* **A new service** — add a function, role and the two API resources/methods
  to `template.yaml` (with the three `BUILD_*` environment variables every
  function carries), **bump the `V1DeploymentF` logical id** (e.g. to
  `V1DeploymentG`), add the service to `ORDER` in `tools/build_docs.py`, give
  `health.py` a probe for it, and mirror it in `terraform/`
  (`terraform/tests/check_parity.py` says what is missing).
* **The API contract version** — `src/version.json` (semver). Bump it when the
  published API changes; `tools/build_docs.py` copies it into the OpenAPI
  `info.version`.
  `tests/test_routing.py` checks that every documented path reaches a handler
  that recognises it.
* **v2** — a new REST API mapped at `v2` with its own tables; v1 URLs never
  change meaning.

## Tests

```bash
pip install pytest "moto[all]" openapi-spec-validator openapi-schema-validator pyyaml flask-cors
cd tests/node && npm install && cd ../..
python -m pytest tests/
```

`tests/test_routing.py` cross-checks `template.yaml` against the OpenAPI
document and the handlers; `tests/test_discovery.py` checks the built
documents are current and that nothing references the pre-v1 deployment.

## Known limitations / decisions to revisit

* `lists/clear` removes records only; variables, counters and the cleanup
  register under the same key are kept.
* The mail table has no TTL (a plain TTL would break the pop pointer logic);
  `mail/clear` is the way to empty an address.
* Mail sent to several of the domain's addresses at once (Cc/Bcc) is filed
  under the first matching header address, as before.
* `BinaryMediaTypes` is `text/csv` and `application/octet-stream`. Verify on
  the first deploy that a Windows-1252 CSV POSTed as
  `text/csv; charset=windows-1252` still imports correctly.
* `wait=` holds a Lambda for its whole duration — right for functional tests
  and pipelines, not for thousands of virtual users.
* `recon` counts each record with one atomic `ADD` to its group, then moves
  the summary counters by exactly what that record changed, and points the
  state index at the group's new state. These are three requests: if a
  function is stopped between them (a timeout mid-batch), the summary or the
  state index can lag the groups, which stay exact (`recon/get?value=` reads
  them directly; `recon/get` with `fields=` recounts from them). A key is
  kept 30 days from its first write; `ignorecase` and `suffix` are fixed by
  that first write. Records are counted, not stored one by one, so a retried
  add counts twice: client record ids for idempotent retries are phase 2.
  Under count matching, fields can only be compared in groups that are unique
  on both sides.

## Licence

Released under the [MIT License](LICENSE).

Copyright (c) 2026 Test Data Services Pty. Ltd. (ABN 91 635 331 332),
Torquay VIC 3228, Australia. Designed and developed by Paul McLean.
See [NOTICE](NOTICE) and [AUTHORS.md](AUTHORS.md).

## Contributions

This is an open-source project, but **it is not accepting contributions at this
time** — pull requests are switched off. You are welcome to fork it and change
your copy under the MIT License. See [CONTRIBUTING.md](CONTRIBUTING.md).
