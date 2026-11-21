"""voice_store - configuration, AWS clients, the audio store and the spend guards.

One bucket (VOICE_BUCKET), three stores, mirroring mail attachments:

* GENERATED  generated/audio/<ref> + generated/meta/<ref>.json
             ref = hash of the whole request, so the same request always
             lands on the same object: repeats are free and byte-identical.
             A lifecycle rule expires generated/ after REF_DAYS; every use of
             an utterance older than a day rewrites it in place, so the clock
             runs from its LAST use.
* SAVED      saved/audio/<key> + saved/meta/<key>.json
             caller-chosen key, kept until deleted: a pinned utterance or an
             uploaded recording.
* HEARD      heard/audio/<ref>.<ext> + heard/meta/<ref>.json + heard/raw/<ref>.json
             audio given to voice/listen and what was made of it. ref = hash
             of the audio and the language it was listened to in, so the same
             audio is the same transcript, once. Expires after HEARD_DAYS: it
             is evidence from one run, and it holds whatever the system under
             test said.

A ref says which store it lives in: v... was said (generated), h... was heard.

The meta object's existence is what makes an utterance real: audio is written
first, meta last.

Speech comes from Amazon Polly in VOICE_REGION, which need not be the stack's
region (the neural and generative engines are not offered everywhere).
Transcripts come from Amazon Transcribe in the stack's OWN region: a
transcription job reads its audio from S3, and the bucket has to be in the
region the job runs in.
"""
import hashlib
import json
import os
import random
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common import ApiError
from voice_script import ENGINES

GENERATED_AUDIO = 'generated/audio/'
GENERATED_META = 'generated/meta/'
SAVED_AUDIO = 'saved/audio/'
SAVED_META = 'saved/meta/'
HEARD_AUDIO = 'heard/audio/'
HEARD_META = 'heard/meta/'
HEARD_RAW = 'heard/raw/'

REF_RE = re.compile(r'^[vh][a-z0-9]{11}$')
REF_DAYS = 30             # keep in step with the bucket's lifecycle rule on generated/
HEARD_DAYS = 30           # ... and with its rule on heard/
TOUCH_AFTER = 86400       # refresh an utterance's expiry at most once a day
PRESIGN_TTL = 900         # seconds; well inside the Lambda role's credential lifetime
VOICES_TTL = 6 * 3600     # the Polly voice list is re-read this often per container
BUDGET_KEEP_DAYS = 90

_clients = {}
_catalogue = {'at': 0.0, 'voices': None}


def env(name, default=''):
    return os.environ.get(name, default)


def bucket():
    return env('VOICE_BUCKET')


def region():
    return env('VOICE_REGION') or env('AWS_REGION') or env('AWS_DEFAULT_REGION')


def default_voice():
    return env('VOICE_NAME') or 'Olivia'


def default_engine():
    e = (env('VOICE_ENGINE') or 'neural').strip().lower()
    return e if e in ENGINES else 'neural'


def daily_characters():
    try:
        return int(env('VOICE_DAILY_CHARACTERS') or 25000)
    except ValueError:
        return 25000


def listen_language():
    return env('VOICE_LANGUAGE') or 'en-AU'


def daily_listen_seconds():
    try:
        return int(env('VOICE_DAILY_LISTEN_SECONDS') or 1800)
    except ValueError:
        return 1800


def now():
    return int(time.time())


