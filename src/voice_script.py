"""voice_script - the neutral script behind voice/say, and its renderers.

Text carrying ElevenLabs-style [tags], or a list of segments, is parsed into
ONE structure, the script: what to say, with what direction, and where the
pauses and non-verbal cues sit. The script - not the SSML - is what is stored
beside the audio and hashed into the ref.

A renderer turns the script into what one engine accepts (SSML for Amazon
Polly today) and report() says, tag by tag, how faithfully that engine could
follow it: native, approximated, inferred or none. When an AWS voice service
takes emotion natively, that is a new renderer plus a new row in CAPABILITY;
callers and stored scripts do not change.

No AWS calls in this module: everything here is tested directly.
"""
import re
import unicodedata
from collections import namedtuple
from xml.sax.saxutils import escape, quoteattr

from common import ApiError

# Bump when the way ANY tag is rendered changes. It is hashed into every ref,
# so an old ref keeps returning the audio it always did and the same request
# gets a new ref (and new audio) under the new rules.
RECIPE = 1

ENGINES = ('standard', 'neural', 'generative')

MAX_CHARACTERS = 3000     # Polly SynthesizeSpeech: billed characters per call
MAX_SSML = 6000           # ... and total characters including markup
MAX_PAUSE_MS = 10000      # one SSML <break>
MAX_SEGMENTS = 100
MAX_PRONOUNCE = 50

# ----------------------------------------------------------- vocabulary ----
# emotion: (rate %, pitch % - standard voices only, volume or None, words)
# Polly has no emotion control, so each is a prosody recipe: a nudge that
# makes the intent audible, not an acted performance.
EMOTIONS = {
    'anxious':    (112, 8, None, ('anxious', 'anxiously', 'worried', 'nervous', 'nervously',
                                  'uneasy', 'concerned')),
    'panicked':   (124, 12, 'loud', ('panicked', 'panicking', 'panic', 'frantic', 'scared',
                                     'afraid', 'terrified', 'fearful')),
    'upset':      (108, 5, None, ('upset', 'distressed', 'agitated')),
    'angry':      (106, -5, 'x-loud', ('angry', 'angrily', 'furious', 'irate', 'mad')),
    'frustrated': (106, -2, 'loud', ('frustrated', 'annoyed', 'irritated', 'impatient',
                                     'exasperated')),
    'sad':        (86, -8, 'soft', ('sad', 'sadly', 'sorrowful', 'unhappy', 'disappointed',
                                    'tearful', 'crying')),
    'tired':      (84, -6, 'soft', ('tired', 'exhausted', 'weary', 'sleepy')),
    'relaxed':    (92, -3, None, ('relaxed', 'calm', 'calmly', 'at ease', 'reassuring')),
    'happy':      (104, 6, None, ('happy', 'happily', 'cheerful', 'cheerfully', 'pleased',
                                  'glad')),
    'excited':    (112, 10, 'loud', ('excited', 'excitedly', 'thrilled', 'enthusiastic',
                                     'delighted')),
    'confused':   (92, 4, None, ('confused', 'unsure', 'uncertain', 'hesitant', 'puzzled')),
    'curious':    (100, 6, None, ('curious', 'inquisitive')),
    'surprised':  (104, 12, None, ('surprised', 'shocked', 'amazed', 'astonished', 'awe')),
    'sarcastic':  (92, -4, None, ('sarcastic', 'sarcastically', 'dismissive')),
}
# pace: (rate %, words)
PACE = {
    'slow':  (80, ('slowly', 'slow', 'deliberately')),
    'drawn': (70, ('drawn out',)),
    'fast':  (125, ('quickly', 'fast', 'rushed', 'hurried', 'rapidly')),
}
# volume: (SSML volume, words). 'whisper' is the whispered voice effect where
# the engine has one, and the quietest volume where it has not.
VOLUME = {
    'soft':    ('soft', ('softly', 'soft', 'quietly', 'quiet', 'gently')),
    'loud':    ('loud', ('loudly', 'loud')),
    'shout':   ('x-loud', ('shouts', 'shouting', 'shout', 'yells', 'yelling')),
    'whisper': ('x-soft', ('whispers', 'whisper', 'whispering', 'whispered')),
}
PAUSES = {'pause': 500, 'short pause': 250, 'long pause': 1200, 'beat': 300}
RESET = ('neutral', 'normal', 'normally')
SOUNDS = ('sighs', 'sigh', 'sighing', 'laughs', 'laugh', 'laughing', 'big laugh', 'chuckles',
          'giggles', 'gasps', 'gasp', 'clears throat', 'coughs', 'cough', 'coughing', 'sniffs',
          'groans', 'yawns', 'exhales', 'inhales')

