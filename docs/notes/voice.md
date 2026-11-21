# voice — design and integration notes

Both halves of a spoken conversation, for testing voice interfaces. First build (November
2026): **speaking** — `say` and the store behind it, which is most of this note. Second build:
**listening** — `listen`, `find`, `compare`; see [Listening](#listening-listen-find-compare).

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
| Listening | Its own build, on the same store and the same words (`ref`, `key`, `wait`, `find`) | See [Listening](#listening-listen-find-compare) |

## Modules (flat in `src/`)

| File | Role |
|---|---|
| `voice.py` | API handler `voice.handler`: `say`, `get`, `save`, `delete`, `voices`, `tags`; routes `listen`, `find`, `compare` to `voice_listen.py` |
| `voice_script.py` | Tags and segments → the script; script → SSML per engine; the per-tag report. No AWS |
| `voice_audio.py` | WAV header, digital silence, durations (PCM/WAV/mu-law exact, MP3 by frames, Ogg by granule), sniffing uploads. No AWS |
| `voice_store.py` | Env, S3/Polly/Transcribe/DynamoDB clients, refs, the stores (generated, saved, heard), voice catalogue, spend guards |
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
  `VOICE_ENGINE`, `VOICE_DAILY_CHARACTERS`; for listening, `VOICE_LANGUAGE` and
  `VOICE_DAILY_LISTEN_SECONDS`.
* IAM: `polly:SynthesizeSpeech`, `polly:DescribeVoices` on `*`; `s3:GetObject, PutObject,
  DeleteObject` on the voice bucket's objects and `s3:ListBucket` on the bucket (so a missing
  object is a 404, not a 403); `dynamodb:GetItem, UpdateItem` on the voice table; for listening,
  three `transcribe:` actions (see Listening).
* Polly is called in `VOICE_REGION` (stack parameter `VoiceRegion`), which need not be the
  stack's region: us-west-1 has neither neural nor generative voices. The audio is stored in the
  stack's region.

## Bucket — `<prefix>-voice-<account>-<region>`

```
generated/audio/<ref>          the audio (ContentType set; Metadata filename)
generated/meta/<ref>.json      everything about it, including the script   <- existence = "this ref is real"
saved/audio/<key>
saved/meta/<key>.json
heard/...                      what was given to voice/listen, and its words (see Listening)
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

## Listening (`listen`, `find`, `compare`)

Second build (November 2026). The other half of a conversation: the audio the system under test
answers with becomes words a test can assert on.

### Decisions

| Decision | Choice | Why |
|---|---|---|
| Verb | `listen` (new in the action vocabulary), not `text` | Pairs with `say`; `text` on mail attachments is an extraction with nothing to wait for, this is a job |
| Engine | Amazon Transcribe **batch** jobs, in the stack's own region | Streaming is not offered in every region (not in us-west-1), boto3 has no streaming client, and a Lambda behind API Gateway cannot hold a stream open. A batch job reads from S3, so it runs where the bucket is |
| Identity | `ref` = `h` + 11 base-36 characters of SHA-256(prepared audio + language + recipe) | Content-addressed like `say`: the same audio is the same listening, transcribed once. The first letter tells `get`/`save`/`find` which store a ref lives in |
| Waiting | `wait` 0–20, **default 20**; `pending: true` at 200 | A job takes seconds even for one sentence. Unlike `mail/pop` there is no useful answer without waiting, so waiting is the default |
| One job per audio | The job **name** is `<bucket>-<ref>` | A second request for the same audio collides (`ConflictException`) and follows the first job instead of paying again — fifty virtual users hearing the same reply cost one transcription. No lock table needed |
| Matching | Canonical tokens (`voice_words`), never strings | A transcriber writes the same speech "3 p.m." or "three PM"; the system under test words its reply differently each run |
| `find` | The mail-attachment modes: `text`, `regex`, `left`/`right` | One vocabulary. Results come back in heard words with timings and confidence |
| Misses | `closest`, `uncertain`, `confidence`, `fuzzyCount`, `hint` | The first question on a failed assertion is "did the bot say it wrong, or did the transcriber hear it wrong?" |
| What may be listened to | WAV, MP3, raw pcm/mulaw/alaw. **Not** Ogg, FLAC, WebM, M4A, AMR | See "The length is counted here" |
| Retention | `heard/` expires after 30 days; a finished job is deleted from Transcribe at once | It is evidence from one run and holds whatever the system under test said. `voice/save` keeps what matters |

### Modules

| File | Role |
|---|---|
| `voice_listen.py` | `listen`, `find`, `compare`: body forms, which audio, preparing it, the job (start / follow / finish / fail), responses |
| `voice_words.py` | Canonical tokens; Transcribe's JSON → words; `find_text`, `find_between`, `closest`, `at` (regex match → heard words), `compare` (true edit distance). No AWS |
| `voice_audio.py` | adds `wav_plain` (any uncompressed WAV → a minimal one, length counted), `mp3_clean` (an MP3 → its frames only, length counted), G.711 decode, padding |
| `voice_store.py` | adds the `heard/` store, `heard_ref`, the Transcribe client and the seconds budget |

### How audio becomes a ref

1. **The audio** is the POST body (octet-stream, or base64 text), a JSON body's `audio` or
   `chunks` (decoded piece by piece and joined), or stored audio named by `ref` / `key`.
2. **What it is**: by its header (`sniff`), or — for audio with no header — because the caller says
   so (`format=pcm|mulaw|alaw`, `hz`). A `format` that contradicts a header is a 400.
3. **Prepared** (`_prepare`), which is where its length is counted:
   * WAV → `wav_plain`: PCM of 8, 16, 24 or 32 bits, or mu-law/A-law — all converted to 16-bit
     PCM (the form Transcribe recommends); 1–2 channels (all it takes); 8–48 kHz. The header is
     rewritten from what was read; other chunks are dropped.
   * raw pcm / mulaw / alaw → 16-bit mono WAV at `hz`.
   * MP3 → `mp3_clean`: the Layer III frames only. ID3 tags, padding and the encoder's Xing / Info
     / VBRI summary frame are dropped.
   * A WAV shorter than 600 ms is padded with silence to 600 ms (`paddedMs`): Transcribe refuses
     clips under half a second, and "Yes." is one.
4. `ref` = hash of the prepared bytes, the language asked for, and `RECIPE`. So the same reply
   sent as raw samples, as a WAV file or as base64 chunks is one listening.

### The length is counted here

Transcribe bills by the second, the endpoint has no login, and the daily budget
(`VoiceDailyListenSeconds`) is the only spend limit. A budget is only as good as the number it
counts, so the rule is: **audio is passed to Transcribe only if this service has counted its
length from the bytes that will be decoded.**

* Uncompressed samples: so many bytes are so many seconds. The WAV header's own byte rate is
  ignored and the header is rewritten, so what Transcribe decodes is what was counted. Worst
  case for a 4096 KB body is 8 kHz 8-bit mono: 524 s.
* MP3: each frame is a fixed length of sound (1152 or 576 samples) and cannot be smaller than
  its bit rate says (8 kbit/s at least; free-format frames are not accepted). Only validated
  frames are passed on, with nothing between them, so the count is of exactly what will be
  decoded.
* Ogg (Vorbis, Opus), FLAC, WebM, M4A and AMR **declare** their length in a header and can
  encode silence in almost no bytes: a few kilobytes can be hours of audio, which Transcribe
  would bill in full (up to its four-hour limit) while the budget counted whatever the header
  claimed. Counting them properly means decoding them, which pure Python cannot do. They are
  refused with a 400 that says so and suggests `ffmpeg`.
* The exception is audio this service made: an Ogg utterance from `voice/say` (a `v…` ref, or a
  key saved from one, or kept from a listening) is trusted, because Polly produced it from at
  most 3000 characters. It is passed on as it is. Polly's Ogg is **Vorbis**; Transcribe's guide
  lists "Ogg" for batch jobs without naming a codec (its Medical page says Opus), so whether
  such a job succeeds is unverified — a refusal is a 422 with a hint to say it again as WAV.

If compressed uploads matter later, the honest ways in are a decoder in a Lambda layer, or a
per-request cost ceiling in Transcribe if AWS adds one — not trusting the header.

### The job

```
_transcribe(src)                       h… ref: load it.  audio: prepare → ref → load heard/meta/<ref>.json
  done     → return (cached)
  pending  → _follow
  missing / failed-and-sent-again → _start → _follow

_start    reserve seconds (ceil of the counted length) → put heard/audio/<ref>.<ext>
          → StartTranscriptionJob(name = <bucket>-<ref>, MediaFormat, LanguageCode | IdentifyLanguage,
                                  OutputBucketName, OutputKey = heard/raw/<ref>.json)
          → put meta {state: pending}
          ConflictException → look at the job: running, or completed with its transcript → follow
          it and hand the seconds back; anything else is stale → delete it and start once more
_follow   GetTranscriptionJob every POLL_S (1 s) until COMPLETED / FAILED / the caller's deadline
_finish   read heard/raw/<ref>.json → voice_words.digest → meta {state: done, text, words…}
          → DeleteTranscriptionJob (best effort)
_failed   meta {state: failed, reason} → 422. Asking by ref repeats the 422; sending the audio
          again starts a new job
```

* The job reads the audio and writes the transcript with the **function role's own** S3
  permissions; Transcribe needs no role or bucket policy.
* A start that fails is refunded in the same request. A **job** that fails is not: several
  requests may find the failure at once, and a refund from each would be a way to grow the
  budget. Whatever Transcribe charges for a failed job, the count errs on the careful side.
* The Transcribe client makes one attempt with short timeouts: the longest wait (20 s) plus one
  hung call still fits inside API Gateway's 29 s. A timeout while following is treated as "not
  yet".
* Meta is written after the job starts, so a record never claims a job that does not exist; a
  job with no record is found again through the `ConflictException` path.
* The request that starts a job asks Transcribe about it every second. Any other request waiting
  on the same job reads the stored record instead and asks Transcribe only on every fourth look
  (`ASK_EVERY`) — enough to finish the job if its starter has gone away, without a hundred
  virtual users waiting on one reply each calling Transcribe every second.

### The words (`voice_words`)

A transcript is digested once into `words[]` — `word`, `shown` (with its punctuation), `startMs`,
`endMs`, `confidence`, and `end` where a sentence ends — and stored in the meta. Everything else
is computed from that on each request, so a better matcher improves old listenings too.

Canonical tokens, made the same way from heard words and from what a test expects: lower case;
punctuation and apostrophes dropped; spoken numbers and ordinals to digits (`twenty-fourth`,
`24th` → `24`; "a hundred and five" → `105`); digits read one at a time joined (`4 8 3 9` →
`4839`); `$3.50` → `3 dollars 50 cents` (and the "and" of "three dollars and fifty cents" is
dropped); `8:30` → `8 30`; `1.5` → `1 point 5`; `50%` → `50 percent`; a few spoken/written twins
(`doctor` = `dr`, `okay` = `ok`). "second" is deliberately not an ordinal. Each token remembers
the heard words it came from, which is how a match is reported as heard, with times.

Transcribe writes numbers as digits in the languages where it supports that, and as words in
the rest. The example in its guide — said: "Meet me at eight-thirty AM on June first at
one-hundred Main Street with three-dollars-and-fifty-cents and one-point-five chocolate bars";
written: "Meet me at 8:30 a.m. on June 1st at 100 Main Street with $3.50 and 1.5 chocolate
bars" — is a test: both sides give the same tokens.

* `find` `text`: the expected tokens as a window over the heard tokens. `fuzzy` is per-word
  `difflib` similarity (spelling, not sound); a number is right or wrong, never nearly.
* `find` `left`/`right`: the same windows as markers; without `right` the capture ends at the
  sentence end. A marker with nothing after it is counted in `emptyMatches`, not matched.
* `find` `regex`: `mail_extract.find_regex` (with its timeout guard) over the normalised string;
  `voice_words.at` maps the match's character span back to heard words. `exact=true` runs it
  over the transcript as written instead.
* `compare`: a full edit-distance table over tokens (true word error rate), walked back into
  `differences`. Capped at 1,000,000 cells (about 1000 words each side).

English only in the number handling. Other languages still transcribe, find and compare — as
words, with their numbers left as written.

### Store, stack and limits

```
heard/audio/<ref>.<wav|mp3|ogg>   the audio as handed to the transcriber
heard/meta/<ref>.json             state (pending | done | failed), the job, the audio's facts, the words
heard/raw/<ref>.json              Transcribe's own output
```

* Lifecycle rule `expire-heard-audio`: `heard/` after 30 days. `voice/save?key=…&ref=h…` copies
  the audio into `saved/` (with the words heard so far) to keep it.
* Parameters `VoiceLanguage` (`en-AU`; a Transcribe code, or `auto`) and
  `VoiceDailyListenSeconds` (1800; 0 = off) → `VOICE_LANGUAGE`, `VOICE_DAILY_LISTEN_SECONDS`.
* IAM: `transcribe:StartTranscriptionJob`, `GetTranscriptionJob`, `DeleteTranscriptionJob` on
  `*`, as for Polly. Narrowing them to this stack's job names (`<bucket>-*`) may be possible; it
  was not attempted without a real account to try the policy against.
* Budget: one item per UTC day in VoiceTable, `PK = "listen"`, `seconds`; the same conditional
  `ADD` as the character budget. One clip longer than the whole day's budget is a 400.
* 4096 KB per POSTed body; `wait` ≤ 20 s.
* Transcribe may leave a small `.write_access_check_file.temp` object beside its output; it is
  harmless.

### Not verified against the real service

The tests use a scripted stand-in for Transcribe (jobs that take a set number of status checks,
conflict on a repeated name, and write a transcript in Transcribe's format to the bucket). Check
on first deploy:

1. a WAV, raw `pcm` at 24 kHz, and an MP3 — and how long each takes (this decides whether one
   request usually suffices or `pending` is the normal first answer);
2. that the job can read `heard/audio/…` and write `heard/raw/…` with the function role alone;
3. a clip of one word (the 600 ms padding), and an MP3 under half a second (expected: 422);
   and `voice/listen?ref=` on a `format=ogg` utterance (Ogg Vorbis: accepted, or a 422?);
4. `language=auto` on a short reply (identification may need more speech than a short reply has);
5. how Transcribe really writes times, currency, abbreviations and digits read out one by one
   in `en-AU`, against the canonical tokens above — the rules in `voice_words` are checked
   against the one example in its guide, not against real transcripts;
6. the exact error code when a job is not found (handled: `NotFoundException` and
   `BadRequestException`) and when a name collides (`ConflictException`);
7. `voice/compare?ref=<a say ref>` for each engine: the round-trip similarity of the default voice.

## Later

* **A conversation log**: `voice/conversations/…` — the said and heard refs of one test's
  turns under a key, with the gaps between them (how long the system took to answer).
* **A browser shim**: a script for the page under test that feeds `voice/say` audio to
  `getUserMedia` as a virtual microphone and taps the page's audio output as raw samples for
  `voice/listen` — so a browser test needs no fake-device flags, and no `MediaRecorder` (whose
  WebM cannot be listened to).
* **Names**: a custom vocabulary (practitioner and place names) passed to the job, so that the
  names a deployment cares about are heard correctly; and a phonetic measure beside `fuzzy`.
* **Speakers**: diarisation, for recordings with both parties on one channel.
* **Streaming** transcription, where the region offers it, for replies that must be acted on
  before they finish.
* `voice/upload` (a page with a record button that saves 16 kHz WAV) and `voice/upload/url`
  for recordings over 4 MB.
* A batch form of `say` to warm many utterances in one call.
* Per-segment voices (dialogue) for `wav`/`pcm`, by concatenation.
* Speech marks (word timings) on standard and neural; not available on generative.