# -------------------------------------------------------------- clients ----
def s3():
    # Regional endpoint and SigV4, for the reasons given in mail_store.s3():
    # a presigned URL for a bucket outside us-east-1 must name the regional
    # host, or S3 answers with a redirect a browser will not follow.
    if 's3' not in _clients:
        reg = env('AWS_REGION') or env('AWS_DEFAULT_REGION')
        _clients['s3'] = boto3.client(
            's3', region_name=reg or None,
            endpoint_url=('https://s3.%s.amazonaws.com' % reg) if reg else None,
            config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
    return _clients['s3']


def polly():
    if 'polly' not in _clients:
        _clients['polly'] = boto3.client(
            'polly', region_name=region() or None,
            # Two attempts at most, each bounded: a slow Polly must surface as our
            # own 502, not as API Gateway's 29 s timeout.
            config=Config(connect_timeout=3, read_timeout=12,
                          retries={'max_attempts': 2, 'mode': 'standard'}))
    return _clients['polly']


def transcribe():
    if 'transcribe' not in _clients:
        reg = env('AWS_REGION') or env('AWS_DEFAULT_REGION')
        _clients['transcribe'] = boto3.client(
            'transcribe', region_name=reg or None,
            # One attempt, tightly bounded: a job is polled anyway, and the
            # longest wait (20 s) plus one hung call must still fit inside
            # API Gateway's 29 s.
            config=Config(connect_timeout=2, read_timeout=4,
                          retries={'max_attempts': 1, 'mode': 'standard'}))
    return _clients['transcribe']


def table():
    if 'table' not in _clients:
        _clients['table'] = boto3.resource('dynamodb').Table(env('VOICE_TABLE'))
    return _clients['table']


def reset_clients():
    """For tests: drop cached clients and the voice list."""
    _clients.clear()
    _catalogue.update(at=0.0, voices=None)


# ----------------------------------------------------------------- refs ----
def _base36(hexdigest, length=11):
    n = int(hexdigest, 16)
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while len(out) < length:
        n, r = divmod(n, 36)
        out += digits[r]
    return out


def ref_of(canonical):
    """The ref for a canonical say request: 'v' + 11 base-36 characters of its SHA-256."""
    blob = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return 'v' + _base36(hashlib.sha256(blob.encode('utf-8')).hexdigest())


def heard_ref(audio, language, recipe):
    """The ref for one listening: 'h' + 11 base-36 characters of the SHA-256 of
    the audio itself and of how it is listened to."""
    h = hashlib.sha256(audio)
    h.update(('|%s|%s' % (language, recipe)).encode('utf-8'))
    return 'h' + _base36(h.hexdigest())


def check_ref(ref, name='ref'):
    if not REF_RE.match(ref or ''):
        raise ApiError(400, "'%s' must be a ref returned by voice/say (v...) or voice/listen "
                            "(h...): one letter followed by 11 letters and digits." % name,
                       received=ref)
    return ref


def is_heard(ref):
    return bool(ref) and ref[0] == 'h'


def ref_not_found(ref):
    if is_heard(ref):
        return ApiError(404, 'Nothing heard under that ref (heard audio is kept %d days).'
                        % HEARD_DAYS, ref=ref,
                        hint='POST the audio to voice/listen again; pin one for good with '
                             'voice/save')
    return ApiError(404, 'No utterance for that ref (generated audio is kept %d days after '
                         'its last use).' % REF_DAYS, ref=ref,
                    hint='say it again with voice/say; pin one for good with voice/save')


def key_not_found(key):
    return ApiError(404, 'No saved audio under that key.', key=key,
                    hint='save an utterance with voice/save?key=...&ref=..., or POST a '
                         'recording to voice/save?key=...')


# ------------------------------------------------------------ S3 access ----
def load(s3key):
    """(parsed JSON, LastModified epoch) or (None, None)."""
    try:
        obj = s3().get_object(Bucket=bucket(), Key=s3key)
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound'):
            return None, None
        raise
    modified = obj.get('LastModified')
    return json.loads(obj['Body'].read().decode('utf-8')), (modified.timestamp() if modified else None)


def put_json(s3key, obj):
    s3().put_object(Bucket=bucket(), Key=s3key, ContentType='application/json',
                    Body=json.dumps(obj, ensure_ascii=False).encode('utf-8'))


def load_bytes_json(s3key):
    """A JSON object stored by someone else (Transcribe's output), or None."""
    try:
        return json.loads(read(s3key).decode('utf-8'))
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound'):
            return None
        raise


def put_audio(s3key, data, content_type, filename):
    s3().put_object(Bucket=bucket(), Key=s3key, Body=data, ContentType=content_type,
                    Metadata={'filename': filename})


def read(s3key):
    return s3().get_object(Bucket=bucket(), Key=s3key)['Body'].read()


def exists(s3key):
    try:
        s3().head_object(Bucket=bucket(), Key=s3key)
        return True
    except ClientError:
        return False


def copy(src, dst, content_type=None, filename=None):
    kw = {'Bucket': bucket(), 'Key': dst, 'CopySource': {'Bucket': bucket(), 'Key': src}}
    if content_type:
        kw.update(MetadataDirective='REPLACE', ContentType=content_type,
                  Metadata={'filename': filename} if filename else {})
    s3().copy_object(**kw)


def delete(s3key):
    s3().delete_object(Bucket=bucket(), Key=s3key)


def presigned(s3key, filename, content_type, disposition='inline'):
    params = {'Bucket': bucket(), 'Key': s3key, 'ResponseContentType': content_type,
              'ResponseContentDisposition': '%s; filename="%s"' % (
                  disposition, (filename or 'audio').replace('"', ''))}
    return s3().generate_presigned_url('get_object', Params=params, ExpiresIn=PRESIGN_TTL)


def touch(ref, meta, modified):
    """Used again: restart the expiry clock by rewriting both objects in place.
    Best effort - a failure here must never fail the request."""
    if not modified or time.time() - modified < TOUCH_AFTER:
        return False
    try:
        copy(GENERATED_AUDIO + ref, GENERATED_AUDIO + ref,
             meta.get('contentType') or 'application/octet-stream', meta.get('filename'))
        put_json(GENERATED_META + ref + '.json', meta)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------- voices ----
def voices():
    """Every Polly voice in VOICE_REGION that one of our engines can use,
    sorted by name. Read from Polly, so new voices appear without a deploy."""
    if _catalogue['voices'] is not None and time.time() - _catalogue['at'] < VOICES_TTL:
        return _catalogue['voices']
    out, token = [], None
    try:
        while True:
            res = polly().describe_voices(**({'NextToken': token} if token else {}))
            for v in res.get('Voices', []):
                engines = sorted(e for e in (v.get('SupportedEngines') or ['standard'])
                                 if e in ENGINES)
                if not engines or not v.get('Id') or not v.get('LanguageCode'):
                    continue
                out.append({'voice': v['Id'], 'language': v['LanguageCode'],
                            'languageName': v.get('LanguageName') or v['LanguageCode'],
                            'gender': str(v.get('Gender') or '').lower(), 'engines': engines,
                            'languages': [v['LanguageCode']] +
                                         sorted(set(v.get('AdditionalLanguageCodes') or []) -
                                                {v['LanguageCode']})})
            token = res.get('NextToken')
            if not token:
                break
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ApiError(502, 'The voice list is not available from Amazon Polly.',
                       detail=str(e), region=region())
    out.sort(key=lambda v: v['voice'].lower())
    _catalogue.update(at=time.time(), voices=out)
    return out


def _speaks(v, lang):
    """lang is a full code ('en-au') or a language on its own ('en'), lower case."""
    return any(c.lower() == lang or c.lower().split('-')[0] == lang for c in v['languages'])


def _code(v, lang):
    """The LanguageCode Polly needs to make this voice speak lang: only when it
    is one of the voice's additional languages."""
    if not lang:
        return None
    for c in v['languages']:
        if c.lower() == lang or c.lower().split('-')[0] == lang:
            return None if c == v['language'] else c
    return None


def pick_voice(want, language, engine, seed=None):
    """-> (voice, LanguageCode or None, chosen at random?).

    want      a voice name, 'random', or nothing (the default voice, or failing
              that the first voice by name that fits)
    language  a code ('en-GB') or a language ('en'); narrows 'random'
    """
    cat = voices()
    by_name = {v['voice'].lower(): v for v in cat}
    lang = str(language).strip().lower() if language else None
    if lang and not any(_speaks(v, lang) for v in cat):
        raise ApiError(404, 'No voice speaks that language.', language=language,
                       hint='voice/voices lists the languages')
    want = str(want).strip() if want else ''
    if want and want.lower() != 'random':
        v = by_name.get(want.lower())
        if v is None:
            raise ApiError(404, 'Unknown voice.', voice=want,
                           hint='voice/voices lists the voices; voice=random picks one')
        if engine not in v['engines']:
            raise ApiError(400, "That voice is not available on the '%s' engine." % engine,
                           voice=v['voice'], allowed=v['engines'])
        if lang and not _speaks(v, lang):
            raise ApiError(400, 'That voice does not speak that language.', voice=v['voice'],
                           allowed=v['languages'])
        return v, _code(v, lang), False

    pool = [v for v in cat if engine in v['engines']]
    default = by_name.get(default_voice().lower())
    if want:                                    # random
        family = lang or (default['language'].split('-')[0].lower() if default else 'en')
        pool = [v for v in pool if _speaks(v, family)]
        if not pool:
            raise ApiError(404, "No '%s' voice speaks that language." % engine,
                           language=language or family)
        # Hashed, as names/get does: small integer seeds make poor Random() seeds.
        rnd = (random.Random(hashlib.sha256(str(seed).encode('utf-8')).hexdigest())
               if seed not in (None, '') else random.Random())
        v = rnd.choice(pool)
        return v, _code(v, lang), True

    if default and engine in default['engines'] and (not lang or _speaks(default, lang)):
        return default, _code(default, lang), False
    family = lang or (default['language'].lower() if default else 'en')
    pool = [v for v in pool if _speaks(v, family)]
    if not pool:
        raise ApiError(404, "No '%s' voice speaks that language." % engine,
                       language=language or family, hint='voice/voices lists what there is')
    return pool[0], _code(pool[0], lang), False


# -------------------------------------------------------- spend guards ----
def _day():
    return time.strftime('%Y-%m-%d', time.gmtime())


def _reserve(pk, attribute, amount, limit, refused):
    """Count `amount` against today's budget BEFORE spending it, or raise `refused`."""
    if amount > limit:
        raise refused
    try:
        table().update_item(
            Key={'PK': pk, 'SK': _day()},
            UpdateExpression='ADD #a :n SET #t = :ttl',
            ConditionExpression='attribute_not_exists(#a) OR #a <= :room',
            ExpressionAttributeNames={'#a': attribute, '#t': 'ttl'},
            ExpressionAttributeValues={':n': amount, ':room': limit - amount,
                                       ':ttl': now() + BUDGET_KEEP_DAYS * 86400})
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
            raise
        raise refused


def _refund(pk, attribute, amount):
    try:
        table().update_item(Key={'PK': pk, 'SK': _day()},
                            UpdateExpression='ADD #a :n',
                            ExpressionAttributeNames={'#a': attribute},
                            ExpressionAttributeValues={':n': -amount})
    except Exception:  # noqa: BLE001
        pass


def _used(pk, attribute):
    try:
        it = table().get_item(Key={'PK': pk, 'SK': _day()}).get('Item') or {}
        return int(it.get(attribute, 0))
    except Exception:  # noqa: BLE001
        return None


def reserve(characters):
    """Characters for Polly. Every cache miss costs money per character and the
    endpoint is open, so the day's synthesis is capped; cached and saved audio
    is never affected."""
    limit = daily_characters()
    if limit <= 0:
        raise ApiError(403, 'Speech synthesis is switched off on this deployment. Stored '
                            'audio (voice/get) still works.', synthesisDisabled=True)
    _reserve('budget', 'characters', characters, limit, ApiError(
        429, "Today's speech synthesis budget is used up. Utterances already generated (the "
             'same request again, or voice/get) still work.',
        budgetExhausted=True, dailyCharacters=limit, retryAfterSeconds=86400 - now() % 86400))


def refund(characters):
    _refund('budget', 'characters', characters)


def used_today():
    return _used('budget', 'characters')


def reserve_seconds(seconds):
    """Seconds of audio for Transcribe, which bills by the second. Audio already
    listened to is never affected."""
    limit = daily_listen_seconds()
    if limit <= 0:
        raise ApiError(403, 'Listening is switched off on this deployment. Transcripts '
                            'already made still work.', listeningDisabled=True)
    if seconds > limit:
        raise ApiError(400, "This audio is longer than a whole day's listening budget on this "
                            "deployment.", seconds=seconds, dailyListenSeconds=limit)
    _reserve('listen', 'seconds', seconds, limit, ApiError(
        429, "Today's listening budget is used up. Audio already listened to (the same audio "
             'again, or its ref) still works.',
        budgetExhausted=True, dailyListenSeconds=limit, retryAfterSeconds=86400 - now() % 86400))


def refund_seconds(seconds):
    _refund('listen', 'seconds', seconds)


def heard_today():
    return _used('listen', 'seconds')
