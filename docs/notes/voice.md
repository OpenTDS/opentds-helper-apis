# voice — design and integration notes

Spoken utterances for testing voice interfaces. First build (November 2026): **speaking**.
Listening (transcribing and searching what the system under test said back) is a later build;
the store is shaped for it.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Path | `/v1/voice` | |
| Verb | `say` (new in the action vocabulary), with `get` for retrieval | A POST to `get` reads badly, and it keeps "`get` has no side effects" true |
| Default engine and voice | Polly **neural**, **Olivia** (en-AU); stack parameters | Natural, stable (the October 2026 generative upgrade left neural untouched), takes rate and volume |
| Replay | From storage, never by asking the engine again | Generative voices are upgraded in place and need not say the same thing the same way twice |
| Identity | `ref` = hash of the canonical request; `key` = caller-chosen, permanent | The mail-attachment concepts, unchanged |
| Tags | ElevenLabs-style `[tag]`, parsed into a neutral script | One stored form, many renderers |
| Honesty | Every tag reported `native` / `approximated` / `inferred` / `none` | No AWS voice takes emotion as an instruction yet; a test must be able to tell |
| Listening | Later | Scope |

## Modules (flat in `src/`)

| File | Role |
|---|---|
| `voice.py` | API handler `voice.handler`: `say`, `get`, `save`, `delete`, `voices`, `tags` |
| `voice_script.py` | Tags and segments → the script; script → SSML per engine; the per-tag report. No AWS |
| `voice_audio.py` | WAV header, digital silence, durations (PCM/WAV/mu-law exact, MP3 by frames, Ogg by granule), sniffing uploads. No AWS |
| `voice_store.py` | Env, S3/Polly/DynamoDB clients, refs, the two stores, voice catalogue, spend guard |
| `voice_page.py` | The HTML page behind `play=true`. No AWS |

## How a `say` request becomes a ref

1. Query parameters, overlaid with the POST body (the fields of a JSON object, or of a form whose
   names are all known ones; any other body is the text).
2. Text with `[tags]`, or `segments`, is parsed into the **script**: a list of
   `{"say": text, "emotion"?, "pace"?, "volume"?}`, `{"pause": ms}` and `{"cue": "sighs"}`
   (sounds, accents and unknown tags — not renderable today, kept verbatim for an engine that
   can). `emotion=x` is exactly a leading `[x]`, so both forms are one utterance.
3. Engine, voice and format are resolved to concrete values (the default voice, a seeded random
   pick, the default sample rate for the format).
4. `ref` = `v` + 11 base-36 characters of the SHA-256 of
   `{recipe, engine, voice, language, format, hz, script, pronounce}`.
   **Only resolved values are hashed**, so `voice/say?text=Hi` and
   `voice/say?text=Hi&voice=Olivia&engine=neural&format=mp3&hz=24000` are the same ref.
5. Meta object present → `cached: true`, no Polly call. Absent → reserve budget, synthesise,
   store audio then meta.

`recipe` (`voice_script.RECIPE`) is bumped whenever the rendering of any tag changes. Old refs
keep returning the audio they always did; the same request gets a new ref under the new rules.
Changing the deployment's default voice or engine has the same effect on requests that name
neither. **To replay exactly, use the `ref` (or a saved `key`), not the original request.**

## Rendering today (Amazon Polly SSML)

| Kind | standard | neural | generative |
|---|---|---|---|
| pause | `<break>` | `<break>` | `<break>` |
| pace | `<prosody rate>` | `<prosody rate>` | `<prosody rate>` around whole sentences |
| volume | `<prosody volume>` | `<prosody volume>` | `<prosody volume>` around whole sentences |
| whisper | `<amazon:effect name="whispered">` | volume `x-soft` | volume `x-soft` |
| emotion | recipe: rate + pitch + volume | recipe: rate + volume | nothing sent: the engine infers it from the words |
| sound, accent, unknown | not rendered | not rendered | not rendered |

