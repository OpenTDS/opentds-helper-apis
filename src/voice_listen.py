"""voice_listen - hearing: audio in, words out, and assertions on what was said.

  POST /v1/voice/listen                  the audio as the body -> what was said in it
  /v1/voice/listen?ref=REF | key=KEY     audio already stored: said (v...), heard (h...), saved
  /v1/voice/find?ref=REF&text=...        was this said? (also regex=, left= + right=)
  /v1/voice/compare?ref=REF&text=...     how close is what was heard to what was expected?

A listening is stored by content, as an utterance is: its ref is 'h' + a hash
of the audio and of the language it was listened to in. The same audio is
therefore transcribed once; asking again - by ref, or by sending it again - is
free and gives the same words.

The words come from an Amazon Transcribe BATCH job (streaming is not offered
in every region, and a Lambda behind API Gateway could not hold a stream open
anyway). A job takes some seconds even for one sentence, so every endpoint
here takes wait= (0-20, default 20) and answers pending: true when the words
are not ready; the same request again, or ref=, picks the job up where it was.

One job per audio: the job's NAME is derived from the ref, so a second request
for the same audio - fifty virtual users may be hearing the same reply -
collides with the first (ConflictException) and follows it instead of paying
for it again.

What may be listened to is narrower than what Transcribe accepts, on purpose:
see voice_audio (LISTEN_HZ, NOT_COUNTABLE). The words themselves - canonical
tokens, finding, comparing - are voice_words, which has no AWS in it.
"""
import json
import math
import re
import time

from botocore.exceptions import ClientError

from common import FALSE, KEY_RE, KEY_RULE, TRUE, ApiError, b64decode, base_url, respond
import mail_extract as mx
import voice_audio as va
import voice_store as store
import voice_words as vw

RECIPE = 1                # part of every heard ref: bump it when a job is set up differently
MAX_AUDIO_KB = 4096       # a POSTed body (API Gateway + Lambda payload caps)
MAX_COMPARE_CELLS = 1000000   # expected words x heard words: the edit-distance table
MIN_MS = 600              # a WAV shorter than this is padded with silence: Transcribe
                          # refuses clips under half a second, and "Yes." is one
WAIT_MAX_S = 20
WAIT_DEFAULT_S = 20
POLL_S = 1.0
ASK_EVERY = 4             # a request following someone else's job asks Transcribe only on
                          # every 4th look, and reads the stored record on the others
FIND_LIMIT_DEFAULT = 20
FIND_LIMIT_MAX = 50

AUDIO_FIELDS = ('audio', 'chunks', 'format', 'hz', 'language', 'wait', 'ref', 'key')
LISTEN_FIELDS = AUDIO_FIELDS
FIND_FIELDS = AUDIO_FIELDS + ('text', 'text64', 'regex', 'regex64', 'left', 'left64', 'right',
                              'right64', 'fuzzy', 'exact', 'ignorecase', 'context', 'limit')
COMPARE_FIELDS = AUDIO_FIELDS + ('text', 'text64', 'otherref', 'otherkey', 'min')

DECLARABLE = ('wav', 'mp3', 'pcm', 'mulaw', 'alaw')
HEADERLESS = ('pcm', 'mulaw', 'alaw')
LANGUAGE_RE = re.compile(r'^[A-Za-z]{2,3}-[A-Za-z]{2}$')

# Transcribe errors the caller can fix, as opposed to Transcribe being unwell.
CALLER_ERRORS = ('BadRequestException', 'ValidationException')
GONE = ('NotFoundException', 'BadRequestException')


# ---------------------------------------------------------------- inputs ----
def _inputs(req, fields):
    """-> (parameters, POSTed audio or None).

    Parameters are the query string, overlaid with the fields of a JSON body.
    The audio is the body itself (raw bytes, or base64 text), or a JSON body's
    'audio' (base64) or 'chunks' (base64 pieces, joined in the order given -
    what a speech-to-speech model streams back)."""
    inp = dict(req.qs)
    if req.method != 'POST':
        return inp, None
    ctype = (req.headers.get('content-type') or '').split(';')[0].strip().lower()
    if ctype == 'application/octet-stream':
        return inp, (req.body_bytes() or None)
    body = req.body_bytes().decode('utf-8', 'replace').strip()
    if not body:
        return inp, None
    if body[:1] == '{':
        try:
            doc = json.loads(body)
        except ValueError as e:
            raise ApiError(400, 'The request body is not valid JSON.', detail=str(e))
        if not isinstance(doc, dict):
            raise ApiError(400, 'The request body must be a JSON object.')
        doc = {str(k).lower(): v for k, v in doc.items()}
        unknown = sorted(set(doc) - set(fields))
        if unknown:
            raise ApiError(400, 'Unknown fields in the request body.', received=unknown,
                           allowed=list(fields))
        audio = _json_audio(doc)
        inp.update({k: v for k, v in doc.items() if k not in ('audio', 'chunks')})
        return inp, audio
    text = ''.join(body.split())
    if not re.fullmatch(r'[A-Za-z0-9+/=_-]+', text):
        raise ApiError(400, 'The body is not audio this service can read. Send the raw bytes with '
                            'Content-Type: application/octet-stream, the base64 of the audio, or '
                            'a JSON object with "audio" (base64) or "chunks" (a list of base64 '
                            'pieces).', received=ctype or None)
    return inp, b64decode(text)


