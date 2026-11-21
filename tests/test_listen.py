"""Tests for the listening half of the voice service: the words (canonical
tokens, finding, comparing), the audio that may be listened to, and the API
with S3/DynamoDB from moto and a scripted stand-in for Amazon Transcribe - one
that takes as many status checks as a test tells it to, and writes its
transcript into the bucket the way the service does."""
import base64
import json
import struct

import boto3
import pytest
from botocore.exceptions import ClientError

import test_voice
from test_voice import BUCKET, MP3_FRAME, OGG, REGION, call, say
import voice_audio as va
import voice_listen as vl
import voice_store as store
import voice_words as vw

aws = test_voice.aws        # its autouse fixture: S3, DynamoDB and the stand-in for Polly

REPLY = 'Your next appointment is on Tuesday the 24th at 3 p.m. with Dr. Nguyen.'
WHOLE = ('p.m.', 'a.m.', 'Dr.', 'Mr.', 'Mrs.')
OCTET = {'Content-Type': 'application/octet-stream'}


def transcript(text, unsure=(), language=None):
    """What Amazon Transcribe writes for `text`: one item per word, punctuation
    as items of its own, times as strings of seconds."""
    items, t = [], 0.12
    for token in text.split():
        word = token if token in WHOLE else token.rstrip('.,?!')
        items.append({'type': 'pronunciation', 'start_time': '%.2f' % t, 'end_time': '%.2f' % (t + 0.3),
                      'alternatives': [{'confidence': '0.41' if word in unsure else '0.99',
                                        'content': word}]})
        t += 0.35
        if token != word:
            items.append({'type': 'punctuation',
                          'alternatives': [{'confidence': '0.0', 'content': token[len(word):]}]})
    results = {'transcripts': [{'transcript': text}], 'items': items}
    if language:
        results['language_code'] = language
    return {'jobName': 'job', 'accountId': '123456789012', 'status': 'COMPLETED', 'results': results}


def words_of(text, **kw):
    return vw.digest(transcript(text, **kw))['words']


def _error(code, message, op):
    return ClientError({'Error': {'Code': code, 'Message': message}}, op)


class FakeTranscribe:
    """Batch jobs that finish after `takes` status checks and say `says`."""

    def __init__(self):
        self.jobs, self.started, self.deleted = {}, [], []
        self.says, self.unsure, self.takes = REPLY, (), 1
        self.fail = None            # an error code for StartTranscriptionJob
        self.failure = None         # a FailureReason: the job itself fails
        self.spoken = None          # the language an IdentifyLanguage job finds

    def start_transcription_job(self, **kw):
        if self.fail:
            raise _error(self.fail, 'transcribe said no', 'StartTranscriptionJob')
        name = kw['TranscriptionJobName']
        if name in self.jobs:
            raise _error('ConflictException', 'The requested job name already exists.',
                         'StartTranscriptionJob')
        self.started.append(kw)
        self.jobs[name] = {'kw': kw, 'checks': 0, 'status': 'QUEUED'}
        return {'TranscriptionJob': {'TranscriptionJobName': name, 'TranscriptionJobStatus': 'QUEUED'}}

    def get_transcription_job(self, TranscriptionJobName):
        job = self.jobs.get(TranscriptionJobName)
        if job is None:
            raise _error('BadRequestException', "The requested job couldn't be found.",
                         'GetTranscriptionJob')
        job['checks'] += 1
        if job['status'] in ('QUEUED', 'IN_PROGRESS'):
            job['status'] = 'IN_PROGRESS'
            if job['checks'] >= self.takes:
                if self.failure:
                    job['status'] = 'FAILED'
                else:
                    kw = job['kw']
                    doc = transcript(self.says, self.unsure,
                                     self.spoken if kw.get('IdentifyLanguage') else None)
                    boto3.client('s3', region_name=REGION).put_object(
                        Bucket=kw['OutputBucketName'], Key=kw['OutputKey'],
                        Body=json.dumps(doc).encode())
                    job['status'] = 'COMPLETED'
        out = {'TranscriptionJobName': TranscriptionJobName, 'TranscriptionJobStatus': job['status']}
        if job['status'] == 'FAILED':
            out['FailureReason'] = self.failure
        if job['kw'].get('LanguageCode'):
            out['LanguageCode'] = job['kw']['LanguageCode']
        return {'TranscriptionJob': out}

    def delete_transcription_job(self, TranscriptionJobName):
        self.deleted.append(TranscriptionJobName)
        self.jobs.pop(TranscriptionJobName, None)


@pytest.fixture(autouse=True)
def transcribe(aws, monkeypatch):
    monkeypatch.setenv('VOICE_LANGUAGE', 'en-AU')
    monkeypatch.setenv('VOICE_DAILY_LISTEN_SECONDS', '1800')
    monkeypatch.setattr(vl, 'POLL_S', 0)
    fake = FakeTranscribe()
    store._clients['transcribe'] = fake
    return fake


PCM = b'\x10\x00\xf0\xff' * 12000                 # 1 s of 16-bit mono at 24 kHz
WAV = va.wav(PCM, 24000)


def post(action, data, qs=None):
    """POST raw bytes, as API Gateway hands a binary body to the function."""
    return call(action, qs, 'POST', base64.b64encode(data).decode(), OCTET, b64=True)


def s3_get(key):
    return boto3.client('s3', region_name=REGION).get_object(Bucket=BUCKET, Key=key)['Body'].read()


