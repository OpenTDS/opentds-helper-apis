# Test Helper API v1

> **Open source (MIT), not accepting contributions at this time.** Fork freely; see [CONTRIBUTING.md](CONTRIBUTING.md).

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
| `voice` | Spoken utterances for testing voice interfaces: text with `[tags]` for emotion, pace and pauses becomes audio (Amazon Polly), stored by content so every request replays byte for byte |

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
  webhook.py totp.py lock.py names.py names_data.py qr.py perf.py
  voice*.py              voice (API, tags -> script -> SSML, audio helpers, S3/Polly store, play page)
  discovery.py           /v1 index, openapi.json, openapi-3.0.json, llms.txt, /.well-known/*
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
account to a working API, from your own machine or from GitHub Actions.

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
* **Expiry** — received mail and its attachments 7 days, import files 90 days,
  webhooks `WebhookRetentionDays` (30), raw perf points 7 days, locks 60 s,
  generated utterances 30 days after their last use.
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
  to `template.yaml`, **bump the `V1DeploymentC` logical id** (e.g. to
  `V1DeploymentD`), and add the service to `ORDER` in `tools/build_docs.py`.
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

## Licence

Released under the [MIT License](LICENSE).

Copyright (c) 2026 Test Data Services Pty. Ltd. (ABN 91 635 331 332),
Torquay VIC 3228, Australia. Designed and developed by Paul McLean.
See [NOTICE](NOTICE) and [AUTHORS.md](AUTHORS.md).

## Contributions

This is an open-source project, but **it is not accepting contributions at this
time** — pull requests are switched off. You are welcome to fork it and change
your copy under the MIT License. See [CONTRIBUTING.md](CONTRIBUTING.md).