def _json_audio(doc):
    audio, chunks = doc.get('audio'), doc.get('chunks')
    if audio is not None and chunks is not None:
        raise ApiError(400, "Pass 'audio' or 'chunks', not both.")
    if chunks is not None:
        if (not isinstance(chunks, list) or not chunks or
                not all(isinstance(c, str) for c in chunks)):
            raise ApiError(400, "'chunks' must be a list of base64 strings: the pieces of the "
                                "audio, in the order they arrived.")
        return b''.join(b64decode(c) for c in chunks if c.strip())
    if audio is not None:
        if not isinstance(audio, str) or not audio.strip():
            raise ApiError(400, "'audio' must be the base64 of the audio.")
        return b64decode(audio)
    return None


def _s(inp, name):
    v = inp.get(name)
    if v is None or isinstance(v, (dict, list)) or str(v).strip() == '':
        return None
    return str(v).strip()


def _flag(inp, name, default=False):
    v = inp.get(name)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in TRUE:
        return True
    if s in FALSE:
        return False if s else default
    raise ApiError(400, f"'{name}' must be true or false.", received=v)


def _int(inp, name, default, lo, hi):
    v = _s(inp, name)
    if v is None:
        return default
    try:
        n = int(float(v))
    except ValueError:
        raise ApiError(400, f"'{name}' must be a whole number.", received=inp.get(name))
    return max(lo, min(hi, n))


def _text(inp, name):
    """name=... or name64=<base64> -> the text, or None."""
    raw64 = inp.get(name + '64')
    if raw64 not in (None, ''):
        try:
            return b64decode(raw64).decode('utf-8')
        except UnicodeDecodeError:
            raise ApiError(400, f"'{name}64' does not decode to UTF-8 text.")
    v = inp.get(name)
    if v in (None, ''):
        return None
    if not isinstance(v, str):
        raise ApiError(400, f"'{name}' must be a string.")
    return v


def _ratio(inp, name, lo=0.0):
    v = _s(inp, name)
    if v is None:
        return None
    try:
        x = float(v)
    except ValueError:
        x = -1.0
    if not lo <= x <= 1.0:
        raise ApiError(400, f"'{name}' must be a number between {lo:g} and 1.",
                       received=inp.get(name))
    return x


def _fuzzy(inp):
    """fuzzy=true -> the default threshold; a number -> that; absent -> 1.0 (off)."""
    v = inp.get('fuzzy')
    if v is None or isinstance(v, bool):
        return vw.FUZZY_DEFAULT if v else 1.0
    s = str(v).strip().lower()
    if not s or s in ('false', 'no', 'off', 'n', 'f', '0'):
        return 1.0
    if s in ('true', 'yes', 'on', 'y', 't'):
        return vw.FUZZY_DEFAULT
    try:
        x = float(s)
    except ValueError:
        x = -1.0
    if not 0.5 <= x <= 1.0:
        raise ApiError(400, "'fuzzy' must be true, false, or a similarity between 0.5 and 1.",
                       received=v)
    return x


def _language(inp):
    v = _s(inp, 'language') or store.listen_language()
    if v.lower() == 'auto':
        return 'auto'
    if not LANGUAGE_RE.match(v):
        raise ApiError(400, "'language' must be a language code such as en-AU, or auto.",
                       received=v)
    lang, place = v.split('-')
    return lang.lower() + '-' + place.upper()


def _hz(inp):
    v = _s(inp, 'hz')
    if v is None:
        return None
    try:
        hz = int(float(v))
    except ValueError:
        raise ApiError(400, "'hz' must be a whole number.", received=inp.get('hz'))
    if not va.LISTEN_HZ[0] <= hz <= va.LISTEN_HZ[1]:
        raise ApiError(400, "'hz' must be between %d and %d." % va.LISTEN_HZ, received=hz)
    return hz


def _named(inp, key_name='key', ref_name='ref'):
    """-> (key, ref): one of them, or neither."""
    key, ref = _s(inp, key_name), _s(inp, ref_name)
    if key and ref:
        raise ApiError(400, f"Pass '{key_name}' or '{ref_name}', not both.")
    if key and not KEY_RE.match(key):
        raise ApiError(400, f"'{key_name}' must be {KEY_RULE}.", received=key)
    if ref:
        store.check_ref(ref, ref_name)
    return key, ref