# ============================================================= the words ====
def test_canonical_tokens_meet_from_both_sides():
    said = vw.canonical_text('Tuesday the twenty-fourth at 3 p.m. with Doctor Nguyen.')
    assert said == vw.canonical_text('tuesday the 24th at three PM with Dr. Nguyen')
    assert said == ['tuesday', 'the', '24', 'at', '3', 'pm', 'with', 'dr', 'nguyen']
    for text, tokens in (
            ('four eight three nine two one', ['483921']),
            ('Your code is 4 8 3 9.', ['your', 'code', 'is', '4839']),
            ('a hundred and five dollars', ['105', 'dollars']), ('$50', ['50', 'dollars']),
            ('$3.50', ['3', 'dollars', '50', 'cents']), ('one dollar', ['1', 'dollars']),
            ('three dollars and fifty cents', ['3', 'dollars', '50', 'cents']),
            ('$1,250.00 and \u00a340.05', ['1250', 'dollars', 'and', '40', 'pounds', '5', 'pence']),
            ('1.5 chocolate bars', ['1', 'point', '5', 'chocolate', 'bars']),
            ('one point two five', ['1', 'point', '25']), ('1.25', ['1', 'point', '25']),
            ('2.5%', ['2', 'point', '5', 'percent']), ('It costs 5.', ['it', 'costs', '5']),
            ('8:30 a.m. on June 1st', ['8', '30', 'am', 'on', 'june', '1']),
            ('eight thirty AM on June first', ['8', '30', 'am', 'on', 'june', '1']),
            ('two thousand and twenty six', ['2026']), ('1,200 people', ['1200', 'people']),
            ('half past 3:30', ['half', 'past', '3', '30']), ('50% off', ['50', 'percent', 'off']),
            ("I've got it, okay?", ['ive', 'got', 'it', 'ok']), ('one second', ['1', 'second']),
            ('the twenty first', ['the', '21']), ('ninety-nine', ['99']), ('?!', [])):
        assert vw.canonical_text(text) == tokens, text
    # the example in Amazon Transcribe's own guide: what was said, and how it writes it
    assert vw.canonical_text('Meet me at eight-thirty AM on June first at one-hundred Main Street '
                             'with three-dollars-and-fifty-cents and one-point-five chocolate bars') == \
        vw.canonical_text('Meet me at 8:30 a.m. on June 1st at 100 Main Street with $3.50 and 1.5 '
                          'chocolate bars.')
    # each token knows the heard words it came from
    assert vw.canonical(['twenty', 'four', 'hours']) == [('24', 0, 1), ('hours', 2, 2)]


def test_digest_keeps_what_a_test_needs():
    d = vw.digest(transcript(REPLY, unsure=('Nguyen',)))
    assert d['text'] == REPLY and d['speech'] is True
    assert [w['word'] for w in d['words']][-3:] == ['with', 'Dr.', 'Nguyen']
    last = d['words'][-1]
    assert last['shown'] == 'Nguyen.' and last['end'] is True and last['confidence'] == 0.41
    assert d['words'][0] == {'word': 'Your', 'shown': 'Your', 'startMs': 120, 'endMs': 420,
                             'confidence': 0.99}
    assert d['uncertain'] == ['Nguyen'] and 0.9 < d['confidence'] < 0.99
    assert d['speechStartMs'] == 120 and d['speechEndMs'] == 120 + 13 * 350 + 300
    assert d['longestPauseMs'] == 50 and 150 < d['wordsPerMinute'] < 190
    assert vw.public_words(d['words'])[0] == {'word': 'Your', 'startMs': 120, 'endMs': 420,
                                              'confidence': 0.99}
    assert vw.digest(transcript('Bonjour.', language='fr-FR'))['language'] == 'fr-FR'
    silent = vw.digest({'results': {'transcripts': [{'transcript': ''}], 'items': []}})
    assert silent == {'text': '', 'words': [], 'speech': False}
    # a transcript with no item list is still searchable
    bare = vw.digest({'results': {'transcripts': [{'transcript': 'Hello there. Bye.'}]}})
    assert [w['word'] for w in bare['words']] == ['Hello', 'there', 'Bye'] and bare['words'][1]['end']


def test_find_text_matches_on_meaning_not_spelling():
    words = words_of(REPLY)
    count, results = vw.find_text(words, 'tuesday the twenty fourth at three PM')
    assert count == 1
    r = results[0]
    assert r['matched'] == 'Tuesday the 24th at 3 p.m.' and r['word'] == 6 and r['words'] == 6
    assert r['startMs'] == 120 + 5 * 350 and r['confidence'] == 0.99 and 'similarity' not in r
    assert r['context'].startswith('Your next appointment') and r['context'].endswith('Nguyen.')
    assert vw.find_text(words, 'doctor nguyen')[0] == 1
    assert vw.find_text(words, 'Wednesday')[0] == 0
    # a near miss is found only when asked for, and a number is never "nearly" right
    heard = words_of('It is with Doctor Newin on the 24th.')
    assert vw.find_text(heard, 'Doctor Nguyen')[0] == 0
    count, results = vw.find_text(heard, 'Doctor Nguyen', fuzzy=0.5)
    assert count == 1 and results[0]['matched'] == 'Doctor Newin' and 0.5 < results[0]['similarity'] < 1
    assert vw.find_text(heard, 'the 25th', fuzzy=0.5)[0] == 0
    near = vw.closest(heard, 'Doctor Nguyen')
    assert near['heard'] == 'Doctor Newin' and near['word'] == 4 and 0.5 < near['similarity'] < 1
    # limit and counting
    many = words_of('yes yes yes no yes')
    count, results = vw.find_text(many, 'yes', limit=2)
    assert count == 4 and len(results) == 2 and [r['word'] for r in results] == [1, 2]


def test_find_between_captures_what_was_said():
    words = words_of('Your reference number is 4 8 3 9 2 1. Please keep it safe.')
    count, results, empty = vw.find_between(words, 'reference number is')
    assert (count, empty) == (1, 0) and results[0]['value'] == '4 8 3 9 2 1'
    assert results[0]['matched'] == 'reference number is 4 8 3 9 2 1'
    count, results, empty = vw.find_between(words_of(REPLY), 'appointment is on', 'with')
    assert count == 1 and results[0]['value'] == 'Tuesday the 24th at 3 p.m.'
    assert vw.find_between(words_of(REPLY), 'appointment is on', 'tomorrow') == (0, [], 0)
    # the marker closes the sentence: nothing follows it, and that is reported, not matched
    assert vw.find_between(words_of('Your code is. Sorry.'), 'code is') == (0, [], 1)


def test_compare_is_a_true_word_error_rate():
    words = words_of('Your next appointment is on Tuesday the 24th at three PM with Doctor Newin.')
    c = vw.compare(REPLY, words)
    assert c['same'] is False and c['expectedWords'] == 14 and c['heardWords'] == 14
    assert c['wordErrorRate'] == round(1 / 14, 3) and c['similarity'] == round(1 - 1 / 14, 3)
    assert c['differences'] == [{'expected': 'nguyen', 'heard': 'newin', 'word': 14,
                                 'startMs': 120 + 13 * 350}]
    assert c['expected'].endswith('3 pm with dr nguyen') and c['heard'].endswith('3 pm with dr newin')
    same = vw.compare('your next appointment is on tuesday the twenty-fourth at 3pm with dr nguyen'
                      .replace('3pm', '3 pm'), words_of(REPLY))
    assert same['same'] is True and same['similarity'] == 1.0 and same['differences'] == []
    # missing and extra words
    c = vw.compare('please hold the line', words_of('Please hold.'))
    assert c['wordErrorRate'] == 0.5 and c['differences'] == [{'expected': 'the line', 'heard': ''}]
    c = vw.compare('hello', words_of('Well hello there.'))
    assert c['wordErrorRate'] == 2.0 and c['similarity'] == 0.0
    assert [d['heard'] for d in c['differences']] == ['well', 'there']
    assert vw.compare('hello', [])['wordErrorRate'] == 1.0


