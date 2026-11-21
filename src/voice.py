"""voice - spoken utterances for testing voice interfaces.

  /v1/voice/say?text=When is my next appointment          text -> audio (cached by content)
  /v1/voice/say?text=...&emotion=anxious&format=wav&hz=16000
  /v1/voice/say?text=Hello&play=true                      a page that plays it, with no player
  POST /v1/voice/say   {"segments": [...], "pronounce": {...}, "strict": true}
  /v1/voice/get?ref=REF | key=KEY                          stored audio, no synthesis
  /v1/voice/save?key=KEY&ref=REF                           pin an utterance (POST a body: a recording)
  /v1/voice/delete?key=KEY
  /v1/voice/voices     /v1/voice/tags
  POST /v1/voice/listen                                    audio -> the words in it (voice_listen)
  /v1/voice/find?ref=REF&text=...   /v1/voice/compare?ref=REF&text=...

Every say request is reduced to a canonical form and hashed; the hash is the
ref and the audio is stored under it. The same request is therefore the same
audio, byte for byte, for as long as it is stored - replay comes from the
store, never from asking the engine again (a generative voice need not say
the same thing the same way twice, and is upgraded under you).

The modules: voice_script (tags -> script -> SSML, and the honest report),
voice_audio (WAV, durations), voice_store (S3, Polly, the spend guards),
voice_page (the play=true page), voice_listen and voice_words (hearing: audio
-> transcript, and finding and comparing what was said).
"""
import base64
import hashlib
import json
import mimetypes
import re
import urllib.parse

from botocore.exceptions import ClientError

from common import FALSE, KEY_RE, KEY_RULE, TRUE, ApiError, b64decode, base_url, raw, respond, run
import voice_audio as va
import voice_listen
import voice_page
import voice_script as vs
import voice_store as store

MAX_INLINE_KB = 4096      # above this, audio comes back as a presigned URL (6 MB Lambda response cap)
MAX_SAVED_KB = 4096       # a recording POSTed to voice/save (API Gateway + Lambda payload caps)

SAY_FIELDS = ('text', 'text64', 'segments', 'voice', 'language', 'engine', 'format', 'hz',
              'emotion', 'seed', 'strict', 'pronounce', 'ref', 'key', 'url', 'view', 'download',
              'play')

# Polly errors the caller can fix, as opposed to Polly being unwell.
CALLER_ERRORS = ('TextLengthExceededException', 'InvalidSsmlException',
                 'InvalidSampleRateException', 'EngineNotSupportedException',
                 'LanguageNotSupportedException', 'ValidationException')


# ---------------------------------------------------------------- inputs ----
def _inputs(req):
    """Query parameters, overlaid with the POST body: a JSON object's fields,
    or - for any other body - the body as the text."""
    inp = dict(req.qs)
    if req.method == 'POST':
        body = req.body_bytes().decode('utf-8', 'replace').strip()
        if body[:1] == '{':
            try:
                doc = json.loads(body)
            except ValueError as e:
                raise ApiError(400, 'The request body is not valid JSON.', detail=str(e))
            if not isinstance(doc, dict):
                raise ApiError(400, 'The request body must be a JSON object.')
            doc = {str(k).lower(): v for k, v in doc.items()}
            unknown = sorted(set(doc) - set(SAY_FIELDS))
            if unknown:
                raise ApiError(400, 'Unknown fields in the request body.', received=unknown,
                               allowed=list(SAY_FIELDS))
            inp.update(doc)
        elif body:
            form = {}
            if 'x-www-form-urlencoded' in (req.headers.get('content-type') or '').lower():
                # What curl -d and an HTML form send. Fields we know mean a form;
                # anything else is just text that curl labelled as one.
                form = {k.lower(): v[-1] for k, v in
                        urllib.parse.parse_qs(body, keep_blank_values=True).items()}
            if form and set(form) <= set(SAY_FIELDS):
                inp.update(form)
            else:
                inp['text'] = body
                inp.pop('text64', None)
    return inp


def _s(inp, name):
    v = inp.get(name)
    if v is None or isinstance(v, (dict, list)) or str(v).strip() == '':
        return None
    return str(v).strip()


def _b(inp, name, default=False):
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


