"""Tests for the voice service: the script (tags -> SSML -> report), the audio
helpers, and the API with S3/DynamoDB from moto and a scripted stand-in for
Amazon Polly (so each test can see exactly what Polly was asked to say)."""
import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

REGION = 'us-west-1'
BUCKET = 'voice-test'
TABLE = 'voice-test'
ENV = {'AWS_ACCESS_KEY_ID': 'testing', 'AWS_SECRET_ACCESS_KEY': 'testing',
       'AWS_SESSION_TOKEN': 'testing', 'AWS_DEFAULT_REGION': REGION, 'AWS_REGION': REGION,
       'BASE_URL': 'https://api.example.com/v1', 'VOICE_BUCKET': BUCKET, 'VOICE_TABLE': TABLE,
       'VOICE_REGION': 'ap-southeast-2', 'VOICE_NAME': 'Olivia', 'VOICE_ENGINE': 'neural',
       'VOICE_DAILY_CHARACTERS': '100000'}
os.environ.update(ENV)

from common import ApiError  # noqa: E402
import voice  # noqa: E402
import voice_audio as va  # noqa: E402
import voice_script as vs  # noqa: E402
import voice_store as store  # noqa: E402

VOICES = [
    ('Olivia', 'en-AU', 'Australian English', 'Female', ['neural', 'generative'], []),
    ('Nicole', 'en-AU', 'Australian English', 'Female', ['standard'], []),
    ('Russell', 'en-AU', 'Australian English', 'Male', ['standard', 'generative'], []),
    ('Amy', 'en-GB', 'British English', 'Female', ['standard', 'neural', 'generative'], []),
    ('Joanna', 'en-US', 'US English', 'Female', ['standard', 'neural', 'generative', 'long-form'], []),
    ('Matthew', 'en-US', 'US English', 'Male', ['standard', 'neural', 'generative'], []),
    ('Kajal', 'en-IN', 'Indian English', 'Female', ['neural'], ['hi-IN']),
    ('Vicki', 'de-DE', 'German', 'Female', ['standard', 'neural'], []),
    ('Gregory', 'en-US', 'US English', 'Male', ['long-form'], []),
]
MP3_FRAME = b'\xff\xf3\x64\xc4' + b'\x00' * 140          # MPEG-2 Layer III, 48 kbit/s, 24 kHz: 24 ms
OGG = (b'OggS' + bytes(24) + b'\x01vorbis' + struct.pack('<IBI', 0, 1, 24000) + bytes(20) +
       b'OggS\x00\x00' + struct.pack('<q', 36000) + bytes(20))   # 1.5 s


