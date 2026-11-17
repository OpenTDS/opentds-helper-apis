# Test Helper API — DevWeb regression suite

Twelve DevWeb scripts that regression- and performance-test every endpoint of the
Test Helper API (`https://api.testdata.help/v1` by default).

| Script | What it covers |
|---|---|
| `init` | Starts a test cycle (llms.txt recipe 4.12), then exits. It closes the gate (`setupReady=0`), clears the previous cycle's pools, publishes a new `namespace`, seeds the shared data, verifies it, and opens the gate (`setupReady=1`). |
| `vars` | `lists/vars/set`, `get` and `list`: special characters, empty values, case-sensitive names, 404s, `equals`, and `wait` as a real long poll (returns when the value is set, or changes to `failed`). |
| `lists` | `push`, `pop`, `cyclepop`, `random`, `list`, `reset` and `clear`; counters; the cleanup register; `codes`. Import covers CSV quoting, delimiters, renamed columns, JSON Lines, 422s, reload by ref, `wait=0`/status, and the S3 presigned upload end to end. |
| `names` | Every region; persona shape and ASCII folding; name order; shared or gendered surnames; seeds; `ANY`/`notregion`; errors. |
| `mail` | The catcher: `pop`, `wait`, `grace`/`repeat`, `reset`, `clear`, plus-addressing. The sender: generated, saved and received attachments, injected bytes, JSON/text POST bodies, 403 refusals. The store: `save` (base64 and binary), `get` (data, url, download/view 302), `text`, `find` (every search mode), `compare`, password-protected PDFs, `delete`, and the S3 upload. |
| `sms` | `mocksend` (templates, codes, sender, the 1-per-second limit); `get` (`since`, `real`, waiting for a new text); `list`; the refusals of `send`; the `sms/receive` device key. |
| `totp` | Stateless and stored codes, checked against DevWeb's own `load.utils.totp`; digits, period and algorithm; validation. |
| `webhook` | `receive` with every method, chosen statuses (including 204), headers, query, JSON, text and binary bodies; `list` (`since`, `limit`, waiting); `clear`. |
| `lock` | Acquire, busy, release, stale refs, waiting for a release, the release URL, and real mutual exclusion between the virtual users, checked with an atomic counter. |
| `qr` | JSON (the PNG is checked byte by byte: signature and size), SVG and HTML; `text`/`text64`; `ecc`, `scale`, `border` and `version`; limits and errors. |
| `perf` | `add`; `stats` checked against values computed in the script (percentiles within the documented 2%); windows; `series`; `assert` (pass, fail, `strict` 412); `keys`; `export`; `clear`. |
| `multi` | End-to-end flows: **signup** (names → lists → verification email → vars → TOTP enrolment with a QR code → webhook), **order** (cyclepop login → lock → invoice PDF emailed, searched and compared → saved → webhook → counters) and **SMS OTP**. Also every discovery document, CORS, and a `perf/assert` gate over the flow timings in `finalize`. |

Every step is a DevWeb transaction named `<script>_<step>`, about 440 in all.
A failed check fails its transaction, writes the reason (with the request and
response) to the log, and fails the iteration. **An all-green run means every
transaction passed and no iteration or Vuser had an error.**

## Running the regression

1. Deploy the current stack first. The scripts use `lists/vars/get` with `equals`/`wait`.
2. Add all twelve script folders to one scenario:
   * `init`: **1 Vuser**. Start it with the others, or before them. It finishes in about 10 seconds and stops itself.
   * The other eleven: **3 Vusers each for 2 minutes**. Each `scenario.yml` already says this, for OpenText Performance Engineering for Developers. In LoadRunner Professional, Enterprise or Cloud, set it in the scenario.
3. The test scripts wait at the gate in `initialize`, polling `setupReady` with `equals=1&wait=20`, until `init` has finished. If `init` fails, they stop at once. If it never runs, they give up after 10 minutes with a clear error.

Iterations are self-contained: each Vuser uses keys made from the namespace,
the script name, its user ID and the iteration, and deletes what it created.
The shared pools (`<ns>_logins`, `<ns>_products`, the invoice and Word files,
and a TOTP secret) belong to the cycle. The next `init` clears them.

### Settings (user arguments)

Defaults are in `_src/common.js`. Override them per script in a `user_args.json` file in the script folder, or as command-line user arguments:

| Name | Default | Meaning |
|---|---|---|
| `baseUrl` | `https://api.testdata.help/v1` | The deployment to test |
| `project` | `th-devweb-regress-Q7M4` | The cycle's control key. The API is open, so keep it distinctive, and use a different one for each suite run side by side. |
| `gateWaitSeconds` | `600` | How long a script waits for `init` |
| `smsHandset` | — | A registered handset number. It turns on the check that `mocksend` refuses it. |
| `smsDeviceKey` | — | With `smsHandset`: posts a capture to `sms/receive` and reads it back (Vuser 1 only) |
| `smsRealSend` | `false` | With `smsHandset`: sends one **real** text per iteration (Vuser 1 only). This costs money. |

Example `user_args.json`: `{ "project": "acme-regress-7QK2" }`.

### What a run sends

* **Email:** about 7 messages per `mail` iteration and 2 per `multi` iteration, all to addresses at the mail domain. That is roughly 100 to 150 per run, through SES.
* **SMS:** none. Texts are only mocked unless `smsRealSend` is on.
* **Traffic:** 34 Vusers in all. The stage throttle (500 requests per second) is far above what this generates.
* **Lambda concurrency:** while waiting at the gate, each script holds a Lambda for up to 20 seconds. A new AWS account with a low concurrency limit can see throttling (429). Raise the limit, or start `init` first.

## Editing the scripts

DevWeb runs each folder on its own, so each `main.js` is self-contained and **generated**. Edit `_src/`, then rebuild:

```
python devweb/build.py            # writes <script>/main.js and scenario.yml
python devweb/build.py --check    # the test suite runs this (tests/test_devweb.py)
```

* `_src/common.js` holds the settings, HTTP helpers, checks, transactions and the cycle gate.
* `_src/<script>.js` holds each script's own tests.
* `_src/fixtures.js` holds small PDF and Word documents as base64. `_src/fixtures/make_fixtures.py` regenerates them.

## Trying them without LoadRunner

`_harness/devweb-mock.js` is a small Node.js stand-in for the parts of the DevWeb SDK these scripts use. It is not DevWeb, but it is useful for a quick check from any machine with Node 18 or later:

```
node devweb/_harness/devweb-mock.js devweb/init --vusers 1 --duration 60
node devweb/_harness/devweb-mock.js devweb/lists --vusers 3 --duration 120 --quiet
node devweb/_harness/devweb-mock.js devweb/mail --vusers 1 --duration 60 --arg project=my-key-123
```

It prints a pass/fail table per transaction and exits with 1 if anything failed.