def _one_of_key_ref(inp):
    key, ref = _s(inp, 'key'), _s(inp, 'ref')
    if key and ref:
        raise ApiError(400, "Pass 'key' (saved audio) or 'ref' (a generated utterance), not both.")
    if not key and not ref:
        raise ApiError(400, "Identify the audio with 'ref' (from voice/say or voice/listen) or "
                            "'key' (saved with voice/save).")
    if key and not KEY_RE.match(key):
        raise ApiError(400, f"'key' must be {KEY_RULE}.", received=key)
    if ref:
        store.check_ref(ref)
    return key, ref


# ------------------------------------------------------- say: resolving ----
def _source(inp):
    """say?ref= / say?key=: a stored utterance to say again (other voice,
    engine or format). -> its meta, or None for a new utterance."""
    if not _s(inp, 'ref') and not _s(inp, 'key'):
        return None
    clash = [n for n in ('text', 'text64', 'segments', 'emotion', 'pronounce')
             if inp.get(n) not in (None, '')]
    if clash:
        raise ApiError(400, "To say a stored utterance again, pass 'ref' (or 'key') with any of "
                            "voice, language, engine, format, hz. Its words and direction are "
                            "already stored.", received=clash)
    key, ref = _one_of_key_ref(inp)
    if ref and store.is_heard(ref):
        raise ApiError(400, 'That ref is audio that was heard (voice/listen), not an utterance: '
                            'there is no script to say again. voice/get plays it as it is.',
                       ref=ref)
    if ref:
        meta, _m = store.load(store.GENERATED_META + ref + '.json')
        if meta is None:
            raise store.ref_not_found(ref)
        return meta
    meta, _m = store.load(store.SAVED_META + key + '.json')
    if meta is None:
        raise store.key_not_found(key)
    if 'script' not in meta:
        raise ApiError(400, 'That key holds a recording, not a generated utterance: there is '
                            'no script to say again.', key=key)
    return meta


def _script(inp, source):
    """-> (script, pronounce)."""
    if source:
        return source['script'], source.get('pronounce') or {}
    text = None
    if _s(inp, 'text64'):
        try:
            text = b64decode(inp['text64']).decode('utf-8')
        except UnicodeDecodeError:
            raise ApiError(400, "'text64' does not decode to UTF-8 text.")
    elif inp.get('text') not in (None, ''):
        text = inp['text']
        if not isinstance(text, str):
            raise ApiError(400, "'text' must be a string.")
    segments = inp.get('segments')
    if isinstance(segments, str):       # a query-string 'segments' makes no sense
        raise ApiError(400, "'segments' is an array in a POSTed JSON body.")
    if text is not None and segments is not None:
        raise ApiError(400, "Pass 'text' or 'segments', not both.")
    if text is None and segments is None:
        raise ApiError(400, "Missing 'text' (or 'text64'): the words to say.",
                       hint='voice/say?text=Hello, or POST a JSON body with text or segments')
    pronounce = vs.check_pronounce(inp.get('pronounce'))
    emotion = inp.get('emotion')
    if text is not None and vs.is_ssml(text):
        if emotion not in (None, '') or pronounce:
            raise ApiError(400, "A complete <speak> document is passed to the engine as "
                                "written: 'emotion' and 'pronounce' do not apply to it.")
        return vs.parse_ssml(text), {}
    return vs.parse(text=text, segments=segments, emotion=emotion), pronounce


def _engine(inp, source):
    engine = (_s(inp, 'engine') or (source or {}).get('engine') or store.default_engine()).lower()
    if engine not in vs.ENGINES:
        raise ApiError(400, "Unknown 'engine'.", received=engine, allowed=list(vs.ENGINES))
    return engine


def _format(inp, engine, source):
    fmt = (_s(inp, 'format') or (source or {}).get('format') or 'mp3').lower()
    if fmt not in va.FORMATS:
        raise ApiError(400, "Unknown 'format'.", received=fmt, allowed=sorted(va.FORMATS))
    valid = va.FORMATS[fmt][3]
    hz = _s(inp, 'hz')
    if hz is None:
        kept = (source or {}).get('hz')
        return fmt, kept if kept in valid else va.default_hz(fmt, engine)
    try:
        hz = int(float(hz))
    except ValueError:
        raise ApiError(400, "'hz' must be a whole number.", received=inp.get('hz'))
    if hz not in valid:
        raise ApiError(400, f"'hz' for {fmt} must be one of the allowed sample rates.",
                       received=hz, allowed=list(valid))
    return fmt, hz