# ------------------------------------------------------ which audio is it ----
def _format(data, declared):
    """What POSTed (or saved) audio is: by its header, or - for audio with no
    header - because the caller says so."""
    declared = (declared or '').lower() or None
    if declared and declared not in DECLARABLE:
        raise ApiError(400, "Unknown 'format'.", received=declared, allowed=list(DECLARABLE))
    if declared in HEADERLESS:
        if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
            raise ApiError(400, "This audio is a WAV file, which says what it is: leave "
                                "'format' out. 'format' is for audio with no header.",
                           received=declared)
        return declared
    found = va.sniff(data)[0]
    if found == 'bin':
        raise ApiError(400, 'Cannot tell what this audio is: it has no header this service '
                            'knows.',
                       hint='for raw samples say what they are: format=pcm&hz=<sample rate> '
                            '(16-bit mono little-endian), or format=mulaw / format=alaw')
    if declared and declared != found:
        raise ApiError(400, "'format' does not match the audio.", received=declared,
                       detected=found,
                       hint="leave 'format' out for audio with a header; it is for headerless "
                            "audio (pcm, mulaw, alaw)")
    return found


def _audio(inp, posted, key_name='key', ref_name='ref'):
    """What is to be listened to. Nothing is spent here.

    -> {'heard': ref}                                  a listening that already has a ref
     | {'data', 'format', 'hz', 'trusted', 'ms', 'said'}   audio: POSTed, said (v...) or saved

    'trusted' is audio this service made itself (its length is known and it
    may be in a format a stranger's upload may not); 'said' is the text of an
    utterance, which is what a round-trip compare is made against."""
    key, ref = _named(inp, key_name, ref_name)
    if posted is not None:
        if key or ref:
            raise ApiError(400, "Send the audio as the request body, or name stored audio with "
                                "'ref' or 'key' - not both.")
        if len(posted) > MAX_AUDIO_KB * 1024:
            raise ApiError(400, 'The audio is larger than a request body may be.',
                           maxKb=MAX_AUDIO_KB, bytes=len(posted),
                           hint='send a shorter stretch, a lower sample rate (16000 Hz mono is '
                                'plenty for speech), or MP3')
        if not posted:
            raise ApiError(400, 'The audio is empty.')
        return {'data': posted, 'format': _format(posted, _s(inp, 'format')), 'hz': _hz(inp),
                'trusted': False}
    if ref and store.is_heard(ref):
        return {'heard': ref}
    if ref:
        meta, modified = store.load(store.GENERATED_META + ref + '.json')
        if meta is None:
            raise store.ref_not_found(ref)
        store.touch(ref, meta, modified)
        return {'data': store.read(store.GENERATED_AUDIO + ref), 'format': meta['format'],
                'hz': meta.get('hz'), 'trusted': True, 'ms': meta.get('durationMs'),
                'said': meta.get('text') or ''}
    if key:
        meta, _m = store.load(store.SAVED_META + key + '.json')
        if meta is None:
            raise store.key_not_found(key)
        data = store.read(store.SAVED_AUDIO + key)
        ours = meta.get('savedFrom') in ('generated', 'heard')
        declared = _s(inp, 'format')
        if ours or (meta.get('format') in HEADERLESS and not declared):
            fmt = meta['format']
        else:
            fmt = _format(data, declared)
        out = {'data': data, 'format': fmt, 'hz': _hz(inp) or meta.get('hz'), 'trusted': ours,
               'ms': meta.get('durationMs') if ours else None}
        if 'script' in meta:
            out['said'] = meta.get('text') or ''
        return out
    raise ApiError(400, 'Nothing to listen to.',
                   hint='POST the audio as the request body (raw bytes with Content-Type: '
                        'application/octet-stream, base64 text, or JSON with "audio" or '
                        '"chunks"), or name stored audio: ref (from voice/say or voice/listen) '
                        'or key (saved with voice/save)')