def test_a_regex_match_is_given_back_as_heard_words():
    words = words_of(REPLY)
    text = vw.normalised(words)
    assert text == 'your next appointment is on tuesday the 24 at 3 pm with dr nguyen'
    r = vw.at(words, text.index('24'), len('24 at 3 pm'))
    assert r['matched'] == '24th at 3 p.m.' and r['word'] == 8 and r['words'] == 4
    written = vw.written(words)
    assert written == REPLY
    r = vw.at(words, written.index('Dr.'), len('Dr. Nguyen'), exact=True)
    assert r['matched'] == 'Dr. Nguyen' and r['word'] == 13
    assert vw.at([], 0, 1) is None


# ============================================================= the audio ====
def test_wav_is_rewritten_from_what_is_really_there():
    def riff(tag, channels, hz, rate, align, bits, body):
        return (b'RIFF' + struct.pack('<I', 36 + len(body)) + b'WAVE' + b'fmt ' +
                struct.pack('<IHHIIHH', 16, tag, channels, hz, rate, align, bits) +
                b'LIST' + struct.pack('<I', 4) + b'INFO' +
                b'data' + struct.pack('<I', len(body)) + body)
    plain, hz, channels, ms = va.wav_plain(riff(1, 1, 24000, 48000, 2, 16, PCM))
    assert plain == WAV and (hz, channels, ms) == (24000, 1, 1000)
    # the header's own idea of its byte rate is not believed
    assert va.wav_plain(riff(1, 1, 24000, 10 ** 9, 2, 16, PCM))[3] == 1000
    assert va.wav_info(riff(1, 1, 24000, 10 ** 9, 2, 16, PCM))[3] == 1000
    # telephone audio in a WAV becomes 16-bit
    plain, hz, channels, ms = va.wav_plain(riff(7, 1, 8000, 8000, 1, 8, b'\xff' * 4000))
    assert (hz, channels, ms) == (8000, 1, 500) and va.wav_info(plain) == (8000, 1, 16, 500)
    # ... and so does every other sample size: 8-bit (unsigned), 24-bit, 32-bit
    plain = va.wav_plain(riff(1, 1, 8000, 8000, 1, 8, bytes([0x80, 0xff, 0x00, 0x90])))[0]
    assert struct.unpack('<4h', plain[44:]) == (0, 32512, -32768, 4096)
    plain, hz, channels, ms = va.wav_plain(
        riff(1, 2, 48000, 288000, 6, 24, bytes([0x56, 0x34, 0x12, 0xff, 0xff, 0xff]) * 24000))
    assert struct.unpack('<2h', plain[44:48]) == (0x1234, -1) and (hz, channels, ms) == (48000, 2, 500)
    plain = va.wav_plain(riff(1, 1, 16000, 64000, 4, 32, struct.pack('<2i', 0x12345678, -2 ** 31)))[0]
    assert struct.unpack('<2h', plain[44:]) == (0x1234, -32768) and va.wav_info(plain)[2] == 16
    for bad, why in ((riff(3, 1, 24000, 96000, 4, 32, PCM), 'floating point'),
                     (riff(85, 1, 24000, 4000, 1, 0, PCM), 'compressed'),
                     (riff(1, 1, 4000, 8000, 2, 16, PCM), 'sample rate'),
                     (riff(1, 6, 24000, 288000, 12, 16, PCM), 'channels'),
                     (riff(1, 1, 24000, 48000, 2, 16, b''), 'no audio'), (b'RIFF0000WAVE', 'readable')):
        with pytest.raises(ValueError, match=why):
            va.wav_plain(bad)


def test_g711_and_padding():
    assert struct.unpack('<4h', va.g711_to_pcm(b'\xff\x00\x80\x7f', 'mulaw')) == (0, -32124, 32124, 0)
    assert struct.unpack('<4h', va.g711_to_pcm(b'\xd5\x55\xaa\x2a', 'alaw')) == (8, -8, 32256, -32256)
    short = va.wav(b'\x01\x00' * 1600, 16000)
    longer = va.lengthen_wav(short, 600)
    assert va.wav_info(short)[3] == 100 and 600 <= va.wav_info(longer)[3] <= 601
    assert longer[44:44 + 3200] == b'\x01\x00' * 1600 and set(longer[44 + 3200:]) == {0}
    assert va.lengthen_wav(longer, 600) is longer and va.lengthen_wav(b'not a wav', 600) == b'not a wav'


def test_mp3_is_reduced_to_its_frames():
    assert va.mp3_clean(MP3_FRAME * 50) == (MP3_FRAME * 50, 1200, 24000)
    summary = MP3_FRAME[:4] + bytes(20) + b'Info' + bytes(116)        # says how long the audio is
    dressed = (b'ID3\x04\x00\x00\x00\x00\x00\x0a' + bytes(10) + summary + MP3_FRAME * 25 +
               b'TAG' + bytes(125))
    assert va.mp3_clean(dressed) == (MP3_FRAME * 25, 600, 24000)
    broken = MP3_FRAME * 10 + b'junk\xff\xf3 junk' * 5 + MP3_FRAME * 10 + MP3_FRAME[:50]
    assert va.mp3_clean(broken) == (MP3_FRAME * 20, 480, 24000)
    assert va.mp3_clean(b'') is None and va.mp3_clean(bytes(1000)) is None
    assert va.mp3_clean(b'\xff' * 100000) is None


# ================================================================ listen ====
def test_listen_turns_posted_audio_into_words(transcribe):
    s, b = post('listen', WAV)
    assert s == 200, b
    ref = b['ref']
    assert ref[0] == 'h' and len(ref) == 12 and b['cached'] is False and b['pending'] is False
    assert b['text'] == REPLY
    assert b['normalised'] == 'your next appointment is on tuesday the 24 at 3 pm with dr nguyen'
    assert b['speech'] is True and b['confidence'] == 0.99 and b['uncertain'] == []
    assert (b['language'], b['format'], b['hz'], b['channels']) == ('en-AU', 'wav', 24000, 1)
    assert b['bytes'] == len(WAV) and b['durationMs'] == 1000 and 'paddedMs' not in b
    assert b['words'][0] == {'word': 'Your', 'startMs': 120, 'endMs': 420, 'confidence': 0.99}
    assert len(b['words']) == 14 and 'shown' not in b['words'][-1]
    assert b['getUrl'] == 'https://api.example.com/v1/voice/get?ref=' + ref
    assert b['playUrl'] == b['getUrl'] + '&play=true' and 'listenUrl' not in b
    # what Amazon Transcribe was asked
    asked, = transcribe.started
    assert asked['TranscriptionJobName'] == BUCKET + '-' + ref
    assert asked['Media'] == {'MediaFileUri': 's3://%s/heard/audio/%s.wav' % (BUCKET, ref)}
    assert asked['MediaFormat'] == 'wav' and asked['LanguageCode'] == 'en-AU'
    assert (asked['OutputBucketName'], asked['OutputKey']) == (BUCKET, 'heard/raw/%s.json' % ref)
    assert 'IdentifyLanguage' not in asked
    # the job is cleared away, the audio is kept, the second counted
    assert transcribe.deleted == [BUCKET + '-' + ref] and transcribe.jobs == {}
    assert s3_get('heard/audio/%s.wav' % ref) == WAV
    assert store.heard_today() == 1