Emotion recipes (`voice_script.EMOTIONS`): rate %, pitch % (standard only), volume.

| Emotion | Rate | Pitch | Volume | | Emotion | Rate | Pitch | Volume |
|---|---|---|---|---|---|---|---|---|
| anxious | 112 | +8 | | | tired | 84 | −6 | soft |
| panicked | 124 | +12 | loud | | relaxed | 92 | −3 | |
| upset | 108 | +5 | | | happy | 104 | +6 | |
| angry | 106 | −5 | x-loud | | excited | 112 | +10 | loud |
| frustrated | 106 | −2 | loud | | confused | 92 | +4 | |
| sad | 86 | −8 | soft | | surprised | 104 | +12 | |
| curious | 100 | +6 | | | sarcastic | 92 | −4 | |

These are starting values chosen by reasoning, not by ear: **tune them by listening**, then bump
`RECIPE`. `curious` is pitch only, so on neural it is honestly reported as `none`.

Generative voices accept `<prosody>` only around full sentences, so for that engine the text is
cut into sentences and each takes the direction in force where it begins.

In `wav` and `pcm`, pauses at the very start and end are taken out of the SSML and added as
exact digital silence (16-bit zeros), so the lead-in and the trailing silence a voice-activity
detector needs are sample-accurate. In `mp3`/`ogg` they stay as `<break>`.

A `text` that is a complete `<speak>` document is passed through as written (well-formedness
checked; declarations, DOCTYPEs and comments refused).

## The play page (`play=true` on `say` and `get`)

`view=true` redirects to the audio file, and a browser tab shows its own player for a file.
`play=true` returns a small HTML page instead, which plays the audio from script
(`new Audio(url).play()`): only the words, a status line and the facts are on screen.

* **Whether it plays without a click is the browser's decision.** Measured in Chromium with
  Chrome's default desktop policy (`document-user-activation-required`):

  | How the visitor arrives | Result |
  |---|---|
  | A click on `<domain>` or `api.<domain>`: a link, `target=_blank`, or `window.open` with `noopener` | plays at once |
  | The same click, but opened with `noreferrer` | refused |
  | A click on another site (`noopener`) | refused |
  | `window.open` from any site that keeps its opener | plays at once |
  | Typed or pasted into a new tab | refused |

  Chrome recognises "a click on this site" from the referrer, so `noreferrer` loses it. The
  landing page therefore opens `play=true` links with `noopener` only (`openWin` in
  `docs/site/index.html`). When refused, the page catches `NotAllowedError` and shows one Play
  button. Automation lifts the rule with `--autoplay-policy=no-user-gesture-required`.
* The page is open to anyone with any text in it, so: the words and key are HTML-escaped; the
  audio URL enters the script as an escaped JSON string; the response carries a
  `Content-Security-Policy` allowing only the page's own script and style by nonce (a fresh one
  per response) and media over https. The CSP also forbids `eval`, which a test tool's
  wait-for-function helper may trip over: poll the `#status` text instead.
* The audio URL in the page is presigned (15 minutes); the page itself is `no-store`, so a reload
  gets a fresh one.
* `play`, `view` and `download` are mutually exclusive (400), checked before anything is
  synthesised. Errors stay JSON.
* Checked in headless Chromium: plays at once when allowed; message, button, click, plays when
  not; no CSP violations; no `<audio>` element in the page. Not checked: Firefox and Safari
  (expect the Play button), and the deployed landing page end to end.

## Leaving room for native emotion

When Polly (or another native AWS speech service) takes emotion or style directly:

* add the engine to `ENGINES` and a row to `CAPABILITY` (or change `emotion` to `native` on an
  existing row), add its branch in `prosody()` / `_blocks()` / `report()`, bump `RECIPE`;