def _polly_error(e, engine, passthrough):
    code = e.response.get('Error', {}).get('Code', '') if isinstance(e, ClientError) else ''
    detail = (e.response.get('Error', {}).get('Message') if isinstance(e, ClientError) else None) or str(e)
    if code in CALLER_ERRORS:
        hint = ('check the SSML against the tags this engine supports' if passthrough else
                'the engine refused this direction: try fewer tags or another engine')
        return ApiError(400, 'Amazon Polly would not say this.', detail=detail, code=code,
                        engine=engine, hint=hint)
    if 'Throttling' in code or 'TooManyRequests' in code:
        return ApiError(429, 'Amazon Polly is throttling requests. Try again shortly.',
                        detail=detail, retryAfterSeconds=2)
    return ApiError(502, 'Amazon Polly could not produce the audio.', detail=detail,
                    code=code or None, region=store.region())


def _synthesise(ref, script, pronounce, tags, engine, voice, code, fmt, hz):
    """Ask Polly, post-process, store audio then meta. -> meta."""
    polly_format, content_type, ext = va.FORMATS[fmt][:3]
    doc, lead, tail = vs.ssml(script['items'], engine, pronounce, trim=fmt in ('wav', 'pcm'))
    passthrough = 'ssml' in script['items'][0]
    characters = len(script['text'])
    store.reserve(characters)
    try:
        kw = {'Engine': engine, 'VoiceId': voice['voice'], 'OutputFormat': polly_format,
              'SampleRate': str(hz), 'Text': doc, 'TextType': 'ssml'}
        if code:
            kw['LanguageCode'] = code
        res = store.polly().synthesize_speech(**kw)
        audio = res['AudioStream'].read()
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        store.refund(characters)
        raise _polly_error(e, engine, passthrough)
    if not audio:
        store.refund(characters)
        raise ApiError(502, 'Amazon Polly returned no audio.', region=store.region())
    if fmt in ('wav', 'pcm'):
        # Edge pauses as exact digital silence (see voice_script.ssml).
        audio = va.silence(lead, hz) + audio + va.silence(tail, hz)
        if fmt == 'wav':
            audio = va.wav(audio, hz)
    filename = '%s.%s' % (ref, ext)
    try:
        billed = int(res.get('RequestCharacters') or characters)
    except (TypeError, ValueError):
        billed = characters
    meta = {
        'ref': ref, 'text': script['text'], 'voice': voice['voice'],
        'language': code or voice['language'], 'engine': engine, 'format': fmt, 'hz': hz,
        'bytes': len(audio), 'durationMs': va.duration_ms(audio, fmt, hz),
        'characters': billed, 'tags': tags, 'ssml': doc, 'filename': filename,
        'contentType': content_type, 'createdTime': store.now(),
        # What is needed to say it again, on any engine, without the caller's help.
        'script': script, 'pronounce': pronounce, 'languageCode': code, 'recipe': vs.RECIPE,
    }
    store.put_audio(store.GENERATED_AUDIO + ref, audio, content_type, filename)
    store.put_json(store.GENERATED_META + ref + '.json', meta)
    # One line per synthesis: what the day's budget went on.
    print(json.dumps({'voice': 'synthesised', 'ref': ref, 'engine': engine,
                      'voiceName': voice['voice'], 'format': fmt, 'hz': hz,
                      'characters': billed, 'durationMs': meta['durationMs'],
                      'tags': len(tags)}))
    return meta


# -------------------------------------------------------------- responses ----
PUBLIC = ('text', 'voice', 'language', 'engine', 'format', 'hz', 'channels', 'bytes',
          'durationMs', 'characters', 'tags', 'ssml', 'filename', 'contentType', 'sha256',
          'createdTime', 'savedTime')


def _public(meta, **first):
    out = dict(first)
    out.update({k: meta[k] for k in PUBLIC if meta.get(k) is not None})
    return out