def test_the_same_audio_is_transcribed_once(transcribe):
    first = post('listen', WAV)[1]
    transcribe.says = 'Something else entirely.'
    s, again = post('listen', WAV)
    assert s == 200 and again['cached'] is True and again['ref'] == first['ref']
    assert again['text'] == REPLY and again['words'] == first['words']
    s, by_ref = call('listen', {'ref': first['ref']})
    assert s == 200 and by_ref['cached'] is True and by_ref['text'] == REPLY
    assert len(transcribe.started) == 1 and store.heard_today() == 1
    # another language is another listening
    s, other = post('listen', WAV, {'language': 'en-us'})
    assert s == 200 and other['ref'] != first['ref'] and other['language'] == 'en-US'
    assert transcribe.started[-1]['LanguageCode'] == 'en-US'


def test_every_way_of_sending_the_same_audio_is_the_same_listening(transcribe):
    ref = post('listen', WAV)[1]['ref']
    b64 = base64.b64encode(WAV).decode()
    assert call('listen', None, 'POST', b64, {'Content-Type': 'text/plain'})[1]['ref'] == ref
    assert call('listen', None, 'POST', {'audio': b64})[1]['ref'] == ref
    # raw samples, as a speech-to-speech model streams them: pieces of base64
    pieces = [base64.b64encode(PCM[i:i + 5000]).decode() for i in range(0, len(PCM), 5000)]
    s, b = call('listen', None, 'POST', {'chunks': pieces, 'format': 'pcm', 'hz': 24000})
    assert s == 200 and b['ref'] == ref and b['cached'] is True and b['format'] == 'wav'
    s, b = post('listen', PCM, {'format': 'pcm', 'hz': '24000'})
    assert s == 200 and b['ref'] == ref
    assert len(transcribe.started) == 1
    # the same samples at another rate are different audio
    s, b = post('listen', PCM, {'format': 'pcm', 'hz': '16000'})
    assert s == 200 and b['ref'] != ref and b['hz'] == 16000 and b['durationMs'] == 1500


def test_listen_refuses_what_it_cannot_count(transcribe):
    def refused(data, qs=None):
        s, b = post('listen', data, qs)
        assert s == 400, b
        return b
    assert "'hz' is required" in refused(PCM, {'format': 'pcm'})['error']
    assert 'between 8000 and 48000' in refused(PCM, {'format': 'pcm', 'hz': '96000'})['error']
    assert refused(PCM, {'format': 'pcm', 'hz': 'fast'})['received'] == 'fast'
    assert 'Cannot tell what this audio is' in refused(PCM)['error']
    assert refused(WAV, {'format': 'pcm', 'hz': '24000'})['received'] == 'pcm'
    assert refused(WAV, {'format': 'mp3'})['detected'] == 'wav'
    assert refused(WAV, {'format': 'opus'})['allowed'] == ['wav', 'mp3', 'pcm', 'mulaw', 'alaw']
    for data, name in ((OGG, 'ogg'), (b'fLaC' + bytes(60), 'flac'),
                       (b'\x1a\x45\xdf\xa3' + bytes(60), 'webm'),
                       (bytes(4) + b'ftypM4A ' + bytes(60), 'm4a'), (b'#!AMR\n' + bytes(60), 'amr')):
        b = refused(data)
        assert b['detected'] == name and 'billed by the second' in b['reason'] and 'ffmpeg' in b['hint']
    assert 'floating point' in refused(WAV.replace(struct.pack('<HH', 1, 1), struct.pack('<HH', 3, 1), 1))['error']
    assert 'No MP3 audio frames' in refused(b'ID3\x04\x00\x00\x00\x00\x00\x00' + bytes(200))['error']
    big = refused(va.wav(bytes(vl.MAX_AUDIO_KB * 1024), 16000))
    assert big['maxKb'] == 4096
    assert call('listen', {'ref': 'h' + 'a' * 11}, 'POST', base64.b64encode(WAV).decode(), OCTET, b64=True)[0] == 400
    assert call('listen')[0] == 400 and 'POST the audio' in call('listen')[1]['hint']
    assert call('listen', None, 'POST', 'not base64 !!', {'Content-Type': 'audio/wav'})[0] == 400
    assert call('listen', None, 'POST', {'chunks': 'abc'})[0] == 400
    assert call('listen', None, 'POST', {'audio': 'QUJD', 'chunks': ['QUJD']})[0] == 400
    assert call('listen', None, 'POST', {'audio': 'QUJD', 'speed': 2})[1]['received'] == ['speed']
    assert call('listen', {'language': 'English'}, 'POST', {'audio': base64.b64encode(WAV).decode()})[0] == 400
    assert call('listen', {'ref': 'x123'})[0] == 400 and call('listen', {'ref': 'h' + 'a' * 11})[0] == 404
    # nothing above was sent to be transcribed, or counted
    assert transcribe.started == [] and store.heard_today() in (0, None)


def test_wait_and_pending(transcribe):
    transcribe.takes = 3
    s, b = post('listen', WAV, {'wait': '0'})
    assert s == 200 and b['pending'] is True and b['text'] is None and b['cached'] is False
    ref = b['ref']
    assert b['listenUrl'] == 'https://api.example.com/v1/voice/listen?ref=%s&wait=20' % ref
    assert 'words' not in b and 'normalised' not in b and b['durationMs'] == 1000
    assert 'pending is false' in b['comment']
    # the audio can already be played, and the job is still there
    assert call('get', {'ref': ref})[0] == 200 and len(transcribe.jobs) == 1
    s, page = call('get', {'ref': ref, 'play': 'true'})
    assert s == 200 and ref + '.wav' in page['body'] and 'text' not in call('get', {'ref': ref})[1]
    s, b = call('listen', {'ref': ref, 'wait': '0'})
    assert s == 200 and b['pending'] is True
    # sending the audio again follows the same job rather than starting another
    s, b = post('listen', WAV)
    assert s == 200 and b['pending'] is False and b['text'] == REPLY and b['cached'] is False
    assert len(transcribe.started) == 1 and store.heard_today() == 1
    assert call('listen', {'ref': ref})[1]['cached'] is True
    assert call('listen', {'ref': ref, 'wait': 'soon'})[0] == 400