* callers change nothing: the same tags, now reported `native`;
* existing utterances can be re-rendered from their stored scripts:
  `voice/say?ref=<ref>&engine=<new>` (sounds and unknown tags were kept for exactly this);
* `voice/voices` → `engines.<engine>.emotion` is where a test finds out.

A renderer for a non-Polly service needs only: script in, audio bytes out, and its own
`CAPABILITY` row.

## Lambda function — `voice.handler`

* python3.12, MemorySize 512, Timeout 29. `/voice` and `/voice/{proxy+}` (ANY), AWS_PROXY.
* Environment: `BASE_URL`, `VOICE_BUCKET`, `VOICE_TABLE`, `VOICE_REGION`, `VOICE_NAME`,
  `VOICE_ENGINE`, `VOICE_DAILY_CHARACTERS`.
* IAM: `polly:SynthesizeSpeech`, `polly:DescribeVoices` on `*`; `s3:GetObject, PutObject,
  DeleteObject` on the voice bucket's objects and `s3:ListBucket` on the bucket (so a missing
  object is a 404, not a 403); `dynamodb:GetItem, UpdateItem` on the voice table.
* Polly is called in `VOICE_REGION` (stack parameter `VoiceRegion`), which need not be the
  stack's region: us-west-1 has neither neural nor generative voices. The audio is stored in the
  stack's region.

## Bucket — `<prefix>-voice-<account>-<region>`

```
generated/audio/<ref>          the audio (ContentType set; Metadata filename)
generated/meta/<ref>.json      everything about it, including the script   <- existence = "this ref is real"
saved/audio/<key>
saved/meta/<key>.json
```

* Lifecycle: `generated/` expires after 30 days. On a cache hit on an utterance more than a day
  old, both objects are rewritten in place, which restarts the clock — so the 30 days run from
  the **last use**. `saved/` has no expiry.
* CORS: `GET`/`HEAD` from any origin (presigned URLs are fetched by test pages on the system
  under test's origin).
* Audio is written first, meta last. A failed synthesis leaves nothing behind.

## Table — VoiceTable

`PK` (S) / `SK` (S), TTL `ttl`. One item per UTC day: `PK = "budget"`, `SK = "2026-11-14"`,
`characters` = characters reserved that day (kept 90 days). The reservation is a conditional
`ADD` made **before** the Polly call and handed back if the call fails.

## Limits

* 3000 spoken characters per utterance; 6000 characters of SSML; one pause ≤ 10 s.
* `VoiceDailyCharacters` (default 25000) of new synthesis per UTC day; 0 = off. Polly list prices
  per million characters: standard USD 4, neural USD 16, generative USD 30 — so the default is at
  most about USD 0.40 (neural) to 0.75 (generative) a day. Long-form (USD 100) is not offered.
* Recording upload: 4096 KB (request-body route only).
* Inline audio up to 4096 KB; larger comes back as a presigned URL.

## Not verified against the real service

The tests use a scripted stand-in for Polly. Check on first deploy:

1. each engine × each format once (in particular `mulaw`, `alaw` and `ogg`);
2. `generative` with pace/volume tags (the whole-sentence rule);
3. a trailing `[pause 1s]` in `mp3` — whether Polly renders an edge `<break>` as silence;
4. `durationMs` for `mp3` against a player (computed by walking MPEG frames);
5. how the emotion recipes actually sound.

## Later

* **Listening**: `voice/text` (Amazon Transcribe, cached beside the audio, `wait` + `pending`),
  `voice/find` and `voice/compare` with the attachment parameters — including a round-trip check
  of generated audio (`text` in, transcript out).
* `voice/upload` (a page with a record button that saves 16 kHz WAV) and `voice/upload/url`
  for recordings over 4 MB.
* A batch form of `say` to warm many utterances in one call.
* Per-segment voices (dialogue) for `wav`/`pcm`, by concatenation.
* Speech marks (word timings) on standard and neural; not available on generative.