def _delivery(inp):
    """How the caller wants the audio: 'play', 'view', 'download', 'url' or
    'inline'. Read before any work is done, so a contradictory request costs
    nothing."""
    asked = [n for n in ('play', 'view', 'download') if _b(inp, n)]
    if len(asked) > 1:
        raise ApiError(400, "Pass one of 'play', 'view' or 'download', not several.",
                       received=asked)
    return asked[0] if asked else 'url' if _b(inp, 'url') else 'inline'


def _deliver(how, out, s3key, meta):
    """The audio itself, the way mail/attachments/get serves a file: inline
    base64, a presigned URL (url=true, or too big to inline), or a redirect a
    browser can follow (view=true shows the browser's player, download=true
    saves it) - and, for voice only, play=true: a page that plays it from
    script, with no player on screen."""
    filename = meta.get('filename') or 'audio'
    ctype = meta.get('contentType') or 'application/octet-stream'
    if how == 'play':
        page, headers = voice_page.render(out, store.presigned(s3key, filename, ctype))
        return raw(200, page, 'text/html; charset=utf-8', headers)
    if how in ('view', 'download'):
        url = store.presigned(s3key, filename, ctype, 'inline' if how == 'view' else 'attachment')
        return raw(302, '', 'text/plain; charset=utf-8', {'Location': url})
    if how == 'url' or (meta.get('bytes') or 0) > MAX_INLINE_KB * 1024:
        out['url'] = store.presigned(s3key, filename, ctype)
        out['expiresInSeconds'] = store.PRESIGN_TTL
        return respond(200, out)
    try:
        out['data'] = base64.b64encode(store.read(s3key)).decode('ascii')
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') not in ('NoSuchKey', '404', 'NotFound'):
            raise
        raise ApiError(404, 'That audio is no longer stored.',
                       **{k: out[k] for k in ('ref', 'key') if k in out})
    return respond(200, out)


def _get_url(req, **params):
    return '%s/voice/get?%s' % (base_url(req.event), urllib.parse.urlencode(params))


# ------------------------------------------------------------------- say ----
def say(req):
    inp = _inputs(req)
    how = _delivery(inp)
    source = _source(inp)
    script, pronounce = _script(inp, source)
    engine = _engine(inp, source)
    fmt, hz = _format(inp, engine, source)
    seed = _s(inp, 'seed')
    want = _s(inp, 'voice') or (source or {}).get('voice')
    language = _s(inp, 'language') or (None if _s(inp, 'voice') else (source or {}).get('languageCode'))
    voice, code, chosen = store.pick_voice(want, language, engine, seed)

    tags = vs.report(script['tags'], engine)
    if _b(inp, 'strict'):
        silent = [t for t in tags if t['rendered'] == 'none']
        if silent:
            raise ApiError(422, "strict: %d tag(s) would not be heard on the '%s' engine."
                           % (len(silent), engine), tags=silent, engine=engine,
                           hint='drop strict, remove those tags, or see voice/tags for an '
                                'engine that renders them')

    # Everything that decides the audio, and nothing else.
    ref = store.ref_of({'recipe': vs.RECIPE, 'engine': engine, 'voice': voice['voice'],
                        'language': code, 'format': fmt, 'hz': hz, 'script': script['items'],
                        'pronounce': sorted(pronounce.items())})
    meta, modified = store.load(store.GENERATED_META + ref + '.json')
    cached = meta is not None
    if cached:
        store.touch(ref, meta, modified)
    else:
        meta = _synthesise(ref, script, pronounce, tags, engine, voice, code, fmt, hz)
    out = _public(meta, ref=ref, cached=cached)
    out['getUrl'] = _get_url(req, ref=ref)
    if chosen:
        out['seed'] = seed
    return _deliver(how, out, store.GENERATED_AUDIO + ref, meta)