def test_short_and_telephone_audio(transcribe):
    s, b = post('listen', va.wav(b'\x01\x00' * 1600, 16000))          # a tenth of a second
    assert s == 200 and b['paddedMs'] >= 500 and 600 <= b['durationMs'] <= 601
    assert va.wav_info(s3_get('heard/audio/%s.wav' % b['ref']))[3] == b['durationMs']
    s, b = post('listen', b'\xff' * 8000, {'format': 'mulaw'})
    assert s == 200 and (b['format'], b['hz'], b['durationMs']) == ('wav', 8000, 1000)
    assert va.wav_info(s3_get('heard/audio/%s.wav' % b['ref'])) == (8000, 1, 16, 1000)
    s, b = post('listen', b'\xd5' * 16000, {'format': 'alaw', 'hz': '16000'})
    assert s == 200 and (b['hz'], b['durationMs']) == (16000, 1000)


def test_mp3_is_sent_on_as_frames_only(transcribe):
    summary = MP3_FRAME[:4] + bytes(20) + b'Xing' + bytes(116)
    s, b = post('listen', b'ID3\x04\x00\x00\x00\x00\x00\x0a' + bytes(10) + summary + MP3_FRAME * 50)
    assert s == 200 and (b['format'], b['hz'], b['durationMs']) == ('mp3', 24000, 1200)
    assert s3_get('heard/audio/%s.mp3' % b['ref']) == MP3_FRAME * 50
    assert transcribe.started[0]['MediaFormat'] == 'mp3' and store.heard_today() == 2
    # ... so the bare frames are the same listening
    assert post('listen', MP3_FRAME * 50)[1]['cached'] is True


def test_a_job_that_fails(transcribe):
    transcribe.failure = 'The media format that you specified does not match the detected media format.'
    s, b = post('listen', WAV)
    assert s == 422 and b['state'] == 'failed' and 'does not match' in b['detail']
    ref = b['ref']
    assert ref in b['hint'] and transcribe.jobs == {}
    assert call('listen', {'ref': ref})[0] == 422 and call('find', {'ref': ref, 'text': 'x'})[0] == 422
    assert call('compare', {'ref': ref, 'text': 'x'})[0] == 422
    assert len(transcribe.started) == 1
    # the audio is still there to be heard, and sending it again tries again
    assert call('get', {'ref': ref})[0] == 200
    transcribe.failure = None
    s, b = post('listen', WAV)
    assert s == 200 and b['ref'] == ref and b['text'] == REPLY and len(transcribe.started) == 2
    # an Ogg utterance is passed on as Polly made it; if that is refused, the hint says what to do
    transcribe.failure = 'Unsupported codec.'
    s, b = call('listen', {'ref': say(text='Hello', format='ogg')[1]['ref']})
    assert s == 422 and 'format=wav' in b['hint']


def test_transcribe_errors_are_named_and_cost_nothing(transcribe):
    for code, status in (('BadRequestException', 400), ('LimitExceededException', 429),
                         ('InternalFailureException', 502)):
        transcribe.fail = code
        s, b = post('listen', WAV)
        assert s == status and b['detail'] == 'transcribe said no', b
    assert store.heard_today() == 0
    assert call('listen', {'ref': 'h' + 'a' * 11})[0] == 404        # nothing was recorded as heard
    transcribe.fail = None
    assert post('listen', WAV)[0] == 200


def test_one_job_however_many_are_listening(transcribe):
    ref = store.heard_ref(WAV, 'en-AU', vl.RECIPE)
    name = BUCKET + '-' + ref
    # another request has already started this audio's job, and has not recorded it yet
    transcribe.jobs[name] = {'checks': 0, 'status': 'IN_PROGRESS', 'kw': {
        'OutputBucketName': BUCKET, 'OutputKey': 'heard/raw/%s.json' % ref, 'LanguageCode': 'en-AU'}}
    s, b = post('listen', WAV)
    assert s == 200 and b['ref'] == ref and b['text'] == REPLY
    assert transcribe.started == [] and store.heard_today() == 0     # followed, not paid for twice


def test_followers_mostly_watch_the_record_not_transcribe(transcribe):
    transcribe.takes = 9
    ref = post('listen', WAV, {'wait': '0'})[1]['ref']               # started here: 1 status check
    name = BUCKET + '-' + ref
    assert transcribe.jobs[name]['checks'] == 1
    # Another request waits on the same job. Time is made to pass in looks, not seconds.
    looks = iter(range(1000))
    real = vl.time.monotonic
    try:
        vl.time.monotonic = lambda: next(looks) * 0.5
        s, b = call('listen', {'ref': ref, 'wait': '10'})
    finally:
        vl.time.monotonic = real
    assert s == 200 and b['pending'] is True
    asked = transcribe.jobs[name]['checks'] - 1
    assert 3 <= asked <= 6                                            # ~20 looks, one in four to Transcribe
    # ... and it still finishes the job itself if nobody else does
    transcribe.takes = 1
    assert call('listen', {'ref': ref})[1]['text'] == REPLY


def test_a_stale_job_of_the_same_name_is_replaced(transcribe):
    ref = store.heard_ref(WAV, 'en-AU', vl.RECIPE)
    name = BUCKET + '-' + ref
    transcribe.jobs[name] = {'checks': 9, 'status': 'COMPLETED', 'kw': {}}      # its transcript has expired
    s, b = post('listen', WAV)
    assert s == 200 and b['text'] == REPLY
    assert transcribe.deleted[0] == name and len(transcribe.started) == 1 and store.heard_today() == 1


def test_a_job_finished_by_someone_else(transcribe):
    transcribe.takes = 5
    ref = post('listen', WAV, {'wait': '0'})[1]['ref']
    # the job completes and is cleared away without this record hearing of it
    boto3.client('s3', region_name=REGION).put_object(
        Bucket=BUCKET, Key='heard/raw/%s.json' % ref, Body=json.dumps(transcript('All done.')).encode())
    transcribe.jobs.clear()
    s, b = call('listen', {'ref': ref})
    assert s == 200 and b['text'] == 'All done.' and b['pending'] is False