# What each engine can do with each kind of direction (voice/voices, voice/tags).
CAPABILITY = {
    'standard':   {'pause': 'native', 'pace': 'native', 'volume': 'native', 'whisper': 'native',
                   'emotion': 'approximated', 'sound': 'none'},
    'neural':     {'pause': 'native', 'pace': 'native', 'volume': 'native',
                   'whisper': 'approximated', 'emotion': 'approximated', 'sound': 'none'},
    'generative': {'pause': 'native', 'pace': 'approximated', 'volume': 'approximated',
                   'whisper': 'approximated', 'emotion': 'inferred', 'sound': 'none'},
}

TAGS = {}
for _name, (_r, _p, _v, _words) in EMOTIONS.items():
    for _w in _words:
        TAGS[_w] = ('emotion', _name)
for _name, (_r, _words) in PACE.items():
    for _w in _words:
        TAGS[_w] = ('pace', _name)
for _name, (_v, _words) in VOLUME.items():
    for _w in _words:
        TAGS[_w] = ('volume', _name)
for _w, _ms in PAUSES.items():
    TAGS[_w] = ('pause', _ms)
for _w in RESET:
    TAGS[_w] = ('reset', None)
for _w in SOUNDS:
    TAGS[_w] = ('sound', _w)

Direction = namedtuple('Direction', 'emotion pace volume')
PLAIN = Direction(None, None, None)

# [tag]: starts with a letter, so "[1]" and "[...]" stay as text. \[ is a literal bracket.
_TAG = re.compile(r"\\\[|\[\s*([A-Za-z][A-Za-z0-9 .'\-]{0,38}?)\s*\]")
_PAUSE = re.compile(r'^pause\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?)?$')
_SENTENCE_END = re.compile(r'[.!?…]+["\')\]]*(?=\s|$)')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def clean(text):
    """NFC, control characters dropped, whitespace runs collapsed, trimmed.
    Case and punctuation are kept: both change how a voice says a sentence."""
    return ' '.join(_CONTROL.sub('', unicodedata.normalize('NFC', str(text))).split())


def lookup(word):
    """A tag's words -> (kind, value). Unknown words are kept, never spoken."""
    w = ' '.join(str(word).lower().split())
    m = _PAUSE.match(w)
    if m:
        ms = float(m.group(1)) * (1000 if (m.group(2) or 'ms').startswith('s') else 1)
        return 'pause', int(round(ms))
    if w in TAGS:
        return TAGS[w]
    if w.endswith(' accent') or w.endswith(' voice'):
        return 'accent', w
    return 'unknown', w


def emotion_name(word):
    """The 'emotion' parameter: one emotion word -> its canonical name, or 400."""
    kind, value = lookup(word) if isinstance(word, str) else ('unknown', None)
    if kind != 'emotion':
        raise ApiError(400, "'emotion' must be one emotion word. Put pace, volume, pauses and "
                            "anything else in the text as [tags] (see voice/tags).",
                       received=word, allowed=sorted(EMOTIONS))
    return value


# --------------------------------------------------------------- parsing ----
def _direction_of(item):
    return {k: v for k, v in item.items() if k != 'say'}