def _prepare(src):
    """The audio as it will be handed to the transcriber, and its length as
    COUNTED here - which is what the day's listening budget is spent on.

    -> {'audio', 'format' (wav | mp3 | ogg), 'hz', 'channels', 'ms', 'padded'}"""
    data, fmt, hz, channels = src['data'], src['format'], src.get('hz'), None
    if fmt == 'wav':
        try:
            audio, hz, channels, ms = va.wav_plain(data)
        except ValueError as e:
            raise ApiError(400, 'This WAV file cannot be listened to: %s.' % e)
    elif fmt in HEADERLESS:
        if fmt == 'pcm':
            if not hz:
                raise ApiError(400, "'hz' is required with format=pcm: raw samples do not say "
                                    "how fast they were recorded.",
                               hint='for example format=pcm&hz=24000 (16-bit mono little-endian)')
            pcm = data[:len(data) - len(data) % 2]
        else:
            hz = hz or 8000
            pcm = va.g711_to_pcm(data, fmt)
        if not pcm:
            raise ApiError(400, 'The audio is empty.')
        audio, channels, fmt = va.wav(pcm, hz), 1, 'wav'
        ms = int(round(len(pcm) * 500.0 / hz))
    elif fmt == 'mp3':
        clean = va.mp3_clean(data)
        if not clean:
            raise ApiError(400, 'No MP3 audio frames were found in this audio.')
        audio, ms, hz = clean
    elif fmt == 'ogg' and src.get('trusted'):
        # Made by voice/say, so its length is known and it is passed on as it
        # is. (Whether the transcriber takes Ogg *Vorbis* is for it to say: a
        # refusal comes back as a failed job, with a hint.)
        audio = data
        ms = src.get('ms') or va.ogg_ms(data) or len(data) // 2
    else:
        raise ApiError(400, 'voice/listen does not take %s audio.' % fmt.upper(),
                       detected=fmt, allowed=list(DECLARABLE),
                       reason='Transcription is billed by the second. The length of WAV, MP3 and '
                              'raw audio can be counted from the bytes; this format only states '
                              'its length, and can hold hours of silence in a few kilobytes.',
                       hint='convert it to 16-bit WAV first, for example: ffmpeg -i reply.%s '
                            '-ar 16000 -ac 1 reply.wav' % fmt)
    padded = 0
    if fmt == 'wav' and ms < MIN_MS:
        audio = va.lengthen_wav(audio, MIN_MS)
        longer = va.wav_info(audio)[3]
        ms, padded = longer, longer - ms
    return {'audio': audio, 'format': fmt, 'hz': hz, 'channels': channels, 'ms': ms,
            'padded': padded}


# ----------------------------------------------------- the transcription ----
def _code(e):
    return e.response.get('Error', {}).get('Code', '') if isinstance(e, ClientError) else ''


def _detail(e):
    return (e.response.get('Error', {}).get('Message') if isinstance(e, ClientError) else None) or str(e)


def _busy(code):
    return code == 'LimitExceededException' or 'Throttling' in code or 'TooManyRequests' in code


def _start_error(e):
    code = _code(e)
    if code in CALLER_ERRORS:
        return ApiError(400, 'Amazon Transcribe would not take this request.', detail=_detail(e),
                        code=code,
                        hint="check 'language': a code Amazon Transcribe supports, such as "
                             "en-AU, or auto")
    if _busy(code):
        return ApiError(429, 'Amazon Transcribe is busy. Try again shortly.', detail=_detail(e),
                        retryAfterSeconds=2)
    return ApiError(502, 'Amazon Transcribe could not start listening.', detail=_detail(e),
                    code=code or None)


def _job_name(ref):
    # The bucket is in the name because job names are per account and region,
    # and two deployments in one account may well hear the same audio.
    return '%s-%s' % (store.bucket(), ref)


def _job(name):
    """-> the job, or None when there is no job of that name."""
    try:
        return store.transcribe().get_transcription_job(
            TranscriptionJobName=name)['TranscriptionJob']
    except ClientError as e:
        if _code(e) in GONE:
            return None
        raise


def _delete_job(name):
    """Best effort. A finished job is cleared away at once: its listing is the
    one place the audio's existence would otherwise outlive the store."""
    try:
        store.transcribe().delete_transcription_job(TranscriptionJobName=name)
    except Exception:  # noqa: BLE001
        pass


def _start_job(ref, audio_key, media, language, again=True):
    """Start the job for this audio. -> True when one was already there and is
    being followed instead (so this request has not cost anything)."""
    name = _job_name(ref)
    kw = {'TranscriptionJobName': name, 'MediaFormat': media,
          'Media': {'MediaFileUri': 's3://%s/%s' % (store.bucket(), audio_key)},
          'OutputBucketName': store.bucket(), 'OutputKey': store.HEARD_RAW + ref + '.json'}
    if language == 'auto':
        kw['IdentifyLanguage'] = True
    else:
        kw['LanguageCode'] = language
    try:
        store.transcribe().start_transcription_job(**kw)
        return False
    except ClientError as e:
        if _code(e) != 'ConflictException':
            raise
    # A job of this name exists: another request got here first, or an old
    # one was never cleared away.
    job = _job(name)
    status = (job or {}).get('TranscriptionJobStatus')
    if status in ('QUEUED', 'IN_PROGRESS'):
        return True
    if status == 'COMPLETED' and store.exists(store.HEARD_RAW + ref + '.json'):
        return True
    if not again:
        raise ApiError(502, 'Amazon Transcribe could not start listening.',
                       detail='a transcription job of the same name is in the way', job=name)
    _delete_job(name)
    return _start_job(ref, audio_key, media, language, again=False)