def test_against_the_real_client_and_motos_transcribe(transcribe):
    """The requests as boto3 itself validates and sends them, and the error
    codes as moto models them. (moto runs the job's states but writes no
    transcript, so the test writes it where the job was told to.)"""
    del store._clients['transcribe']                    # the real client, talking to moto
    s, b = post('listen', WAV, {'wait': '0'})
    assert s == 200 and b['pending'] is True, b
    ref = b['ref']
    real = boto3.client('transcribe', region_name=REGION)
    job = real.get_transcription_job(TranscriptionJobName=BUCKET + '-' + ref)['TranscriptionJob']
    assert job['LanguageCode'] == 'en-AU' and job['MediaFormat'] == 'wav'
    assert job['Media']['MediaFileUri'] == 's3://%s/heard/audio/%s.wav' % (BUCKET, ref)
    boto3.client('s3', region_name=REGION).put_object(
        Bucket=BUCKET, Key='heard/raw/%s.json' % ref, Body=json.dumps(transcript(REPLY)).encode())
    s, b = call('listen', {'ref': ref})
    assert s == 200 and b['pending'] is False and b['text'] == REPLY, b
    with pytest.raises(ClientError) as gone:            # cleared away once finished
        real.get_transcription_job(TranscriptionJobName=BUCKET + '-' + ref)
    assert gone.value.response['Error']['Code'] in vl.GONE
    # a job of the same name already running: ConflictException, and it is followed
    other = store.heard_ref(WAV, 'en-GB', vl.RECIPE)
    real.start_transcription_job(
        TranscriptionJobName=BUCKET + '-' + other, LanguageCode='en-GB', MediaFormat='wav',
        Media={'MediaFileUri': 's3://%s/heard/audio/%s.wav' % (BUCKET, other)},
        OutputBucketName=BUCKET, OutputKey='heard/raw/%s.json' % other)
    s, b = post('listen', WAV, {'language': 'en-GB', 'wait': '0'})
    assert s == 200 and b['ref'] == other and b['pending'] is True and store.heard_today() == 1
    # identifying the language is a request boto3 accepts too
    s, b = post('listen', WAV, {'language': 'auto', 'wait': '0'})
    assert s == 200 and b['pending'] is True, b


def test_the_listening_budget(transcribe, monkeypatch):
    monkeypatch.setenv('VOICE_DAILY_LISTEN_SECONDS', '2')
    two = va.wav(PCM + PCM[:2400], 24000)                     # 1.1 s: counted as 2
    assert post('listen', two)[0] == 200 and store.heard_today() == 2
    s, b = post('listen', WAV)
    assert s == 429 and b['budgetExhausted'] is True and b['dailyListenSeconds'] == 2
    assert b['retryAfterSeconds'] > 0 and len(transcribe.started) == 1
    assert post('listen', two)[1]['cached'] is True           # what has been heard still works
    s, b = post('listen', va.wav(PCM * 3, 24000))             # longer than the whole day
    assert s == 400 and b['seconds'] == 3 and b['dailyListenSeconds'] == 2
    monkeypatch.setenv('VOICE_DAILY_LISTEN_SECONDS', '0')
    s, b = post('listen', WAV)
    assert s == 403 and b['listeningDisabled'] is True
    assert post('listen', two)[0] == 200


def test_language_can_be_identified(transcribe):
    transcribe.says, transcribe.spoken, transcribe.takes = 'Bonjour, comment puis-je vous aider?', 'fr-FR', 2
    s, b = post('listen', WAV, {'language': 'auto', 'wait': '0'})
    assert s == 200 and b['pending'] is True and b['language'] == 'auto'
    asked, = transcribe.started
    assert asked['IdentifyLanguage'] is True and 'LanguageCode' not in asked
    s, b = call('listen', {'ref': b['ref']})
    assert s == 200 and b['language'] == 'fr-FR' and b['text'].startswith('Bonjour')


# ===================================== said, heard and saved audio together ====
def test_listening_to_what_was_said(transcribe):
    said = say(text='When is my next appointment?')[1]
    transcribe.says = 'When is my next appointment?'
    s, b = call('listen', {'ref': said['ref']})
    assert s == 200 and b['ref'][0] == 'h' and b['text'] == 'When is my next appointment?'
    assert (b['format'], b['durationMs']) == ('mp3', 1200) and transcribe.started[0]['MediaFormat'] == 'mp3'
    assert call('listen', {'ref': said['ref']})[1]['cached'] is True
    # every format voice/say makes can be listened to - Ogg too, because its length is known
    for fmt, media, ms in (('ogg', 'ogg', 1500), ('wav', 'wav', 600), ('pcm', 'wav', 600),
                           ('mulaw', 'wav', 600), ('alaw', 'wav', 600)):
        made = say(text='Hello', format=fmt)[1]
        s, b = call('listen', {'ref': made['ref']})
        assert s == 200 and (b['format'], b['durationMs']) == (media, ms), (fmt, b)
    assert transcribe.started[1]['Media']['MediaFileUri'].endswith('.ogg')


def test_heard_audio_can_be_played_and_kept(transcribe):
    ref = post('listen', PCM, {'format': 'pcm', 'hz': '24000'})[1]['ref']
    s, g = call('get', {'ref': ref})
    assert s == 200 and g['source'] == 'heard' and g['text'] == REPLY and g['format'] == 'wav'
    assert base64.b64decode(g['data']) == WAV and g['contentType'] == 'audio/wav'
    assert g['filename'] == ref + '.wav' and g['durationMs'] == 1000
    s, page = call('get', {'ref': ref, 'play': 'true'})
    assert s == 200 and page['headers']['Content-Type'].startswith('text/html') and 'Nguyen' in page['body']
    s, r = call('get', {'ref': ref, 'view': 'true'})
    assert s == 302 and 'heard/audio/%s.wav' % ref in r['headers']['Location']
    # there is no script behind it to say again
    s, b = call('say', {'ref': ref})
    assert s == 400 and 'heard' in b['error']
    # kept under a key, it is found again by listening to the key
    s, k = call('save', {'key': 'reply-1', 'ref': ref})
    assert s == 200 and k['saved'] is True and k['source'] == 'heard' and k['filename'] == 'reply-1.wav'
    s, g = call('get', {'key': 'reply-1'})
    assert s == 200 and g['savedFrom'] == 'heard' and g['ref'] == ref and base64.b64decode(g['data']) == WAV
    s, b = call('listen', {'key': 'reply-1'})
    assert s == 200 and b['ref'] == ref and b['cached'] is True
    assert call('say', {'key': 'reply-1'})[0] == 400
    assert call('save', {'key': 'x', 'ref': 'h' + 'a' * 11})[0] == 404