class _Builder:
    def __init__(self):
        self.items = []
        self.tags = []
        self.length = 0        # characters of spoken text so far

    def say(self, text, d):
        text = clean(text)
        if not text:
            return
        item = {'say': text}
        item.update({k: v for k, v in d._asdict().items() if v})
        last = self.items[-1] if self.items else None
        if last and 'say' in last and _direction_of(last) == _direction_of(item):
            last['say'] += ' ' + text
        else:
            self.items.append(item)
        self.length += len(text) + (1 if self.length else 0)

    def pause(self, ms):
        ms = max(0, min(MAX_PAUSE_MS, int(ms)))
        if ms:
            self.items.append({'pause': ms})
        return ms

    def tag(self, word, kind, value):
        t = {'tag': ' '.join(str(word).lower().split()), 'kind': kind, 'at': self.length}
        if kind == 'pause':
            t['ms'] = value
        elif value and kind in ('emotion', 'pace', 'volume'):
            t['value'] = value
        self.tags.append(t)
        return t


def _inline(b, text, d):
    """Walk text, speaking what lies between [tags] and applying each tag."""
    if not isinstance(text, str):
        raise ApiError(400, "'text' must be a string.")
    buf, pos = '', 0
    for m in _TAG.finditer(text):
        buf += text[pos:m.start()]
        pos = m.end()
        if m.group(1) is None:          # \[ - a literal bracket
            buf += '['
            continue
        b.say(buf, d)
        buf = ''
        kind, value = lookup(m.group(1))
        if kind == 'pause':
            got = b.pause(value)
            t = b.tag(m.group(1), kind, got)
            if got != value:
                t['clamped'] = True
            continue
        t = b.tag(m.group(1), kind, value)
        if kind == 'emotion':
            d = d._replace(emotion=value)
        elif kind == 'pace':
            d = d._replace(pace=value)
        elif kind == 'volume':
            d = d._replace(volume=value)
        elif kind == 'reset':
            d = PLAIN
        else:                           # sound, accent, unknown: kept for a future engine
            b.items.append({'cue': t['tag']})
    b.say(buf + text[pos:], d)
    return d


def parse(text=None, segments=None, emotion=None):
    """-> {'items': [...], 'tags': [...], 'text': spoken text}.

    items   {'say': str, 'emotion'?, 'pace'?, 'volume'?} | {'pause': ms} | {'cue': str}
    tags    one per tag, in order: {'tag', 'kind', 'at', 'value'? | 'ms'?}

    With 'text', a tag holds until the next tag of its kind or the end. With
    'segments', each segment starts from the utterance's own emotion again.
    """
    b = _Builder()
    base = PLAIN
    if emotion not in (None, ''):
        base = base._replace(emotion=emotion_name(emotion))
        b.tag(emotion, 'emotion', base.emotion)
    if segments is None:
        _inline(b, text if text is not None else '', base)
    else:
        if not isinstance(segments, list) or not segments:
            raise ApiError(400, "'segments' must be a non-empty array.")
        if len(segments) > MAX_SEGMENTS:
            raise ApiError(400, f"At most {MAX_SEGMENTS} segments.", received=len(segments))
        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                raise ApiError(400, f'segments[{i}] must be an object: {{"text": ...}} or '
                                    '{"pause": milliseconds}.')
            extra = sorted(set(seg) - {'text', 'emotion', 'pause'})
            if extra:
                raise ApiError(400, f'segments[{i}] has unknown fields.', received=extra,
                               allowed=['text', 'emotion', 'pause'])
            if 'pause' in seg:
                if 'text' in seg or 'emotion' in seg:
                    raise ApiError(400, f'segments[{i}] is a pause or text, not both.')
                try:
                    asked = int(float(seg['pause']))
                except (TypeError, ValueError):
                    raise ApiError(400, f'segments[{i}].pause must be milliseconds.',
                                   received=seg['pause'])
                got = b.pause(asked)
                t = b.tag('pause', 'pause', got)
                if got != asked:
                    t['clamped'] = True
                continue
            if 'text' not in seg:
                raise ApiError(400, f"segments[{i}] needs 'text' or 'pause'.")
            d = base
            if seg.get('emotion') not in (None, ''):
                d = d._replace(emotion=emotion_name(seg['emotion']))
                b.tag(seg['emotion'], 'emotion', d.emotion)
            _inline(b, seg['text'], d)
    spoken = ' '.join(it['say'] for it in b.items if 'say' in it)
    if not spoken:
        raise ApiError(400, 'Nothing to say: the text is empty once the [tags] are removed.')
    if len(spoken) > MAX_CHARACTERS:
        raise ApiError(400, f'Too long: one utterance is at most {MAX_CHARACTERS} characters.',
                       characters=len(spoken), maxCharacters=MAX_CHARACTERS)
    return {'items': b.items, 'tags': b.tags, 'text': spoken}