def _start(ref, prepared, language):
    """Store the audio, count it against the day, start the job. -> meta (pending)."""
    media = prepared['format']
    audio_key = '%s%s.%s' % (store.HEARD_AUDIO, ref, media)
    seconds = max(1, int(math.ceil(prepared['ms'] / 1000.0)))
    store.reserve_seconds(seconds)
    try:
        store.put_audio(audio_key, prepared['audio'], va.CONTENT_TYPES[media],
                        '%s.%s' % (ref, media))
        followed = _start_job(ref, audio_key, media, language)
    except ApiError:
        store.refund_seconds(seconds)
        raise
    except Exception as e:  # noqa: BLE001
        store.refund_seconds(seconds)
        raise _start_error(e)
    if followed:
        store.refund_seconds(seconds)
    meta = {'ref': ref, 'state': 'pending', 'job': _job_name(ref), 'language': language,
            'format': media, 'hz': prepared['hz'], 'channels': prepared['channels'],
            'bytes': len(prepared['audio']), 'durationMs': prepared['ms'],
            'filename': '%s.%s' % (ref, media), 'contentType': va.CONTENT_TYPES[media],
            'audioKey': audio_key, 'seconds': seconds, 'createdTime': store.now(),
            'recipe': RECIPE}
    if prepared['padded']:
        meta['paddedMs'] = prepared['padded']
    store.put_json(store.HEARD_META + ref + '.json', meta)
    # One line per job: what the day's budget went on.
    print(json.dumps({'voice': 'listening', 'ref': ref, 'language': language, 'format': media,
                      'seconds': seconds, 'followed': followed}))
    return meta


def _finish(ref, meta, job):
    doc = store.load_bytes_json(store.HEARD_RAW + ref + '.json')
    if doc is None:
        raise ApiError(502, 'Amazon Transcribe finished, but its transcript is not in the store.',
                       ref=ref)
    facts = vw.digest(doc)
    spoken = facts.pop('language', None) or (job or {}).get('LanguageCode')
    meta = dict(meta, state='done', finishedTime=store.now(), **facts)
    if spoken:
        meta['heardLanguage'] = spoken
    store.put_json(store.HEARD_META + ref + '.json', meta)
    _delete_job(meta['job'])
    print(json.dumps({'voice': 'heard', 'ref': ref, 'words': len(meta['words']),
                      'confidence': meta.get('confidence'), 'durationMs': meta.get('durationMs'),
                      'tookSeconds': meta['finishedTime'] - meta.get('createdTime', 0)}))
    return meta


def _failed(ref, meta, reason):
    # The seconds are not handed back: a failure may be found by several
    # requests at once, and a refund from each would be a way to grow the
    # budget. Whatever Transcribe charges for a failed job, the day's count
    # errs on the careful side.
    meta = dict(meta, state='failed', finishedTime=store.now(),
                reason=reason or 'Amazon Transcribe gave no reason.')
    store.put_json(store.HEARD_META + ref + '.json', meta)
    _delete_job(meta['job'])
    print(json.dumps({'voice': 'not heard', 'ref': ref, 'reason': meta['reason']}))
    return meta


def _follow(ref, meta, deadline, mine=False):
    """Watch the job until it ends or the caller's wait runs out. -> meta

    The request that started the job asks Transcribe every second. Any other
    request for the same audio - there may be many, all waiting on one job -
    mostly watches the stored record instead, and asks Transcribe itself only
    now and then, in case the request that started it has gone away."""
    looks = 0
    while True:
        looks += 1
        if not mine and looks % ASK_EVERY != 1:
            fresh, _m = store.load(store.HEARD_META + ref + '.json')
            if fresh and fresh.get('state') != 'pending':
                return fresh
            if time.monotonic() + POLL_S > deadline:
                return meta
            time.sleep(POLL_S)
            continue
        try:
            job = _job(meta['job'])
        except ClientError as e:
            if not _busy(_code(e)):
                raise ApiError(502, 'Amazon Transcribe could not be asked how the job is going.',
                               detail=_detail(e), ref=ref)
            job = {'TranscriptionJobStatus': 'IN_PROGRESS'}
        except Exception as e:  # noqa: BLE001 - a timeout: ask again while there is time
            job = {'TranscriptionJobStatus': 'IN_PROGRESS', 'slow': str(e)}
        if job is None:
            # Finished and cleared away by another request between our two looks.
            fresh, _m = store.load(store.HEARD_META + ref + '.json')
            if fresh and fresh.get('state') != 'pending':
                return fresh
            if store.exists(store.HEARD_RAW + ref + '.json'):
                return _finish(ref, meta, None)
            return _failed(ref, meta, 'The transcription job is no longer there.')
        status = job.get('TranscriptionJobStatus')
        if status == 'COMPLETED':
            return _finish(ref, meta, job)
        if status == 'FAILED':
            return _failed(ref, meta, job.get('FailureReason'))
        if time.monotonic() + POLL_S > deadline:
            return meta
        time.sleep(POLL_S)