def test_listening_to_a_saved_recording(transcribe):
    assert call('save', {'key': 'caller'}, 'POST', base64.b64encode(WAV).decode(), OCTET, b64=True)[0] == 200
    s, b = call('listen', {'key': 'caller'})
    assert s == 200 and b['text'] == REPLY and b['ref'] == store.heard_ref(WAV, 'en-AU', vl.RECIPE)
    # an uploaded recording gets no more trust under a key than in a body
    assert call('save', {'key': 'opus'}, 'POST', base64.b64encode(OGG).decode(), OCTET, b64=True)[0] == 200
    s, b = call('listen', {'key': 'opus'})
    assert s == 400 and b['detected'] == 'ogg'
    # raw samples saved under a name that says so need their rate
    assert call('save', {'key': 'raw', 'filename': 'reply.pcm'}, 'POST',
                base64.b64encode(PCM).decode(), OCTET, b64=True)[1]['format'] == 'pcm'
    assert "'hz' is required" in call('listen', {'key': 'raw'})[1]['error']
    assert call('listen', {'key': 'raw', 'hz': '24000'})[1]['cached'] is True
    assert call('listen', {'key': 'nobody'})[0] == 404
    # a pinned utterance is trusted, as its ref is
    said = say(text='Hello', format='ogg')[1]
    call('save', {'key': 'hello', 'ref': said['ref']})
    assert call('listen', {'key': 'hello'})[1]['format'] == 'ogg'


# ================================================================== find ====
def test_find_words_in_what_was_heard(transcribe):
    ref = post('listen', WAV)[1]['ref']
    s, b = call('find', {'ref': ref, 'text': 'Tuesday the twenty-fourth at 3pm'.replace('3pm', '3 PM')})
    assert s == 200 and b['found'] is True and b['count'] == 1 and b['truncated'] is False, b
    assert b['match'] == {'mode': 'words', 'text': 'Tuesday the twenty-fourth at 3 PM',
                          'normalised': 'tuesday the 24 at 3 pm', 'fuzzy': 1.0, 'context': 5}
    r, = b['results']
    assert r['matched'] == 'Tuesday the 24th at 3 p.m.' and r['word'] == 6 and r['startMs'] == 1870
    assert b['text'] == REPLY and b['normalised'].startswith('your next appointment')
    assert b['confidence'] == 0.99 and 'values' not in b and 'hint' not in b and b['pending'] is False
    # a miss says what was nearest
    transcribe.says = 'It is with Doctor Newin on the 24th.'
    other = post('listen', PCM, {'format': 'pcm', 'hz': '16000'})[1]['ref']
    s, b = call('find', {'ref': other, 'text': 'Dr Nguyen'})
    assert s == 200 and b['found'] is False and b['count'] == 0 and b['results'] == []
    assert b['closest']['heard'] == 'Doctor Newin' and b['closest']['similarity'] == 0.773
    assert 'fuzzyCount' not in b            # too far apart in spelling for fuzzy=true to accept
    assert "'normalised' is what is searched" in b['hint']
    s, b = call('find', {'ref': other, 'text': 'Dr Nguyen', 'fuzzy': '0.5'})
    assert b['found'] is True and b['results'][0]['matched'] == 'Doctor Newin'
    assert b['results'][0]['similarity'] == 0.773 and b['match']['fuzzy'] == 0.5
    s, b = call('find', {'ref': other, 'text': 'Doctor Newan'})
    assert b['found'] is False and b['fuzzyCount'] == 1 and 'fuzzy=true' in b['hint']
    assert call('find', {'ref': other, 'text': 'Doctor Newan', 'fuzzy': 'true'})[1]['found'] is True
    assert call('find', {'ref': other, 'text64': base64.b64encode(b'the 24th').decode()})[1]['count'] == 1


def test_find_with_a_pattern_or_markers(transcribe):
    ref = post('listen', WAV)[1]['ref']
    # a regex runs on the normalised words, so it need not guess how a number was written
    s, b = call('find', {'ref': ref, 'regex': r'the (\d+) at (\d+) (am|pm)'})
    assert s == 200 and b['found'] is True and b['values'] == ['24'], b
    r, = b['results']
    assert r['groups'] == ['24', '3', 'pm'] and r['matched'] == 'the 24th at 3 p.m.' and r['word'] == 7
    assert b['match'] == {'mode': 'regex', 'regex': r'the (\d+) at (\d+) (am|pm)', 'exact': False,
                          'ignoreCase': True, 'context': 5}
    assert call('find', {'ref': ref, 'regex': 'TUESDAY'})[1]['found'] is True
    # exact=true: the transcript as it was written
    s, b = call('find', {'ref': ref, 'regex': r'Dr\. (\w+)', 'exact': 'true'})
    assert b['values'] == ['Nguyen'] and b['results'][0]['matched'] == 'Dr. Nguyen'
    assert call('find', {'ref': ref, 'regex': 'tuesday', 'exact': 'true'})[1]['found'] is False
    assert call('find', {'ref': ref, 'regex': 'tuesday', 'exact': 'true', 'ignorecase': 'true'})[1]['found'] is True
    s, b = call('find', {'ref': ref, 'text': '24th at 3 p.m.', 'exact': 'true'})
    assert b['found'] is True and b['match']['mode'] == 'literal' and 'values' not in b
    assert call('find', {'ref': ref, 'text': '24 at 3 pm', 'exact': 'true'})[1]['found'] is False
    # what was said between two markers
    s, b = call('find', {'ref': ref, 'left': 'appointment is on', 'right': 'with'})
    assert b['values'] == ['Tuesday the 24th at 3 p.m.'] and b['match']['mode'] == 'boundaries'
    s, b = call('find', {'ref': ref, 'left': 'with'})
    assert b['values'] == ['Dr. Nguyen']
    s, b = call('find', {'ref': ref, 'left': 'Dr Nguyen'})
    assert b['found'] is False and b['emptyMatches'] == 1 and 'nothing followed it' in b['hint']
    s, b = call('find', {'ref': ref, 'left': 'your balance is'})
    assert b['found'] is False and b['closest']['similarity'] < 1
    # limit and context
    transcribe.says = 'Yes. Yes. Yes. No. Yes.'
    many = post('listen', PCM, {'format': 'pcm', 'hz': '8000'})[1]['ref']
    s, b = call('find', {'ref': many, 'text': 'yes', 'limit': '2', 'context': '0'})
    assert b['count'] == 4 and b['truncated'] is True and [r['context'] for r in b['results']] == ['Yes.', 'Yes.']