# ---- SSML passthrough -------------------------------------------------------
def is_ssml(text):
    return isinstance(text, str) and text.lstrip().lower().startswith('<speak')


def parse_ssml(text):
    """A complete <speak> document, passed to the engine as written. No tags
    are parsed and nothing is reported: the caller has taken control."""
    import xml.etree.ElementTree as ET
    doc = text.strip()
    if '<!' in doc or '<?' in doc:
        raise ApiError(400, 'SSML may not contain declarations, DOCTYPEs, comments or '
                            'processing instructions.')
    if len(doc) > MAX_SSML:
        raise ApiError(400, f'Too long: SSML is at most {MAX_SSML} characters.',
                       characters=len(doc), maxCharacters=MAX_SSML)
    probe = doc if 'xmlns:amazon' in doc else doc.replace(
        '<speak', '<speak xmlns:amazon="urn:amazon"', 1)
    try:
        root = ET.fromstring(probe)
    except ET.ParseError as e:
        raise ApiError(400, 'The SSML is not well-formed XML.', detail=str(e))
    if root.tag != 'speak':
        raise ApiError(400, 'SSML must be one <speak>...</speak> document.')
    spoken = clean(''.join(root.itertext()))
    if not spoken:
        raise ApiError(400, 'Nothing to say: the SSML holds no text.')
    if len(spoken) > MAX_CHARACTERS:
        raise ApiError(400, f'Too long: one utterance is at most {MAX_CHARACTERS} characters.',
                       characters=len(spoken), maxCharacters=MAX_CHARACTERS)
    return {'items': [{'ssml': doc}], 'tags': [], 'text': spoken}


def check_pronounce(pronounce):
    """{'word or phrase': 'how to say it'} -> validated dict (or {})."""
    if pronounce in (None, '', {}):
        return {}
    if not isinstance(pronounce, dict):
        raise ApiError(400, "'pronounce' must be an object: {\"word\": \"how to say it\"}.")
    if len(pronounce) > MAX_PRONOUNCE:
        raise ApiError(400, f"'pronounce' takes at most {MAX_PRONOUNCE} entries.")
    out = {}
    for word, alias in pronounce.items():
        w = clean(word)
        a = clean(alias) if isinstance(alias, str) else ''
        if not w or not a or len(w) > 60 or len(a) > 120:
            raise ApiError(400, "Each 'pronounce' entry maps a word or phrase (up to 60 "
                                "characters) to how it should be said (up to 120).",
                           received={str(word): alias})
        out[w] = a
    return out


# ------------------------------------------------------------- rendering ----
def prosody(item, engine):
    """A say item's direction as SSML for this engine -> (attributes, whispered)."""
    rate, pitch, volume = 100.0, 0, None
    emotion = item.get('emotion')
    if emotion and engine != 'generative':     # generative voices take emotion from the words
        rate, pitch, volume = float(EMOTIONS[emotion][0]), EMOTIONS[emotion][1], EMOTIONS[emotion][2]
    if item.get('pace'):
        rate = rate * PACE[item['pace']][0] / 100.0
    whispered = False
    if item.get('volume'):
        volume = VOLUME[item['volume']][0]
        if item['volume'] == 'whisper' and engine == 'standard':
            whispered, volume = True, None
    attrs = {}
    r = int(round(max(20.0, min(200.0, rate))))
    if r != 100:
        attrs['rate'] = '%d%%' % r
    if pitch and engine == 'standard':
        attrs['pitch'] = '%+d%%' % pitch
    if volume:
        attrs['volume'] = volume
    return attrs, whispered