def _transcribe(src, language, deadline):
    """-> (meta, cached). Starts a job when this audio has not been heard in
    this language; follows one that is running until the deadline. cached
    means the words were already there."""
    if 'heard' in src:
        ref = src['heard']
        meta, _m = store.load(store.HEARD_META + ref + '.json')
        if meta is None:
            raise store.ref_not_found(ref)
    else:
        prepared = _prepare(src)
        ref = store.heard_ref(prepared['audio'], language, RECIPE)
        meta, _m = store.load(store.HEARD_META + ref + '.json')
        # Audio that failed before and is sent again is tried again.
        if meta is None or meta.get('state') == 'failed':
            return _follow(ref, _start(ref, prepared, language), deadline, mine=True), False
    if meta.get('state') == 'pending':
        return _follow(ref, meta, deadline), False
    return meta, True


def _unheard(meta):
    hint = ('hear what was sent with voice/get?ref=%s&play=true; send the audio again to try '
            'again' % meta['ref'])
    if meta.get('format') == 'ogg':
        hint = ('the transcriber may not take Ogg Vorbis: say the utterance again as WAV '
                '(voice/say?ref=<its ref>&format=wav) and listen to that')
    return ApiError(422, 'Amazon Transcribe could not make words of this audio.',
                    ref=meta['ref'], state='failed', detail=meta.get('reason'), hint=hint)


# -------------------------------------------------------------- responses ----
AUDIO_FACTS = ('format', 'hz', 'channels', 'bytes', 'durationMs', 'paddedMs')
HEARD_FACTS = ('speech', 'confidence', 'uncertain')
TIMING_FACTS = ('speechStartMs', 'speechEndMs', 'wordsPerMinute', 'longestPauseMs')


def _wait(inp):
    return _int(inp, 'wait', WAIT_DEFAULT_S, 0, WAIT_MAX_S)


def _waited(started):
    return int((time.monotonic() - started) * 1000)


def _pending(meta):
    return meta.get('state') == 'pending'


def _listen_url(req, ref):
    return '%s/voice/listen?ref=%s&wait=%d' % (base_url(req.event), ref, WAIT_MAX_S)


def _not_yet(req, out, ref, again):
    out['listenUrl'] = _listen_url(req, ref)
    out['comment'] = ('still being transcribed: %s until pending is false' % again)
    return out


def _pick(meta, names):
    return {k: meta[k] for k in names if meta.get(k) is not None}


# ---------------------------------------------------------------- listen ----
def listen(req):
    started = time.monotonic()
    inp, posted = _inputs(req, LISTEN_FIELDS)
    wait = _wait(inp)
    src = _audio(inp, posted)
    meta, cached = _transcribe(src, _language(inp), started + wait)
    if meta.get('state') == 'failed':
        raise _unheard(meta)
    ref, base = meta['ref'], base_url(req.event)
    out = {'ref': ref, 'cached': cached, 'pending': _pending(meta)}
    if out['pending']:
        out['text'] = None
    else:
        out.update(text=meta['text'], normalised=vw.normalised(meta['words']))
        out.update(_pick(meta, HEARD_FACTS))
    out['language'] = meta.get('heardLanguage') or meta.get('language')
    out.update(_pick(meta, AUDIO_FACTS))
    if not out['pending']:
        out.update(_pick(meta, TIMING_FACTS))
        out['words'] = vw.public_words(meta['words'])
    out['getUrl'] = '%s/voice/get?ref=%s' % (base, ref)
    out['playUrl'] = '%s/voice/get?ref=%s&play=true' % (base, ref)
    out['createdTime'] = meta.get('createdTime')
    if out['pending']:
        _not_yet(req, out, ref, 'call listenUrl (or send the same request again)')
    out['waitedMs'] = _waited(started)
    return respond(200, out)