# ------------------------------------------------------------------- get ----
def get(req):
    key, ref = _one_of_key_ref(req.qs)
    how = _delivery(req.qs)
    if ref and store.is_heard(ref):
        # Audio given to voice/listen, as it was handed to the transcriber
        # (raw samples come back as a WAV, so they can be played).
        meta, _m = store.load(store.HEARD_META + ref + '.json')
        if meta is None:
            raise store.ref_not_found(ref)
        return _deliver(how, _public(meta, ref=ref, source='heard'), meta['audioKey'], meta)
    if ref:
        meta, modified = store.load(store.GENERATED_META + ref + '.json')
        if meta is None:
            raise store.ref_not_found(ref)
        store.touch(ref, meta, modified)
        return _deliver(how, _public(meta, ref=ref, source='generated'),
                        store.GENERATED_AUDIO + ref, meta)
    meta, _m = store.load(store.SAVED_META + key + '.json')
    if meta is None:
        raise store.key_not_found(key)
    out = _public(meta, key=key, source='saved', savedFrom=meta.get('savedFrom') or 'upload')
    if meta.get('ref'):
        out['ref'] = meta['ref']
    return _deliver(how, out, store.SAVED_AUDIO + key, meta)


# ------------------------------------------------------------------ save ----
def _safe_name(name):
    return re.sub(r'[^A-Za-z0-9._-]', '_', name.replace('\\', '/').rsplit('/', 1)[-1]).lstrip('._-')


def _named(key, ext):
    """A filename for audio saved under key: the key, with the right extension."""
    name = _safe_name(key) or 'audio'
    return name if name.lower().endswith('.' + ext) else '%s.%s' % (name, ext)


def _body_bytes(req):
    """The POSTed recording: raw bytes (application/octet-stream) or base64 text."""
    ctype = (req.headers.get('content-type') or '').split(';')[0].strip().lower()
    if ctype == 'application/octet-stream':
        data = req.body_bytes()
        if not data:
            raise ApiError(400, 'Request body is empty.')
        return data, 'binary'
    body = ''.join(req.body_bytes().decode('ascii', 'replace').split())
    if not body:
        raise ApiError(400, 'Request body must be the audio: raw bytes with Content-Type: '
                            'application/octet-stream, or the base64 of the file.')
    if not re.fullmatch(r'[A-Za-z0-9+/=_-]+', body):
        raise ApiError(400, 'The body is not base64. To send the raw file, set Content-Type: '
                            'application/octet-stream; otherwise send the base64 of the file.',
                       received=ctype or None)
    data = b64decode(body)
    if not data:
        raise ApiError(400, 'The audio is empty after base64 decode.')
    return data, 'base64'


def save(req):
    key = req.key('key')
    ref = req.text('ref')
    if ref and store.is_heard(store.check_ref(ref)):
        # Keep what was heard: the audio, and the words if there are any yet.
        meta, _m = store.load(store.HEARD_META + ref + '.json')
        if meta is None:
            raise store.ref_not_found(ref)
        saved = {k: meta[k] for k in ('ref', 'text', 'language', 'format', 'hz', 'channels',
                                      'bytes', 'durationMs', 'contentType')
                 if meta.get(k) is not None}
        saved.update(key=key, savedFrom='heard', savedTime=store.now(),
                     filename=_named(key, meta['format']))
        store.copy(meta['audioKey'], store.SAVED_AUDIO + key, meta['contentType'],
                   saved['filename'])
        store.put_json(store.SAVED_META + key + '.json', saved)
        out = _public(saved, key=key, saved=True, source='heard', ref=ref)
        out['getUrl'] = _get_url(req, key=key)
        return respond(200, out)
    if ref:
        # Pin: copy a generated utterance into the saved store, S3 to S3.
        store.check_ref(ref)
        meta, _m = store.load(store.GENERATED_META + ref + '.json')
        if meta is None:
            raise store.ref_not_found(ref)
        ext = va.FORMATS[meta['format']][2]
        saved = dict(meta, key=key, savedFrom='generated', savedTime=store.now(),
                     filename=_named(key, ext))
        store.copy(store.GENERATED_AUDIO + ref, store.SAVED_AUDIO + key,
                   meta['contentType'], saved['filename'])
        store.put_json(store.SAVED_META + key + '.json', saved)
        out = _public(saved, key=key, saved=True, source='generated', ref=ref)
        out['getUrl'] = _get_url(req, key=key)
        return respond(200, out)

    if req.method != 'POST':
        raise ApiError(400, "Pass 'ref' to save an utterance (or audio that was heard), or POST "
                            "a recording as the request body (raw bytes with Content-Type: "
                            "application/octet-stream, or base64 text).")
    data, body_format = _body_bytes(req)
    if len(data) > MAX_SAVED_KB * 1024:
        raise ApiError(400, 'The recording is larger than a request body may be.',
                       maxKb=MAX_SAVED_KB, bytes=len(data))
    given = req.text('filename')
    if given and len(given) > 255:
        raise ApiError(400, "'filename' must be 255 characters or fewer.")
    fmt, ctype, ext = va.sniff(data, given or key)
    filename = (_safe_name(given) if given else '') or _named(key, ext)
    if fmt == 'bin':
        ctype = mimetypes.guess_type(filename)[0] or ctype
    saved = {'key': key, 'savedFrom': 'upload', 'savedTime': store.now(), 'format': fmt,
             'bytes': len(data), 'filename': filename, 'contentType': ctype,
             'sha256': hashlib.sha256(data).hexdigest()}
    info = va.wav_info(data) if fmt == 'wav' else None
    if info:
        saved.update(hz=info[0], channels=info[1], durationMs=info[3])
    else:
        saved['durationMs'] = va.duration_ms(data, fmt)
    store.put_audio(store.SAVED_AUDIO + key, data, ctype, filename)
    store.put_json(store.SAVED_META + key + '.json', saved)
    out = _public(saved, key=key, saved=True, source='upload', bodyFormat=body_format)
    out['getUrl'] = _get_url(req, key=key)
    if fmt == 'bin':
        out['hint'] = ('not a format this service recognises, so it is stored and served as it '
                       'came; name it with filename=<name>.<extension>')
    return respond(200, out)