def _spoken(text, pronounce):
    """Escaped text, with <sub alias> around each word that has a pronunciation."""
    if not pronounce:
        return escape(text)
    words = sorted(pronounce, key=len, reverse=True)
    rx = re.compile(r'(?<![A-Za-z0-9])(' + '|'.join(re.escape(w) for w in words) +
                    r')(?![A-Za-z0-9])', re.I)
    lower = {w.lower(): a for w, a in pronounce.items()}
    out, pos = [], 0
    for m in rx.finditer(text):
        out.append(escape(text[pos:m.start()]))
        out.append('<sub alias=%s>%s</sub>' % (quoteattr(lower[m.group(1).lower()]),
                                               escape(m.group(1))))
        pos = m.end()
    out.append(escape(text[pos:]))
    return ''.join(out)


def _block(attrs, whispered, body):
    if whispered:
        body = '<amazon:effect name="whispered">%s</amazon:effect>' % body
    if attrs:
        body = '<prosody %s>%s</prosody>' % (
            ' '.join('%s=%s' % (k, quoteattr(v)) for k, v in sorted(attrs.items())), body)
    return body


def _blocks(items, engine, pronounce):
    """[('say', attrs, whispered, ssml) | ('pause', ms)] with equal neighbours merged."""
    out = []

    def add(attrs, whispered, body):
        if out and out[-1][0] == 'say' and out[-1][1] == attrs and out[-1][2] == whispered:
            out[-1] = ('say', attrs, whispered, out[-1][3] + ' ' + body)
        else:
            out.append(('say', attrs, whispered, body))

    if engine != 'generative':
        for it in items:
            if 'pause' in it:
                out.append(('pause', it['pause']))
            elif 'say' in it:
                attrs, whispered = prosody(it, engine)
                add(attrs, whispered, _spoken(it['say'], pronounce))
        return out

    # Generative voices take <prosody> around whole sentences only. Cut the text
    # into sentences; each takes the direction in force where it begins, and a
    # pause inside a sentence stays inside it.
    sentence, sent_attrs = [], None
    last_say = max([i for i, it in enumerate(items) if 'say' in it] or [-1])

    def flush():
        nonlocal sentence, sent_attrs
        if sentence:
            add(sent_attrs or {}, False, ' '.join(sentence))
        sentence, sent_attrs = [], None

    for i, it in enumerate(items):
        if 'pause' in it:
            if sentence and i < last_say:
                sentence.append('<break time="%dms"/>' % it['pause'])
            else:
                flush()
                out.append(('pause', it['pause']))
        elif 'say' in it:
            attrs, _w = prosody(it, engine)
            text, pos = it['say'], 0
            for m in _SENTENCE_END.finditer(text):
                part = text[pos:m.end()].strip()
                pos = m.end()
                if part:
                    if not sentence:
                        sent_attrs = attrs
                    sentence.append(_spoken(part, pronounce))
                    flush()
            rest = text[pos:].strip()
            if rest:
                if not sentence:
                    sent_attrs = attrs
                sentence.append(_spoken(rest, pronounce))
    flush()
    return out


def ssml(items, engine, pronounce=None, trim=False):
    """-> (ssml, lead_ms, tail_ms).

    trim=True (raw PCM and WAV) takes pauses at the very start and end out of
    the SSML and returns them as milliseconds, so the caller can add exact
    digital silence instead of relying on how the engine renders an edge break.
    """
    if items and 'ssml' in items[0]:
        return items[0]['ssml'], 0, 0
    blocks = _blocks(items, engine, pronounce or {})
    lead = tail = 0
    if trim:
        while blocks and blocks[0][0] == 'pause':
            lead += blocks.pop(0)[1]
        while blocks and blocks[-1][0] == 'pause':
            tail += blocks.pop()[1]
    parts = []
    for bl in blocks:
        if bl[0] == 'pause':
            parts.append('<break time="%dms"/>' % bl[1])
        else:
            parts.append(_block(bl[1], bl[2], bl[3]))
    doc = '<speak>%s</speak>' % ' '.join(parts)
    if len(doc) > MAX_SSML:
        raise ApiError(400, 'Too long once the direction is added: shorten the text or split '
                            'it into two utterances.', characters=len(doc), maxCharacters=MAX_SSML)
    return doc, lead, tail


# ------------------------------------------------------------- reporting ----
def _how(attrs):
    return ', '.join('%s %s' % (k, attrs[k]) for k in ('rate', 'pitch', 'volume') if k in attrs)