class FakePolly:
    """Returns half a second of audio in the format asked for, and remembers the request."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def describe_voices(self, **kw):
        return {'Voices': [{'Id': i, 'Name': i, 'LanguageCode': lc, 'LanguageName': ln,
                            'Gender': g, 'SupportedEngines': e, 'AdditionalLanguageCodes': a}
                           for i, lc, ln, g, e, a in VOICES]}

    def synthesize_speech(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise ClientError({'Error': {'Code': self.fail, 'Message': 'polly said no'}},
                              'SynthesizeSpeech')
        fmt, hz = kw['OutputFormat'], int(kw['SampleRate'])
        audio = {'pcm': b'\x01\x00' * (hz // 2), 'mp3': MP3_FRAME * 50, 'ogg_vorbis': OGG,
                 'mulaw': b'\x7f' * 4000, 'alaw': b'\x55' * 4000}[fmt]
        return {'AudioStream': io.BytesIO(audio), 'ContentType': 'audio/x',
                'RequestCharacters': str(len(ET.fromstring(_ns(kw['Text'])).itertext().__next__() or ''))}


def _ns(doc):
    return doc.replace('<speak', '<speak xmlns:amazon="urn:amazon"', 1)


def well_formed(doc):
    """SSML must parse, and must be one <speak> element."""
    root = ET.fromstring(_ns(doc))
    assert root.tag == 'speak'
    return root


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    for k, v in ENV.items():            # other test modules set these at import time
        monkeypatch.setenv(k, v)
    with mock_aws():
        store.reset_clients()
        boto3.client('s3', region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={'LocationConstraint': REGION})
        boto3.client('dynamodb', region_name=REGION).create_table(
            TableName=TABLE, BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': 'PK', 'AttributeType': 'S'},
                                  {'AttributeName': 'SK', 'AttributeType': 'S'}],
            KeySchema=[{'AttributeName': 'PK', 'KeyType': 'HASH'},
                       {'AttributeName': 'SK', 'KeyType': 'RANGE'}])
        polly = FakePolly()
        store._clients['polly'] = polly
        yield polly
        store.reset_clients()


def call(action, qs=None, method='GET', body=None, headers=None, b64=False):
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    ev = {'httpMethod': method, 'resource': '/voice/{proxy+}', 'path': '/v1/voice/' + action,
          'pathParameters': {'proxy': action} if action else None, 'queryStringParameters': qs,
          'headers': {'Host': 'api.example.com', **(headers or {})}, 'body': body,
          'isBase64Encoded': b64, 'requestContext': {'identity': {'sourceIp': '203.0.113.9'}}}
    r = voice.handler(ev, None)
    if r['headers'].get('Content-Type', '').startswith('application/json'):
        return r['statusCode'], json.loads(r['body'])
    return r['statusCode'], r


def say(**qs):
    return call('say', {k: str(v) for k, v in qs.items()})


LINE = "[anxious] I've been waiting forty minutes. [sighs] Is the doctor running late?"


# ============================================================ the script ====
def test_tags_are_parsed_out_of_the_words():
    s = vs.parse(LINE)
    assert s['text'] == "I've been waiting forty minutes. Is the doctor running late?"
    assert '[' not in s['text']
    assert s['items'] == [
        {'say': "I've been waiting forty minutes.", 'emotion': 'anxious'},
        {'cue': 'sighs'},
        {'say': 'Is the doctor running late?', 'emotion': 'anxious'}]
    assert [(t['tag'], t['kind'], t['at']) for t in s['tags']] == \
        [('anxious', 'emotion', 0), ('sighs', 'sound', 32)]


def test_emotion_parameter_is_a_leading_tag():
    a = vs.parse('[worried] When is my appointment?')
    b = vs.parse('When is my appointment?', emotion='anxious')
    assert a['items'] == b['items'] == [{'say': 'When is my appointment?', 'emotion': 'anxious'}]
    with pytest.raises(ApiError) as e:
        vs.parse('Hello', emotion='whispers')        # a delivery, not an emotion
    assert e.value.status == 400 and 'anxious' in e.value.extra['allowed']


def test_a_tag_holds_until_the_next_of_its_kind():
    s = vs.parse('[sad] One. [slowly] Two. [happy] Three. [neutral] Four.')
    assert s['items'] == [
        {'say': 'One.', 'emotion': 'sad'},
        {'say': 'Two.', 'emotion': 'sad', 'pace': 'slow'},
        {'say': 'Three.', 'emotion': 'happy', 'pace': 'slow'},
        {'say': 'Four.'}]


def test_synonyms_spacing_and_case():
    for word, kind, value in (('Worried', 'emotion', 'anxious'), ('CLEARS   throat', 'sound', 'clears throat'),
                              ('drawn out', 'pace', 'drawn'), ('whispering', 'volume', 'whisper'),
                              ('pause 1.5s', 'pause', 1500), ('pause 800ms', 'pause', 800),
                              ('pause 250', 'pause', 250), ('long pause', 'pause', 1200),
                              ('australian accent', 'accent', 'australian accent'),
                              ('wibble', 'unknown', 'wibble')):
        assert vs.lookup(word) == (kind, value), word


def test_brackets_that_are_not_tags_are_spoken():
    s = vs.parse(r'Press [1] then \[menu] and wait [...] ok')
    assert s['text'] == 'Press [1] then [menu] and wait [...] ok' and s['tags'] == []


def test_unknown_tags_are_kept_but_never_spoken():
    s = vs.parse('Hello [robot voice] there [wibble] friend [laughs]')
    assert s['text'] == 'Hello there friend'
    assert [i for i in s['items'] if 'cue' in i] == [{'cue': 'robot voice'}, {'cue': 'wibble'}, {'cue': 'laughs'}]
    rep = vs.report(s['tags'], 'neural')
    assert [(r['kind'], r['rendered']) for r in rep] == [('accent', 'none'), ('unknown', 'none'), ('sound', 'none')]


def test_segments_each_start_from_the_utterance_emotion():
    s = vs.parse(segments=[{'text': "Hi, it's Jo.", 'emotion': 'relaxed'}, {'pause': 600},
                           {'text': '[quickly] I am worried.', 'emotion': 'anxious'},
                           {'text': 'Thanks.'}], emotion='happy')
    assert s['items'] == [
        {'say': "Hi, it's Jo.", 'emotion': 'relaxed'}, {'pause': 600},
        {'say': 'I am worried.', 'emotion': 'anxious', 'pace': 'fast'},
        {'say': 'Thanks.', 'emotion': 'happy'}]
    assert s['text'] == "Hi, it's Jo. I am worried. Thanks."
    for bad in ([], 'x', [{'text': 'a', 'pause': 5}], [{'words': 'a'}], [{}], ['a'], [{'pause': 'soon'}]):
        with pytest.raises(ApiError):
            vs.parse(segments=bad)


def test_nothing_to_say_and_too_long():
    for text in ('', '   ', '[anxious] [pause]'):
        with pytest.raises(ApiError) as e:
            vs.parse(text)
        assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        vs.parse('word ' * 700)
    assert e.value.extra['maxCharacters'] == 3000


# ------------------------------------------------------------- rendering ----
def test_neural_gets_rate_and_volume_but_never_pitch():
    s = vs.parse(LINE + ' [pause 800ms] [angry] No!')
    doc, lead, tail = vs.ssml(s['items'], 'neural')
    assert doc == ('<speak><prosody rate="112%">I\'ve been waiting forty minutes. '
                   'Is the doctor running late?</prosody> <break time="800ms"/> '
                   '<prosody rate="106%" volume="x-loud">No!</prosody></speak>')
    assert 'pitch' not in doc and (lead, tail) == (0, 0)
    well_formed(doc)
    rep = vs.report(s['tags'], 'neural')
    assert [(r['tag'], r['rendered'], r['how']) for r in rep] == [
        ('anxious', 'approximated', 'rate 112%'),
        ('sighs', 'none', 'no AWS voice makes this sound yet; kept in the stored script'),
        ('pause 800ms', 'native', '800 ms of silence'),
        ('angry', 'approximated', 'rate 106%, volume x-loud')]


def test_standard_adds_pitch_and_a_real_whisper():
    s = vs.parse('[anxious] Hurry. [whispers] Quietly now. [loudly] Hello!')
    doc, _l, _t = vs.ssml(s['items'], 'standard')
    assert '<prosody pitch="+8%" rate="112%">Hurry.</prosody>' in doc
    assert '<amazon:effect name="whispered">Quietly now.</amazon:effect>' in doc
    assert 'volume="loud"' in doc
    well_formed(doc)
    rep = {r['tag']: r for r in vs.report(s['tags'], 'standard')}
    assert rep['whispers']['rendered'] == 'native' and rep['anxious']['how'] == 'rate 112%, pitch +8%'
    neural = {r['tag']: r for r in vs.report(s['tags'], 'neural')}
    assert neural['whispers']['rendered'] == 'approximated' and 'x-soft' in neural['whispers']['how']
    assert 'amazon:effect' not in vs.ssml(s['items'], 'neural')[0]


def test_generative_infers_emotion_and_takes_direction_by_sentence():
    s = vs.parse('[anxious] I am not sure. [slowly] Can you say that again? Thanks. [quickly] One more '
                 'thing [pause] is it open [slowly] on Sunday?')
    doc, _l, _t = vs.ssml(s['items'], 'generative')
    well_formed(doc)
    # no prosody from the emotion; pace wraps whole sentences only, and the sentence that
    # was already under way when [slowly] arrived keeps the pace it started with
    assert doc == ('<speak>I am not sure. <prosody rate="80%">Can you say that again? Thanks.</prosody> '
                   '<prosody rate="125%">One more thing <break time="500ms"/> is it open on Sunday?'
                   '</prosody></speak>')
    rep = {r['tag']: r for r in vs.report(s['tags'], 'generative')}
    assert rep['anxious']['rendered'] == 'inferred'
    assert rep['slowly']['rendered'] == 'approximated' and 'whole sentence' in rep['slowly']['how']


def test_text_is_escaped_and_ssml_is_always_well_formed():
    nasty = 'Tom & Jerry <b>said</b> "5 > 3" & it\'s \'fine\' ]]> &amp; <speak>'
    for engine in vs.ENGINES:
        for text in (nasty, '[angry] ' + nasty + ' [whispers] ' + nasty, '[slowly]' + nasty + '[pause]'):
            s = vs.parse(text)
            root = well_formed(vs.ssml(s['items'], engine, {'Jerry': 'Gerald "G" <x>'})[0])
            assert vs.clean(''.join(root.itertext())) == s['text']
    assert vs.parse('a\x00b\x07c ​ d\n\n\te')['text'] == 'abc ​ d e'


def test_pauses_clamp_and_edge_pauses_can_be_trimmed():
    s = vs.parse('[pause 300ms] Hello [pause 45s] there [long pause] [pause]')
    assert [i['pause'] for i in s['items'] if 'pause' in i] == [300, 10000, 1200, 500]
    assert 'at most 10000 ms' in vs.report(s['tags'], 'neural')[1]['how']
    doc, lead, tail = vs.ssml(s['items'], 'neural', trim=True)
    assert (lead, tail) == (300, 1700)
    assert doc == '<speak>Hello <break time="10000ms"/> there</speak>'
    doc, lead, tail = vs.ssml(s['items'], 'neural')
    assert doc.startswith('<speak><break time="300ms"/> Hello') and doc.endswith('<break time="500ms"/></speak>')
    # generative: a pause after the last words is at the edge even with no full stop
    assert vs.ssml(vs.parse('[slowly] Hello there [pause 1s]')['items'], 'generative', trim=True) == \
        ('<speak><prosody rate="80%">Hello there</prosody></speak>', 0, 1000)


def test_pronounce_wraps_whole_words_only():
    s = vs.parse('The ACME portal. Acme again, not acmes.')
    doc, _l, _t = vs.ssml(s['items'], 'neural', vs.check_pronounce({'ACME': 'ack me', 'portal': 'por-tal'}))
    assert doc == ('<speak>The <sub alias="ack me">ACME</sub> <sub alias="por-tal">portal</sub>. '
                   '<sub alias="ack me">Acme</sub> again, not acmes.</speak>')
    for bad in ('x', {'a': ''}, {'': 'b'}, {'a': 5}, {str(i): 'x' for i in range(51)}):
        with pytest.raises(ApiError):
            vs.check_pronounce(bad)


def test_ssml_passthrough_is_checked_but_not_changed():
    doc = '<speak>Code <say-as interpret-as="digits">4839</say-as> <amazon:effect name="whispered">ok</amazon:effect></speak>'
    s = vs.parse_ssml('  ' + doc + ' ')
    assert s == {'items': [{'ssml': doc}], 'tags': [], 'text': 'Code 4839 ok'}
    assert vs.ssml(s['items'], 'neural', None, trim=True) == (doc, 0, 0)
    for bad in ('<speak>unclosed', '<speak><!DOCTYPE x>hi</speak>', '<speak><?pi?>hi</speak>',
                '<speak></speak>', '<speaker>hi</speaker>', '<speak>' + 'x' * 6001 + '</speak>'):
        with pytest.raises(ApiError):
            vs.parse_ssml(bad)


def test_every_known_tag_renders_on_every_engine():
    for word, (kind, _v) in vs.TAGS.items():
        s = vs.parse('Before. [%s] After this tag. And more.' % word)
        for engine in vs.ENGINES:
            well_formed(vs.ssml(s['items'], engine)[0])
            r = vs.report(s['tags'], engine)[0]
            assert r['rendered'] in ('native', 'approximated', 'inferred', 'none') and r['how'], (word, engine)
    # 'curious' is pitch only: honest about being silent where pitch is not available
    assert vs.report(vs.parse('[curious] Oh?')['tags'], 'neural')[0]['rendered'] == 'none'
    assert vs.report(vs.parse('[curious] Oh?')['tags'], 'standard')[0]['rendered'] == 'approximated'
    assert {t['kind'] for t in vs.vocabulary()} == {'emotion', 'pace', 'volume', 'pause', 'reset', 'sound'}


# ================================================================= audio ====
def test_wav_header_and_durations():
    pcm = b'\x01\x00' * 16000
    w = va.wav(pcm, 16000)
    assert w[:4] == b'RIFF' and w[8:16] == b'WAVEfmt ' and len(w) == 44 + 32000
    assert struct.unpack_from('<I', w, 4)[0] == len(w) - 8 and struct.unpack_from('<I', w, 40)[0] == 32000
    assert va.wav_info(w) == (16000, 1, 16, 1000)
    assert va.duration_ms(w, 'wav') == 1000 and va.duration_ms(pcm, 'pcm', 16000) == 1000
    assert va.duration_ms(b'\x7f' * 4000, 'mulaw', 8000) == 500
    assert len(va.silence(250, 16000)) == 8000 and set(va.silence(10, 8000)) == {0}
    assert va.duration_ms(MP3_FRAME * 50, 'mp3') == 1200
    assert va.duration_ms(b'ID3\x04\x00\x00\x00\x00\x00\x0a' + bytes(10) + MP3_FRAME * 25 + b'TAG' + bytes(125), 'mp3') == 600
    assert va.duration_ms(OGG, 'ogg') == 1500
    assert va.duration_ms(b'not audio at all', 'mp3') is None and va.duration_ms(b'RIFFxxxx', 'wav') is None
    assert va.default_hz('mp3', 'neural') == 24000 and va.default_hz('mp3', 'standard') == 22050
    assert va.default_hz('wav', 'neural') == 16000 and va.default_hz('mulaw', 'neural') == 8000


def test_sniff():
    assert va.sniff(va.wav(b'', 8000)) == ('wav', 'audio/wav', 'wav')
    assert va.sniff(MP3_FRAME)[0] == 'mp3' and va.sniff(b'ID3\x03')[0] == 'mp3'
    assert va.sniff(OGG) == ('ogg', 'audio/ogg', 'ogg')
    assert va.sniff(b'\x00\x01\x02\x03', 'take1.pcm') == ('pcm', 'audio/pcm', 'pcm')
    assert va.sniff(b'\x1a\x45\xdf\xa3rest')[0] == 'webm'
    assert va.sniff(b'hello', 'notes.txt') == ('bin', 'application/octet-stream', 'txt')
    # raw PCM can open with 0xFFFF (a sample of -1): the name decides, not a weak MP3 guess
    assert va.sniff(b'\xff\xff\xfe\xff' * 10, 'take.raw')[0] == 'pcm'
    assert va.sniff(b'\xff\xff\xfe\xff' * 10)[0] == 'bin' and va.sniff(MP3_FRAME * 2, 'x.dat')[0] == 'mp3'


# =================================================================== say ====
def test_say_then_the_same_request_is_the_same_audio(aws):
    s, a = say(text='When is my next appointment?')
    assert s == 200
    assert a['cached'] is False and store.REF_RE.match(a['ref'])
    assert (a['voice'], a['language'], a['engine'], a['format'], a['hz']) == ('Olivia', 'en-AU', 'neural', 'mp3', 24000)
    assert a['text'] == 'When is my next appointment?' and a['tags'] == []
    assert a['ssml'] == '<speak>When is my next appointment?</speak>'
    assert a['durationMs'] == 1200 and a['bytes'] == 7200 and a['contentType'] == 'audio/mpeg'
    assert a['filename'] == a['ref'] + '.mp3' and a['getUrl'] == 'https://api.example.com/v1/voice/get?ref=' + a['ref']
    assert base64.b64decode(a['data']) == MP3_FRAME * 50
    assert aws.calls == [{'Engine': 'neural', 'VoiceId': 'Olivia', 'OutputFormat': 'mp3', 'SampleRate': '24000',
                          'Text': '<speak>When is my next appointment?</speak>', 'TextType': 'ssml'}]
    # again, and with the spelled-out defaults, and with untidy whitespace: no second synthesis
    for qs in ({'text': 'When is my next appointment?'},
               {'text': '  When is   my next appointment? ', 'voice': 'olivia', 'engine': 'NEURAL', 'format': 'MP3', 'hz': '24000'}):
        s, b = call('say', qs)
        assert s == 200 and b['cached'] is True and b['ref'] == a['ref'] and b['data'] == a['data']
    assert len(aws.calls) == 1


def test_anything_that_changes_the_audio_changes_the_ref(aws):
    base = say(text='Hello there.')[1]['ref']
    refs = {base}
    for extra in ({'voice': 'Amy'}, {'format': 'wav'}, {'hz': 16000}, {'engine': 'generative'},
                  {'emotion': 'anxious'}, {'text': 'Hello there!'}, {'text': 'hello there.'},
                  {'text': 'Hello there. [sighs]'}):
        s, b = say(**{'text': 'Hello there.', **extra})
        assert s == 200 and b['cached'] is False, extra
        refs.add(b['ref'])
    assert len(refs) == 9 and len(aws.calls) == 9
    # the emotion parameter and the leading tag are one and the same utterance
    assert say(text='[anxious] Hello there.')[1]['cached'] is True


def test_wav_is_pcm_with_a_header_and_exact_edge_silence(aws):
    s, b = say(text='[pause 250ms] Yes. [pause 1s]', format='wav')
    assert s == 200 and (b['format'], b['hz'], b['contentType']) == ('wav', 16000, 'audio/wav')
    data = base64.b64decode(b['data'])
    assert va.wav_info(data) == (16000, 1, 16, 1750) and b['durationMs'] == 1750 and b['bytes'] == len(data)
    body = data[44:]
    assert body[:8000] == bytes(8000) and body[8000:24000] == b'\x01\x00' * 8000 and body[24000:] == bytes(32000)
    assert aws.calls[0]['Text'] == '<speak>Yes.</speak>' and aws.calls[0]['OutputFormat'] == 'pcm'
    assert b['ssml'] == '<speak>Yes.</speak>'
    assert [t['how'] for t in b['tags']] == ['250 ms of silence', '1000 ms of silence']
    s, p = say(text='Yes.', format='pcm', hz=8000)
    assert base64.b64decode(p['data']) == b'\x01\x00' * 4000 and p['durationMs'] == 500
    s, m = say(text='Yes.', format='mulaw')
    assert (m['hz'], m['durationMs'], aws.calls[-1]['OutputFormat']) == (8000, 500, 'mulaw')
    s, o = say(text='Yes.', format='ogg')
    assert (o['durationMs'], aws.calls[-1]['OutputFormat'], o['contentType']) == (1500, 'ogg_vorbis', 'audio/ogg')


def test_tags_reach_polly_as_ssml_and_come_back_as_a_report(aws):
    s, b = say(text=LINE, format='wav')
    assert s == 200
    assert aws.calls[0]['Text'] == ("<speak><prosody rate=\"112%\">I've been waiting forty minutes. "
                                    "Is the doctor running late?</prosody></speak>")
    assert b['text'] == "I've been waiting forty minutes. Is the doctor running late?"
    assert b['tags'] == [
        {'tag': 'anxious', 'kind': 'emotion', 'at': 0, 'rendered': 'approximated', 'how': 'rate 112%'},
        {'tag': 'sighs', 'kind': 'sound', 'at': 32, 'rendered': 'none',
         'how': 'no AWS voice makes this sound yet; kept in the stored script'}]
    s, g = say(text=LINE, engine='generative')
    assert g['tags'][0]['rendered'] == 'inferred' and 'prosody' not in aws.calls[-1]['Text']


def test_strict_refuses_what_would_not_be_heard(aws):
    s, b = say(text=LINE, strict='true')
    assert s == 422 and b['engine'] == 'neural' and [t['tag'] for t in b['tags']] == ['sighs']
    assert aws.calls == []                                   # refused before any money was spent
    assert say(text='[anxious] [slowly] Fine. [pause]', strict='true')[0] == 200
    assert say(text='[whispers] Fine.', strict='1', engine='standard')[0] == 200


def test_text64_and_bad_parameters(aws):
    t64 = base64.urlsafe_b64encode('[anxious] Is 5 + 5 = 10 & more?'.encode()).decode().rstrip('=')
    s, b = say(text64=t64)
    assert s == 200 and b['text'] == 'Is 5 + 5 = 10 & more?' and '&amp;' in b['ssml']
    for qs, status, word in (({}, 400, 'text'), ({'text': 'x', 'engine': 'turbo'}, 400, 'engine'),
                             ({'text': 'x', 'format': 'flac'}, 400, 'format'),
                             ({'text': 'x', 'hz': '44100'}, 400, 'hz'), ({'text': 'x', 'hz': 'fast'}, 400, 'hz'),
                             ({'text': 'x', 'format': 'wav', 'hz': '24000'}, 400, 'hz'),
                             ({'text': 'x', 'emotion': 'purple'}, 400, 'emotion'),
                             ({'text': 'x', 'strict': 'maybe'}, 400, 'strict'),
                             ({'text': 'x', 'view': 'true', 'download': 'true'}, 400, 'view'),
                             ({'text': 'x', 'segments': '[]'}, 400, 'segments'),
                             ({'text': 'x', 'voice': 'Nobody'}, 404, 'voice'),
                             ({'text': 'x', 'language': 'xx-XX'}, 404, 'language'),
                             ({'text64': '////'}, 400, 'text64')):
        s, b = call('say', qs)
        assert s == status and word in b['error'].lower() and b['status'] == status, (qs, b)
    assert say(text='x', hz=44100)[1]['allowed'] == [8000, 16000, 22050, 24000]
    assert aws.calls and len(aws.calls) == 1


# ---------------------------------------------------------------- voices ----
def test_voice_selection(aws):
    def who(**qs):
        s, b = say(**{'text': 'Hi.', **qs})
        assert s == 200, b
        return b['voice'], b['language'], b['engine']
    assert who() == ('Olivia', 'en-AU', 'neural')
    assert who(engine='generative') == ('Olivia', 'en-AU', 'generative')
    assert who(engine='standard') == ('Nicole', 'en-AU', 'standard')      # Olivia has no standard voice
    assert who(language='en-GB') == ('Amy', 'en-GB', 'neural')
    assert who(language='de') == ('Vicki', 'de-DE', 'neural')
    assert who(voice='joanna') == ('Joanna', 'en-US', 'neural')
    assert who(voice='Kajal', language='hi-IN') == ('Kajal', 'hi-IN', 'neural')
    assert aws.calls[-1]['LanguageCode'] == 'hi-IN' and 'LanguageCode' not in aws.calls[-2]
    s, b = say(text='Hi.', voice='Nicole')
    assert s == 400 and b['allowed'] == ['standard'] and b['voice'] == 'Nicole'
    s, b = say(text='Hi.', voice='Vicki', language='en')
    assert s == 400 and b['allowed'] == ['de-DE']
    assert say(text='Hi.', voice='Gregory')[0] == 404          # long-form only: not offered
    assert say(text='Hi.', engine='generative', language='de')[0] == 404


def test_random_voice_is_repeatable_with_a_seed(aws):
    picks = {}
    for seed in ('run-1', 'run-2', 'run-3', '1', '2', '3', '4', '5'):
        s, b = say(text='Hi.', voice='random', seed=seed)
        assert s == 200 and b['seed'] == seed and b['language'].startswith('en-')
        picks[seed] = b['voice']
        assert say(text='Hi.', voice='random', seed=seed)[1]['voice'] == b['voice']
    assert len(set(picks.values())) > 1                       # variety across seeds
    assert set(picks.values()) <= {'Olivia', 'Amy', 'Joanna', 'Matthew', 'Kajal'}   # English, neural
    assert say(text='Hi.', voice='random', language='de', seed='x')[1]['voice'] == 'Vicki'
    s, b = say(text='Hi.', voice='random')
    assert s == 200 and b['seed'] is None
    assert 'seed' not in say(text='Hi.')[1]


def test_voices_and_tags_endpoints(aws):
    s, b = call('voices')
    assert s == 200 and b['region'] == 'ap-southeast-2'
    assert b['default'] == {'voice': 'Olivia', 'language': 'en-AU', 'engine': 'neural'}
    assert b['count'] == 8 and [v['voice'] for v in b['voices']][:2] == ['Amy', 'Joanna']
    assert 'Gregory' not in [v['voice'] for v in b['voices']]
    olivia = next(v for v in b['voices'] if v['voice'] == 'Olivia')
    assert olivia == {'voice': 'Olivia', 'language': 'en-AU', 'languageName': 'Australian English',
                      'gender': 'female', 'engines': ['generative', 'neural'], 'languages': ['en-AU']}
    assert next(v for v in b['voices'] if v['voice'] == 'Joanna')['engines'] == ['generative', 'neural', 'standard']
    assert b['engines']['neural']['emotion'] == 'approximated' and b['engines']['generative']['emotion'] == 'inferred'
    assert [v['voice'] for v in call('voices', {'language': 'en-AU', 'engine': 'standard'})[1]['voices']] == ['Nicole', 'Russell']
    assert call('voices', {'language': 'hi'})[1]['count'] == 1
    assert call('voices', {'engine': 'warp'})[0] == 400
    s, t = call('tags')
    assert s == 200 and t['engines'] == b['engines'] and len(t['syntax']) >= 4
    anxious = next(x for x in t['tags'] if x['tag'] == 'anxious')
    assert 'worried' in anxious['also'] and anxious['rendered'] == \
        {'standard': 'approximated', 'neural': 'approximated', 'generative': 'inferred'}
    s, i = call('')
    assert s == 200 and i['actions'] == ['delete', 'get', 'save', 'say', 'tags', 'voices']
    s, e = call('sing')
    assert s == 404 and 'say' in e['actions']


def test_voice_list_failure_is_a_502(aws):
    def boom(**kw):
        raise RuntimeError('no route to polly')
    aws.describe_voices = boom
    s, b = say(text='Hi.')
    assert s == 502 and b['region'] == 'ap-southeast-2' and 'no route' in b['detail']


# ------------------------------------------------------- delivery and get ----
def test_url_view_and_download(aws):
    s, b = say(text='Hello.', url='true')
    assert s == 200 and 'data' not in b and b['expiresInSeconds'] == 900
    assert BUCKET in b['url'] and 'generated/audio/' + b['ref'] in b['url'] and 'audio%2Fmpeg' in b['url']
    s, r = say(text='Hello.', view='true')
    assert s == 302 and r['body'] == '' and 'inline' in r['headers']['Location']
    assert r['headers']['Access-Control-Allow-Origin'] == '*'
    s, r = call('get', {'ref': b['ref'], 'download': 'true'})
    assert s == 302 and 'attachment' in r['headers']['Location'] and b['ref'] + '.mp3' in r['headers']['Location']
    assert len(aws.calls) == 1


def test_play_is_a_page_that_plays_the_audio_with_no_player(aws):
    plain = say(text='Hello Paul')[1]
    s, r = say(text='Hello Paul', play='true')
    assert s == 200 and r['headers']['Content-Type'] == 'text/html; charset=utf-8'
    assert r['headers']['Cache-Control'] == 'no-store' and len(aws.calls) == 1   # the same stored utterance
    page, csp = r['body'], r['headers']['Content-Security-Policy']
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", csp).group(1)
    assert csp.startswith("default-src 'none'") and page.count('nonce="%s"' % nonce) == 2
    assert '<audio' not in page and ' controls' not in page          # no player element at all
    assert 'new Audio()' in page and 'NotAllowedError' in page and '<button id="play" type="button" hidden>' in page
    assert '<p class="said">Hello Paul</p>' in page and '<dd>Olivia</dd>' in page and '<dd>1.2 s</dd>' in page
    assert '<dd>%s</dd>' % plain['ref'] in page
    src = json.loads(re.search(r'var src = (".*?");', page).group(1))
    assert src.startswith('https://') and 'generated/audio/' + plain['ref'] in src and 'inline' in src
    assert not re.search(r'__[A-Z]+__', page)                        # every token filled
    assert say(text='Hello Paul', play='true')[1]['headers']['Content-Security-Policy'] != csp   # a fresh nonce
    # by ref, by key, and a recording that has no words
    s, r = call('get', {'ref': plain['ref'], 'play': '1'})
    assert s == 200 and '<p class="said">Hello Paul</p>' in r['body']
    call('save', {'key': 'hello.paul', 'ref': plain['ref']})
    s, r = call('get', {'key': 'hello.paul', 'play': 'true'})
    assert s == 200 and 'saved/audio/hello.paul' in r['body'] and '<dd>hello.paul</dd>' in r['body']
    call('save', {'key': 'take1'}, 'POST', base64.b64encode(va.wav(b'\x02\x00' * 8000, 16000)).decode())
    s, r = call('get', {'key': 'take1', 'play': 'true'})
    assert s == 200 and '<p class="said">take1.wav</p>' in r['body'] and '<dd>0.5 s</dd>' in r['body']
    s, r = call('say', None, 'POST', {'text': 'Hello Paul', 'play': True})
    assert s == 200 and r['headers']['Content-Type'].startswith('text/html')
    # one way of being served at a time, decided before anything is synthesised
    for combo in ({'play': 'true', 'view': 'true'}, {'play': 'true', 'download': 'true'}):
        s, b = call('say', {'text': 'Not said', **combo})
        assert s == 400 and b['received'] == sorted(combo, key=['play', 'view', 'download'].index)
    assert call('get', {'ref': plain['ref'], 'play': 'true', 'view': 'true'})[0] == 400
    assert len(aws.calls) == 1


def test_play_page_cannot_be_made_to_run_a_callers_markup(aws):
    evil = ('</script><script>alert(1)</script> <img src=x onerror=alert(2)> "quoted" \'single\' '
            '__SRC__ __NONCE__ </p></div> & more')
    s, r = say(text=evil, play='true')
    assert s == 200
    page = r['body']
    assert page.count('<script') == 1 and page.count('</script>') == 1
    assert '<img' not in page and 'onerror=alert(2)&gt;' in page
    assert '&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert '__SRC__ __NONCE__' in page                               # their text, left alone
    assert page.count('<p') == 3 and page.count('</div>') == 2 + page.count('<div><dt>')
    # a key is the only other caller text on the page, and its alphabet has no markup in it
    assert call('save', {'key': '<b>'}, 'POST', 'AAAA')[0] == 400
    # the audio URL is a script string: nothing in it can close the element
    import voice_page
    lit = voice_page._js_string('https://x/?a=1&b=</script><!--')
    assert '<' not in lit and '>' not in lit and '&' not in lit
    assert json.loads(lit) == 'https://x/?a=1&b=</script><!--'


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_play_page_script_is_valid_javascript(aws, tmp_path):
    page = say(text="It's \"quoted\" \\ and `ticked` ${x}", play='true')[1]['body']
    js = tmp_path / 'play.js'
    js.write_text(re.search(r'<script[^>]*>(.*?)</script>', page, re.S).group(1), encoding='utf-8')
    r = subprocess.run(['node', '--check', str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_get_replays_the_stored_bytes(aws):
    s, a = say(text=LINE, format='wav')
    s, g = call('get', {'ref': a['ref']})
    assert s == 200 and g['source'] == 'generated' and g['data'] == a['data']
    for k in ('ref', 'text', 'voice', 'language', 'engine', 'format', 'hz', 'bytes', 'durationMs', 'tags', 'ssml', 'createdTime'):
        assert g[k] == a[k], k
    assert 'cached' not in g and 'script' not in g and len(aws.calls) == 1
    aws.fail = 'ServiceFailureException'                     # Polly down: replay does not care
    assert call('get', {'ref': a['ref']})[0] == 200 and say(text=LINE, format='wav')[1]['cached'] is True
    assert call('get', {'ref': 'vzzzzzzzzzzz'})[0] == 404
    for qs in ({}, {'ref': 'nope'}, {'ref': a['ref'], 'key': 'k'}, {'key': 'bad key!'}):
        assert call('get', qs)[0] == 400, qs


def test_use_restarts_the_expiry_clock(aws):
    ref = say(text='Keep me.')[1]['ref']
    meta, modified = store.load(store.GENERATED_META + ref + '.json')
    assert store.touch(ref, meta, modified) is False              # fresh: nothing to do
    assert store.touch(ref, meta, modified - 2 * 86400) is True   # older than a day: rewritten in place
    s, b = say(text='Keep me.')
    assert s == 200 and b['cached'] is True and base64.b64decode(b['data']) == MP3_FRAME * 50
    head = boto3.client('s3', region_name=REGION).head_object(Bucket=BUCKET, Key=store.GENERATED_AUDIO + ref)
    assert head['ContentType'] == 'audio/mpeg' and head['Metadata'] == {'filename': ref + '.mp3'}


# ------------------------------------------------------------ save/delete ----
def test_save_pins_an_utterance_under_a_key(aws):
    a = say(text='[relaxed] Good morning.', format='wav')[1]
    s, v = call('save', {'key': 'greeting.relaxed', 'ref': a['ref']})
    assert s == 200 and v['saved'] is True and v['source'] == 'generated' and v['ref'] == a['ref']
    assert v['filename'] == 'greeting.relaxed.wav' and v['getUrl'].endswith('/voice/get?key=greeting.relaxed')
    assert v['savedTime'] >= a['createdTime'] and v['text'] == 'Good morning.'
    s, g = call('get', {'key': 'greeting.relaxed'})
    assert s == 200 and g['source'] == 'saved' and g['savedFrom'] == 'generated' and g['ref'] == a['ref']
    assert g['data'] == a['data'] and g['tags'] == a['tags'] and g['key'] == 'greeting.relaxed'
    # the saved copy outlives the generated one
    boto3.resource('s3', region_name=REGION).Bucket(BUCKET).objects.filter(Prefix='generated/').delete()
    assert call('get', {'ref': a['ref']})[0] == 404 and call('get', {'key': 'greeting.relaxed'})[1]['data'] == a['data']
    assert call('save', {'key': 'k2', 'ref': a['ref']})[0] == 404
    assert call('save', {'key': 'k2'})[0] == 400 and call('save', {'ref': a['ref']})[0] == 400
    assert call('save', {'key': 'k2', 'ref': 'short'})[0] == 400
    s, d = call('delete', {'key': 'greeting.relaxed'})
    assert s == 200 and d == {'key': 'greeting.relaxed', 'deleted': True}
    assert call('delete', {'key': 'greeting.relaxed'})[1]['deleted'] is False
    assert call('get', {'key': 'greeting.relaxed'})[0] == 404 and call('delete')[0] == 400


def test_save_a_recording(aws):
    rec = va.wav(b'\x02\x00' * 12000, 16000)
    s, v = call('save', {'key': 'human+anxious'}, 'POST', base64.b64encode(rec).decode(),
                {'Content-Type': 'application/octet-stream'}, b64=True)
    assert s == 200 and v['source'] == 'upload' and v['bodyFormat'] == 'binary'
    assert (v['format'], v['hz'], v['channels'], v['durationMs'], v['bytes']) == ('wav', 16000, 1, 750, len(rec))
    assert v['filename'] == 'human_anxious.wav' and v['contentType'] == 'audio/wav'
    assert v['getUrl'].endswith('/voice/get?key=human%2Banxious')
    s, g = call('get', {'key': 'human+anxious'})
    assert s == 200 and base64.b64decode(g['data']) == rec and g['savedFrom'] == 'upload' and 'text' not in g
    s, r = call('get', {'key': 'human+anxious', 'view': '1'})
    assert s == 302 and 'saved/audio/human%2Banxious' in r['headers']['Location']
    # base64 text body, with a filename; an MP3
    s, v = call('save', {'key': 'clip', 'filename': 'My Clip (1).mp3'}, 'POST',
                base64.b64encode(MP3_FRAME * 25).decode(), {'Content-Type': 'text/plain'})
    assert s == 200 and (v['format'], v['durationMs'], v['filename'], v['bodyFormat']) == ('mp3', 600, 'My_Clip__1_.mp3', 'base64')
    s, v = call('save', {'key': 'mystery'}, 'POST', base64.b64encode(b'hello world').decode())
    assert s == 200 and v['format'] == 'bin' and 'hint' in v and 'durationMs' not in v
    assert call('save', {'key': 'x'}, 'POST', 'this is not base64!')[0] == 400
    assert call('save', {'key': 'x'}, 'POST', '')[0] == 400
    big = base64.b64encode(bytes(4096 * 1024 + 1)).decode()
    s, b = call('save', {'key': 'x'}, 'POST', big, {'Content-Type': 'application/octet-stream'}, b64=True)
    assert s == 400 and b['maxKb'] == 4096
    # a recording has no words to say again
    s, b = call('say', {'key': 'human+anxious', 'engine': 'standard'})
    assert s == 400 and 'recording' in b['error']
    assert aws.calls == []


# ------------------------------------------------------------------- POST ----
def test_post_json_segments_and_pronounce(aws):
    body = {'voice': 'Olivia', 'format': 'wav', 'hz': 16000, 'strict': True,
            'pronounce': {'ACME': 'ack me'},
            'segments': [{'text': "Hi, it's Jo from ACME.", 'emotion': 'relaxed'}, {'pause': 600},
                         {'text': "Actually, I'm quite worried about my results.", 'emotion': 'anxious'}]}
    s, b = call('say', None, 'POST', body, {'Content-Type': 'application/json'})
    assert s == 200 and b['cached'] is False
    assert b['text'] == "Hi, it's Jo from ACME. Actually, I'm quite worried about my results."
    assert aws.calls[0]['Text'] == (
        '<speak><prosody rate="92%">Hi, it\'s Jo from <sub alias="ack me">ACME</sub>.</prosody> '
        '<break time="600ms"/> <prosody rate="112%">Actually, I\'m quite worried about my results.'
        '</prosody></speak>')
    assert [(t['tag'], t['at']) for t in b['tags']] == [('relaxed', 0), ('pause', 22), ('anxious', 22)]
    # the same thing as text with inline tags is the same utterance (and Postman's text/plain is fine)
    s, c = call('say', {'format': 'wav'}, 'POST',
                json.dumps({'text': "[relaxed] Hi, it's Jo from ACME. [pause 600ms] [anxious] Actually, "
                                    "I'm quite worried about my results.", 'pronounce': {'ACME': 'ack me'}}),
                {'Content-Type': 'text/plain'})
    assert s == 200 and c['ref'] == b['ref'] and c['cached'] is True
    # a different pronunciation is different audio
    s, d = call('say', None, 'POST', dict(body, pronounce={'ACME': 'ay see em ee'}))
    assert d['ref'] != b['ref']
    # a body that is not JSON is the text itself; query parameters still apply
    s, e = call('say', {'voice': 'Amy', 'emotion': 'happy'}, 'POST', 'Good morning [pause] everyone')
    assert s == 200 and e['voice'] == 'Amy' and e['text'] == 'Good morning everyone'
    assert e['tags'][0]['tag'] == 'happy'
    # curl -d sends a form: known fields are fields, anything else is the text
    form = {'Content-Type': 'application/x-www-form-urlencoded'}
    s, f = call('say', None, 'POST', 'text=Good+morning+%5Bpause%5D+everyone&voice=Amy&emotion=happy', form)
    assert s == 200 and f['ref'] == e['ref'] and f['cached'] is True
    s, f = call('say', None, 'POST', 'Is 2+2=4?', form)
    assert s == 200 and f['text'] == 'Is 2+2=4?'
    for bad, word in (({'text': 'a', 'segments': [{'text': 'b'}]}, 'not both'), ({'txt': 'a'}, 'unknown fields'),
                      ({'segments': [{'text': 'a', 'speed': 2}]}, 'unknown fields'), ({'text': 5}, 'string'),
                      ({'text': 'a', 'pronounce': 'x'}, 'pronounce'), ('{"text": ', 'not valid json'),
                      ({'segments': []}, 'segments')):
        s, b2 = call('say', None, 'POST', bad)
        assert s == 400 and word in b2['error'].lower(), (bad, b2)
    assert call('say', None, 'POST', {'txt': 'a'})[1]['allowed'][0] == 'text'


def test_post_ssml_is_passed_through(aws):
    doc = '<speak>Your code is <say-as interpret-as="digits">483921</say-as>.</speak>'
    s, b = call('say', {'format': 'wav'}, 'POST', {'text': doc})
    assert s == 200 and b['ssml'] == doc and b['text'] == 'Your code is 483921.' and b['tags'] == []
    assert aws.calls[0]['Text'] == doc
    assert call('say', {'text': doc, 'emotion': 'happy'})[0] == 400
    assert call('say', {'text': '<speak>oops'})[0] == 400


# ----------------------------------------------------------- say it again ----
def test_say_again_on_another_engine_or_format(aws):
    a = call('say', None, 'POST', {'text': '[anxious] [whispers] Is anyone there? [sighs]',
                                   'pronounce': {'anyone': 'any one'}})[1]
    s, same = call('say', {'ref': a['ref']})
    assert s == 200 and same['ref'] == a['ref'] and same['cached'] is True
    s, b = call('say', {'ref': a['ref'], 'engine': 'standard', 'voice': 'Nicole', 'format': 'wav'})
    assert s == 200 and b['ref'] != a['ref'] and b['cached'] is False and b['text'] == a['text']
    assert (b['engine'], b['voice'], b['format'], b['hz']) == ('standard', 'Nicole', 'wav', 16000)
    assert '<amazon:effect name="whispered">' in b['ssml'] and '<sub alias="any one">' in b['ssml']
    assert [t['rendered'] for t in a['tags']] == ['approximated', 'approximated', 'none']
    assert [t['rendered'] for t in b['tags']] == ['approximated', 'native', 'none']
    # Olivia has no standard voice: say so, do not quietly change the speaker
    s, e = call('say', {'ref': a['ref'], 'engine': 'standard'})
    assert s == 400 and e['allowed'] == ['generative', 'neural']
    call('save', {'key': 'whisper', 'ref': a['ref']})
    s, k = call('say', {'key': 'whisper', 'engine': 'generative'})
    assert s == 200 and k['engine'] == 'generative' and k['tags'][0]['rendered'] == 'inferred'
    assert call('say', {'ref': a['ref'], 'text': 'something else'})[0] == 400
    assert call('say', {'ref': 'vzzzzzzzzzzz'})[0] == 404 and call('say', {'key': 'nope'})[0] == 404


# ----------------------------------------------------------- spend guard ----
def test_daily_budget_stops_new_synthesis_but_not_replay(aws, monkeypatch):
    monkeypatch.setenv('VOICE_DAILY_CHARACTERS', '30')
    a = say(text='Twenty characters ok')[1]                  # 20
    assert a['cached'] is False and store.used_today() == 20
    s, b = say(text='Eleven more')                           # 11: would make 31
    assert s == 429 and b['budgetExhausted'] is True and b['dailyCharacters'] == 30
    assert 0 < b['retryAfterSeconds'] <= 86400 and store.used_today() == 20
    assert say(text='Ten chars.')[0] == 200 and store.used_today() == 30
    assert say(text='x')[0] == 429
    assert say(text='Twenty characters ok')[1]['cached'] is True      # replay is free
    monkeypatch.setenv('VOICE_DAILY_CHARACTERS', '5')
    store.table().delete_item(Key={'PK': 'budget', 'SK': store._day()})
    assert say(text='Longer than a whole day allows')[0] == 429        # even as the day's first request
    assert store.used_today() == 0
    monkeypatch.setenv('VOICE_DAILY_CHARACTERS', '30')
    store.table().put_item(Item={'PK': 'budget', 'SK': store._day(), 'characters': 30})
    assert call('get', {'ref': a['ref']})[0] == 200
    assert len(aws.calls) == 2
    monkeypatch.setenv('VOICE_DAILY_CHARACTERS', '0')
    s, b = say(text='Anything new')
    assert s == 403 and b['synthesisDisabled'] is True
    assert say(text='Twenty characters ok')[0] == 200


def test_polly_failures(aws):
    aws.fail = 'ServiceFailureException'
    s, b = say(text='Hello there')
    assert s == 502 and b['detail'] == 'polly said no' and b['region'] == 'ap-southeast-2'
    assert store.used_today() == 0                           # the reservation was handed back
    aws.fail = 'InvalidSsmlException'
    s, b = say(text='[slowly] Hello there')
    assert s == 400 and b['code'] == 'InvalidSsmlException' and 'tags' in b['hint']
    aws.fail = 'ThrottlingException'
    assert say(text='Hello there')[0] == 429
    aws.fail = None
    s, b = say(text='Hello there')
    assert s == 200 and b['cached'] is False                 # nothing half-stored by the failures
    assert store.used_today() == 11