# ------------------------------------------------------------------ find ----
def _scan(words, pattern, kind, exact, icase, context, limit):
    """A regex (or, for exact text, a literal) over the transcript -> (count,
    results, truncated), each result given back in heard words."""
    hay = vw.written(words) if exact else vw.normalised(words)
    try:
        count, found, truncated, _empty = mx.find_regex(
            hay, pattern, ctx=0, ignorecase=icase or not exact, maxresults=limit, kind=kind)
    except re.error as exc:
        raise ApiError(400, 'Invalid regular expression.', detail=str(exc), pattern=pattern)
    except mx.RegexTimeout:
        raise ApiError(400, 'Pattern took too long to evaluate and was abandoned.',
                       timeoutSeconds=mx.REGEX_TIMEOUT_S, pattern=pattern,
                       hint='nested quantifiers such as (a+)+ backtrack catastrophically; '
                            'anchor the pattern or use left/right instead')
    results = []
    for f in found:
        r = vw.at(words, f['offset'], len(f['matched']), context, exact) or {}
        if kind == 'regex':
            r['value'] = f['value']
            for k in ('groups', 'named'):
                if k in f:
                    r[k] = f[k]
        results.append(r)
    return count, results, truncated


def find(req):
    started = time.monotonic()
    inp, posted = _inputs(req, FIND_FIELDS)
    wait = _wait(inp)
    term, pattern = _text(inp, 'text'), _text(inp, 'regex')
    left, right = _text(inp, 'left'), _text(inp, 'right')
    modes = [m for m, v in (('words', term), ('regex', pattern), ('boundaries', left)) if v]
    if len(modes) > 1:
        raise ApiError(400, 'Choose one search mode.',
                       hint="use 'text', OR 'regex', OR 'left' (+ optional 'right')")
    if not modes:
        if right:
            raise ApiError(400, "'right' needs a 'left' marker.")
        raise ApiError(400, 'Missing search term.',
                       hint="pass 'text' / 'text64' for words that should have been said, "
                            "'regex' / 'regex64' for a pattern, or 'left' (+ optional 'right') "
                            "to capture what was said between two markers")
    mode = modes[0]
    if right and mode != 'boundaries':
        raise ApiError(400, "'right' is only used with 'left'.")
    exact, icase, fuzzy = _flag(inp, 'exact'), _flag(inp, 'ignorecase'), _fuzzy(inp)
    context = _int(inp, 'context', vw.CONTEXT_DEFAULT, 0, vw.CONTEXT_MAX)
    limit = _int(inp, 'limit', FIND_LIMIT_DEFAULT, 1, FIND_LIMIT_MAX)
    if exact and mode == 'boundaries':
        raise ApiError(400, "'exact' is not used with left/right: the markers are always "
                            "matched as words. Use regex with exact=true for the transcript "
                            "as written.")
    if fuzzy < 1.0 and (exact or mode == 'regex'):
        raise ApiError(400, "'fuzzy' applies to words (text, left, right): not to a regex, and "
                            "not with exact=true.")

    # Everything about the question is checked before any audio is paid for.
    if mode == 'words' and not exact:
        wanted = vw.canonical_text(term)
        if not wanted:
            raise ApiError(400, "'text' holds no words to look for.", received=term)
        match = {'mode': 'words', 'text': term, 'normalised': ' '.join(wanted), 'fuzzy': fuzzy,
                 'context': context}
    elif mode == 'words':
        mode, pattern = 'literal', re.escape(vw.clean(term))
        match = {'mode': 'literal', 'text': term, 'exact': True, 'ignoreCase': icase,
                 'context': context}
    elif mode == 'boundaries':
        if not vw.canonical_text(left) or (right and not vw.canonical_text(right)):
            raise ApiError(400, "'left' and 'right' must each hold at least one word.")
        match = {'mode': 'boundaries', 'left': left, 'right': right or None, 'fuzzy': fuzzy,
                 'context': context}
    else:
        match = {'mode': 'regex', 'regex': pattern, 'exact': exact,
                 'ignoreCase': icase or not exact, 'context': context}
    if mode in ('regex', 'literal'):
        if len(pattern) > mx.REGEX_MAX_LEN:
            raise ApiError(400, 'Pattern too long.', maxChars=mx.REGEX_MAX_LEN)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ApiError(400, 'Invalid regular expression.', detail=str(exc), pattern=pattern)

    meta, _cached = _transcribe(_audio(inp, posted), _language(inp), started + wait)
    if meta.get('state') == 'failed':
        raise _unheard(meta)
    ref = meta['ref']
    out = {'ref': ref, 'pending': _pending(meta), 'match': match}
    if out['pending']:
        out.update(found=None, count=None, results=[], text=None)
        _not_yet(req, out, ref, 'send this request again with ref=%s' % ref)
        out['waitedMs'] = _waited(started)
        return respond(200, out)

    words, empty = meta['words'], 0
    if mode == 'words':
        count, results = vw.find_text(words, term, fuzzy, context, limit)
        truncated = count > len(results)
    elif mode == 'boundaries':
        count, results, empty = vw.find_between(words, left, right, fuzzy, context, limit)
        truncated = count > len(results)
    else:
        count, results, truncated = _scan(words, pattern, mode, exact, icase, context, limit)
    out.update(found=count > 0, count=count, truncated=truncated, results=results)
    if mode in ('regex', 'boundaries'):
        # The captures on their own: what goes straight into an assertion or lists/push.
        out['values'] = [r.get('value') for r in results]
    if empty:
        out['emptyMatches'] = empty
    out.update(text=meta['text'], normalised=vw.normalised(words))
    out.update(_pick(meta, HEARD_FACTS))
    if count == 0:
        hints = []
        if not meta.get('speech'):
            hints.append('no speech was heard in this audio')
        elif mode in ('words', 'boundaries'):
            near = vw.closest(words, term if mode == 'words' else left)
            if near:
                out['closest'] = near
            if fuzzy == 1.0:
                if mode == 'words':
                    loose = vw.find_text(words, term, vw.FUZZY_DEFAULT, 0, 1)[0]
                else:
                    loose = vw.find_between(words, left, right, vw.FUZZY_DEFAULT, 0, 1)[0]
                if loose:
                    out['fuzzyCount'] = loose
                    hints.append('matches when fuzzy=true')
            if mode == 'boundaries' and empty:
                hints.append("'left' was heard, but nothing followed it")
        if meta.get('speech'):
            hints.append("exact=true searches 'text' as it was written; without it the words "
                         "are matched whatever their case, punctuation or spelling of numbers"
                         if exact else
                         "'text' is what was heard; 'normalised' is what is searched")
        out['hint'] = '; '.join(hints)
    out['waitedMs'] = _waited(started)
    return respond(200, out)