def report(tags, engine):
    """What this engine did with each tag: the honest part of the response.

    rendered  native        the engine did exactly this
              approximated  the nearest thing this engine has
              inferred      left to the engine, which reads emotion from the wording
              none          not audible in this audio (the tag is still stored)
    """
    out = []
    for t in tags:
        kind = t['kind']
        r = {'tag': t['tag'], 'kind': kind, 'at': t['at']}
        if kind == 'pause':
            r['rendered'] = 'native'
            r['how'] = '%d ms of silence' % t['ms'] + (
                ' (one pause is at most %d ms)' % MAX_PAUSE_MS if t.get('clamped') else '')
        elif kind == 'reset':
            r['rendered'], r['how'] = 'native', 'back to the plain voice'
        elif kind == 'emotion':
            if engine == 'generative':
                r['rendered'] = 'inferred'
                r['how'] = ('generative voices take emotion from the wording; this engine has '
                            'no emotion control')
            else:
                attrs, _w = prosody({'emotion': t['value']}, engine)
                if attrs:
                    r['rendered'], r['how'] = 'approximated', _how(attrs)
                else:
                    r['rendered'] = 'none'
                    r['how'] = 'needs pitch, which only standard voices take'
        elif kind == 'pace':
            attrs, _w = prosody({'pace': t['value']}, engine)
            r['rendered'] = CAPABILITY[engine]['pace']
            r['how'] = _how(attrs) + ('; from the next whole sentence'
                                      if engine == 'generative' else '')
        elif kind == 'volume':
            attrs, whispered = prosody({'volume': t['value']}, engine)
            if t['value'] == 'whisper':
                r['rendered'] = CAPABILITY[engine]['whisper']
                r['how'] = ('whispered voice effect' if whispered else
                            'volume x-soft; %s voices have no whisper effect' % engine)
            else:
                r['rendered'] = CAPABILITY[engine]['volume']
                r['how'] = _how(attrs)
            if engine == 'generative':
                r['how'] += '; from the next whole sentence'
        elif kind == 'sound':
            r['rendered'] = 'none'
            r['how'] = 'no AWS voice makes this sound yet; kept in the stored script'
        elif kind == 'accent':
            r['rendered'] = 'none'
            r['how'] = 'choose the accent with voice= or language= (see voice/voices)'
        else:
            r['rendered'] = 'none'
            r['how'] = 'not a known tag (see voice/tags); kept in the stored script'
        out.append(r)
    return out


def vocabulary():
    """The tag list for voice/tags."""
    def same(value):
        return {e: value for e in ENGINES}

    out = []
    for name, (_rate, _pitch, _volume, words) in EMOTIONS.items():
        tag = {'tag': name, 'kind': 'emotion', 'at': 0, 'value': name}
        out.append({'tag': name, 'kind': 'emotion', 'also': [w for w in words if w != name],
                    'rendered': {e: report([tag], e)[0]['rendered'] for e in ENGINES}})
    for name, (_rate, words) in PACE.items():
        out.append({'tag': words[0], 'kind': 'pace', 'also': list(words[1:]),
                    'rendered': {e: CAPABILITY[e]['pace'] for e in ENGINES}})
    for name, (_vol, words) in VOLUME.items():
        cap = 'whisper' if name == 'whisper' else 'volume'
        out.append({'tag': words[0], 'kind': 'volume', 'also': list(words[1:]),
                    'rendered': {e: CAPABILITY[e][cap] for e in ENGINES}})
    for word, ms in PAUSES.items():
        out.append({'tag': word, 'kind': 'pause', 'also': [], 'ms': ms, 'rendered': same('native')})
    out.append({'tag': 'pause 800ms', 'kind': 'pause', 'also': ['pause 1.5s'],
                'rendered': same('native')})
    out.append({'tag': RESET[0], 'kind': 'reset', 'also': list(RESET[1:]),
                'rendered': same('native')})
    out.append({'tag': SOUNDS[0], 'kind': 'sound', 'also': list(SOUNDS[1:]),
                'rendered': same('none')})
    return out