# ---------------------------------------------------------------- delete ----
def delete(req):
    key = req.key('key')
    audio, meta = store.SAVED_AUDIO + key, store.SAVED_META + key + '.json'
    existed = store.exists(meta) or store.exists(audio)
    store.delete(audio)
    store.delete(meta)
    return respond(200, {'key': key, 'deleted': existed})


# ------------------------------------------------------- voices and tags ----
def voices(req):
    engine = (req.text('engine') or '').lower() or None
    if engine and engine not in vs.ENGINES:
        raise ApiError(400, "Unknown 'engine'.", received=engine, allowed=list(vs.ENGINES))
    language = req.text('language')
    lang = language.lower() if language else None
    rows = [v for v in store.voices()
            if (not engine or engine in v['engines']) and (not lang or store._speaks(v, lang))]
    try:
        v, _code, _chosen = store.pick_voice(None, None, store.default_engine())
        default = {'voice': v['voice'], 'language': v['language'],
                   'engine': store.default_engine()}
    except ApiError:
        default = {'voice': store.default_voice(), 'engine': store.default_engine()}
    return respond(200, {'region': store.region() or None, 'default': default,
                         'engines': vs.CAPABILITY, 'count': len(rows), 'voices': rows})


def tags(req):
    return respond(200, {
        'syntax': [
            'Put a tag in square brackets where the delivery should change: '
            '[anxious] I have been waiting. [pause] Is the doctor late?',
            'A tag holds until the next tag of the same kind (emotion, pace, volume) or the '
            'end; [neutral] clears all three.',
            'Tags are never spoken. One this service does not know is kept in the stored '
            'script and reported as rendered: none.',
            'The emotion parameter is the same as one emotion tag at the very start.',
            'Write \\[ for a bracket that should be read as text.',
        ],
        'rendered': {
            'native': 'the engine does exactly this',
            'approximated': 'the nearest thing the engine has (a change of rate, pitch or volume)',
            'inferred': 'left to the engine, which reads emotion from the wording',
            'none': 'not audible; with strict=true the request is refused (422) instead',
        },
        'engines': vs.CAPABILITY,
        'tags': vs.vocabulary(),
    })


ROUTES = {'say': say, 'get': get, 'save': save, 'delete': delete, 'voices': voices, 'tags': tags,
          'listen': voice_listen.listen, 'find': voice_listen.find,
          'compare': voice_listen.compare}

INDEX = {
    'description': 'Both halves of a spoken conversation, for testing voice interfaces. say: '
                   'text (with [tags] for emotion, pace and pauses) to audio, stored by content '
                   'so every request can be replayed byte for byte. listen: audio to the words '
                   'in it, with find and compare to assert on what was said.',
}


def handler(event, context):
    return run(event, 'voice', ROUTES, index=INDEX)