# --------------------------------------------------------------- compare ----
def compare(req):
    started = time.monotonic()
    inp, posted = _inputs(req, COMPARE_FIELDS)
    wait = _wait(inp)
    minimum = _ratio(inp, 'min')
    expected = _text(inp, 'text')
    okey, oref = _named(inp, 'otherkey', 'otherref')
    if sum(1 for v in (expected, oref, okey) if v) > 1:
        raise ApiError(400, "Pass one thing to compare with: 'text', 'otherref' or 'otherkey'.")
    src = _audio(inp, posted)
    other = _audio({'ref': oref, 'key': okey}, None) if (oref or okey) else None
    round_trip = False
    if other is None and expected is None:
        if src.get('said') is None:
            raise ApiError(400, "Missing 'text': what you expected to hear.",
                           hint="pass text / text64, or otherref / otherkey to compare with "
                                "other audio. Only an utterance made by voice/say (ref=v..., or "
                                "a key saved from one) can be compared with its own words")
        expected, round_trip = src['said'], True
    if other is not None and other.get('said') is not None:
        expected, other = other['said'], None
    if other is None and not vw.canonical_text(expected):
        raise ApiError(400, "'text' holds no words to compare with.", received=expected)

    language, deadline = _language(inp), started + wait
    meta, _cached = _transcribe(src, language, deadline)
    if meta.get('state') == 'failed':
        raise _unheard(meta)
    ref = meta['ref']
    out = {'ref': ref, 'pending': _pending(meta)}
    if other is not None:
        heard_other, _c = _transcribe(other, language, deadline)
        if heard_other.get('state') == 'failed':
            raise _unheard(heard_other)
        out['otherRef'] = heard_other['ref']
        if _pending(heard_other):
            out['pending'] = True
        else:
            expected = heard_other['text']
    if out['pending']:
        out.update(same=None, similarity=None, wordErrorRate=None, text=None)
        _not_yet(req, out, ref, 'send this request again')
        out['waitedMs'] = _waited(started)
        return respond(200, out)

    size = len(vw.canonical_text(expected)) * len(meta['words'])
    if size > MAX_COMPARE_CELLS:
        raise ApiError(400, 'Too long to compare word by word.', ref=ref,
                       expectedWords=len(vw.canonical_text(expected)),
                       heardWords=len(meta['words']),
                       hint='compare a shorter stretch, or use voice/find for the phrases '
                            'that matter')
    result = vw.compare(expected, meta['words'])
    if minimum is not None:
        out['pass'] = result['similarity'] >= minimum
        out['min'] = minimum
    if round_trip:
        out['roundTrip'] = True
    out.update(same=result['same'], similarity=result['similarity'],
               wordErrorRate=result['wordErrorRate'], expectedWords=result['expectedWords'],
               heardWords=result['heardWords'], differences=result['differences'],
               differencesTruncated=result['differencesTruncated'],
               expectedText=vw.clean(expected), text=meta['text'],
               expectedNormalised=result['expected'], normalised=result['heard'])
    out.update(_pick(meta, HEARD_FACTS))
    out['waitedMs'] = _waited(started)
    return respond(200, out)