def test_find_checks_the_question_before_paying_for_the_answer(transcribe):
    for qs in ({}, {'text': 'a', 'regex': 'b'}, {'right': 'x'}, {'text': 'a', 'right': 'b'},
               {'regex': '(unclosed'}, {'regex': 'x' * 2001}, {'text': '?!'}, {'left': '...'},
               {'left': 'a', 'exact': 'true'}, {'regex': 'a', 'fuzzy': 'true'},
               {'text': 'a', 'fuzzy': '0.2'}, {'text': 'a', 'exact': 'maybe'},
               {'text': 'a', 'limit': 'few'}):
        s, b = post('find', WAV, qs)
        assert s == 400, (qs, b)
    assert transcribe.started == []
    assert call('find', {'text': 'hello'})[0] == 400                 # no audio named
    # audio can go straight to find: one request to ask "did it say this?"
    s, b = post('find', WAV, {'text': 'Dr Nguyen'})
    assert s == 200 and b['found'] is True and b['ref'][0] == 'h'
    s, b = call('find', None, 'POST', {'audio': base64.b64encode(WAV).decode(), 'left': 'with'})
    assert s == 200 and b['values'] == ['Dr. Nguyen']


def test_find_while_the_words_are_not_ready(transcribe):
    transcribe.takes = 4
    s, b = post('find', WAV, {'text': 'Tuesday', 'wait': '0'})
    assert s == 200 and b['pending'] is True and b['found'] is None and b['count'] is None
    assert b['results'] == [] and b['ref'] in b['comment'] and b['ref'] in b['listenUrl']
    s, b = call('find', {'ref': b['ref'], 'text': 'Tuesday'})
    assert s == 200 and b['pending'] is False and b['found'] is True
    # silence is an answer too
    transcribe.says = ''
    s, b = post('find', PCM, {'format': 'pcm', 'hz': '8000', 'text': 'hello'})
    assert s == 200 and b['found'] is False and b['speech'] is False and b['text'] == ''
    assert b['hint'] == 'no speech was heard in this audio' and 'closest' not in b


# =============================================================== compare ====
def test_compare_what_was_heard_with_what_was_expected(transcribe):
    transcribe.says = 'Your next appointment is on Tuesday the 24th at three PM with Doctor Newin.'
    transcribe.unsure = ('Newin',)
    ref = post('listen', WAV)[1]['ref']
    s, b = call('compare', {'ref': ref, 'text': REPLY, 'min': '0.9'})
    assert s == 200 and b['pass'] is True and b['min'] == 0.9 and b['same'] is False, b
    assert b['similarity'] == 0.929 and b['wordErrorRate'] == 0.071
    assert (b['expectedWords'], b['heardWords']) == (14, 14) and b['differencesTruncated'] is False
    assert b['differences'] == [{'expected': 'nguyen', 'heard': 'newin', 'word': 14, 'startMs': 4670}]
    assert b['expectedText'] == REPLY and b['text'] == transcribe.says
    assert b['expectedNormalised'].endswith('dr nguyen') and b['normalised'].endswith('dr newin')
    assert b['uncertain'] == ['Newin'] and b['pending'] is False and 'roundTrip' not in b
    assert call('compare', {'ref': ref, 'text': REPLY, 'min': '0.95'})[1]['pass'] is False
    s, b = call('compare', {'ref': ref, 'text': transcribe.says.upper()})
    assert b['same'] is True and b['similarity'] == 1.0 and 'pass' not in b and b['differences'] == []
    # audio and question in one request
    s, b = call('compare', None, 'POST', {'audio': base64.b64encode(WAV).decode(), 'text': REPLY,
                                         'min': 0.5})
    assert s == 200 and b['ref'] == ref and b['pass'] is True


def test_compare_round_trip_and_with_other_audio(transcribe):
    said = say(text='[calm] When is my next appointment?')[1]
    transcribe.says = 'When is my next appointment?'
    s, b = call('compare', {'ref': said['ref']})
    assert s == 200 and b['roundTrip'] is True and b['same'] is True and b['ref'][0] == 'h', b
    assert b['expectedText'] == 'When is my next appointment?'
    call('save', {'key': 'question', 'ref': said['ref']})
    assert call('compare', {'key': 'question', 'min': '1'})[1]['pass'] is True
    # against other audio: what was said, or what was heard another time
    transcribe.says = 'When is my next appointment please?'
    heard = post('listen', WAV)[1]['ref']
    s, b = call('compare', {'ref': heard, 'otherref': said['ref']})
    assert s == 200 and b['same'] is False and b['differences'] == [
        {'expected': '', 'heard': 'please', 'word': 6, 'startMs': 1870}]
    assert 'otherRef' not in b and 'roundTrip' not in b
    again = post('listen', PCM, {'format': 'pcm', 'hz': '8000'})[1]['ref']
    s, b = call('compare', {'ref': heard, 'otherref': again})
    assert s == 200 and b['same'] is True and b['otherRef'] == again
    assert call('compare', {'ref': heard, 'otherkey': 'question'})[1]['similarity'] == 0.8


def test_compare_checks_the_question_first(transcribe, monkeypatch):
    ref = post('listen', WAV)[1]['ref']
    started = len(transcribe.started)
    s, b = call('compare', {'ref': ref})
    assert s == 400 and "Missing 'text'" in b['error'] and 'voice/say' in b['hint']
    assert post('compare', PCM, {'format': 'pcm', 'hz': '8000'})[0] == 400       # nothing to compare with
    for qs in ({'text': 'a', 'otherref': ref}, {'text': '...'}, {'text': 'a', 'min': '2'},
               {'text': 'a', 'min': 'most'}, {'otherref': 'nope'}, {'otherref': ref, 'otherkey': 'k'}):
        s, b = call('compare', dict(qs, ref=ref))
        assert s == 400, (qs, b)
    assert call('compare', {'ref': ref, 'otherref': 'v' + 'a' * 11})[0] == 404
    assert call('compare', {'text': 'hello'})[0] == 400
    assert len(transcribe.started) == started
    monkeypatch.setattr(vl, 'MAX_COMPARE_CELLS', 20)
    s, b = call('compare', {'ref': ref, 'text': 'please hold the line'})
    assert s == 400 and (b['expectedWords'], b['heardWords']) == (4, 14)
    transcribe.takes = 4
    s, b = post('compare', PCM, {'format': 'pcm', 'hz': '8000', 'text': 'hello', 'wait': '0'})
    assert s == 200 and b['pending'] is True and b['same'] is None and b['similarity'] is None
    assert 'pass' not in b and 'listenUrl' in b


def test_the_service_lists_its_listening_actions():
    s, b = call('')
    assert s == 200 and {'listen', 'find', 'compare'} <= set(b['actions'])
    assert 'listen' in b['description']
