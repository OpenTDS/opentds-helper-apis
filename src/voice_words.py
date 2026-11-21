"""voice_words - what was heard, as words a test can assert on.

A transcript is never word-for-word predictable: the system under test words
its reply differently each run, and a transcriber renders the same speech as
"3 p.m." one day and "three PM" the next. So nothing here compares strings.
Every comparison is made on CANONICAL TOKENS, produced the same way from the
transcript and from what the test expects:

    "Tuesday the twenty-fourth at 3 p.m. with Doctor Nguyen."
 -> tuesday the 24 at 3 pm with dr nguyen

    lower case; punctuation and apostrophes dropped; spoken numbers and
    ordinals become digits ("twenty fourth", "24th" -> 24); digits read out
    one by one join up ("four eight three" -> 483); "$3.50" -> 3 dollars 50
    cents; "8:30 a.m." -> 8 30 am; "1.5" -> 1 point 5; a few spoken/written
    twins meet (doctor = dr, okay = ok).

Amazon Transcribe writes numbers as digits in the languages where it can
("Meet me at 8:30 a.m. on June 1st ... with $3.50 and 1.5 chocolate bars") and
as words in the others; a test may type either. Both come out the same here.

Each token remembers which heard words it came from, so a match is reported in
the words actually heard, with their timings and confidence.

Pure functions, no AWS: everything here is tested directly.
"""
import re
import unicodedata
from difflib import SequenceMatcher

UNCERTAIN_BELOW = 0.6     # a word the transcriber was less sure of than this is flagged
MAX_UNCERTAIN = 20
MAX_DIFFERENCES = 50
CONTEXT_DEFAULT = 5       # words either side of a match
CONTEXT_MAX = 50
FUZZY_DEFAULT = 0.8       # fuzzy=true

UNITS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
         'eight': 8, 'nine': 9}
TEENS = {'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
         'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19}
TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70,
        'eighty': 80, 'ninety': 90}
# 'second' is left out on purpose: it is far more often a length of time.
ORDINAL_UNITS = {'first': 1, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6, 'seventh': 7,
                 'eighth': 8, 'ninth': 9}
ORDINAL_TEENS = {'tenth': 10, 'eleventh': 11, 'twelfth': 12, 'thirteenth': 13, 'fourteenth': 14,
                 'fifteenth': 15, 'sixteenth': 16, 'seventeenth': 17, 'eighteenth': 18,
                 'nineteenth': 19}
ORDINAL_TENS = {'twentieth': 20, 'thirtieth': 30, 'fortieth': 40, 'fiftieth': 50, 'sixtieth': 60,
                'seventieth': 70, 'eightieth': 80, 'ninetieth': 90}
# Spoken and written forms of the same word.
TWINS = {'doctor': 'dr', 'mister': 'mr', 'missus': 'mrs', 'okay': 'ok', 'alright': 'allright',
         'milligrams': 'mg', 'milligram': 'mg', 'kilograms': 'kg', 'kilogram': 'kg',
         'dollar': 'dollars', 'cent': 'cents', 'pound': 'pounds', 'euro': 'euros'}
# symbol -> (the unit as spoken, its hundredth)
MONEY = {'$': ('dollars', 'cents'), '£': ('pounds', 'pence'), '€': ('euros', 'cents')}

_EDGES = '.,!?;:"\'()[]{}…'
_SPLIT = re.compile(r'[\s\-–—/]+')
_ABBREVIATION = re.compile(r'^[a-z](\.[a-z])+$')
_CURRENCY = re.compile(r'^([$£€])(\d[\d,]*)(?:\.(\d\d))?$')
_PERCENT = re.compile(r'^(\d[\d,]*(?:\.\d+)?)%$')
_DECIMAL = re.compile(r'^(\d+)\.(\d+)$')
_ORDINAL = re.compile(r'^(\d+)(?:st|nd|rd|th)$')
_GROUPED = re.compile(r'^\d{1,3}(?:,\d{3})+$')
_CLOCK = re.compile(r'^(\d{1,2}):(\d{2})$')
_SENTENCE_END = '.?!'


def clean(text):
    return ' '.join(unicodedata.normalize('NFC', str(text or '')).split())


# -------------------------------------------------------- canonical form ----
def _digits(part):
    """A written number -> atoms: 1,200 -> 1200; 1.5 -> 1 point 5 (as it is said)."""
    if _GROUPED.match(part):
        return [part.replace(',', '')]
    m = _DECIMAL.match(part)
    if m:
        return [str(int(m.group(1))), 'point', m.group(2)]
    return [part]


def atoms(shown):
    """One heard (or expected) word -> its canonical pieces, before numbers are joined."""
    s = unicodedata.normalize('NFKC', str(shown)).lower().replace('’', "'").replace('‘', "'")
    out = []
    for part in _SPLIT.split(s):
        part = part.strip(_EDGES)
        if not part:
            continue
        m = _CURRENCY.match(part)
        if m:                                       # $3.50 -> 3 dollars 50 cents
            unit, hundredth = MONEY[m.group(1)]
            out += [m.group(2).replace(',', ''), unit]
            if m.group(3) and int(m.group(3)):
                out += [str(int(m.group(3))), hundredth]
            continue
        m = _PERCENT.match(part)
        if m:
            out += _digits(m.group(1)) + ['percent']
            continue
        m = _ORDINAL.match(part)
        if m:
            out.append(m.group(1))
            continue
        if _GROUPED.match(part) or _DECIMAL.match(part):
            out += _digits(part)
            continue
        m = _CLOCK.match(part)
        if m:                                       # 3:30 -> 3 30, as "three thirty" is
            out += [str(int(m.group(1))), m.group(2)]
            continue
        if _ABBREVIATION.match(part):
            part = part.replace('.', '')            # p.m -> pm
        part = part.replace("'", '')                # i've -> ive, o'clock -> oclock
        if part:
            out.append(TWINS.get(part, part))
    return out


def _under_100(a, i):
    """A spoken number below 100 starting at a[i] -> (value, atoms used) or (None, 0)."""
    w = a[i]
    nxt = a[i + 1] if i + 1 < len(a) else None
    if w in TENS:
        if nxt in UNITS and UNITS[nxt]:
            return TENS[w] + UNITS[nxt], 2
        if nxt in ORDINAL_UNITS:
            return TENS[w] + ORDINAL_UNITS[nxt], 2
        return TENS[w], 1
    for table in (ORDINAL_TENS, TEENS, ORDINAL_TEENS, UNITS, ORDINAL_UNITS):
        if w in table:
            return table[w], 1
    return None, 0


def _number(a, i):
    """The longest spoken number starting at a[i] (up to 999,999) -> (value, atoms used)."""
    if a[i] == 'a' and i + 1 < len(a) and a[i + 1] in ('hundred', 'thousand'):
        value, used = 1, 1                          # "a hundred"
    else:
        value, used = _under_100(a, i)
    if not used:
        return None, 0
    j = i + used
    if j < len(a) and a[j] == 'hundred' and value:
        value, j = value * 100, j + 1
        k = j + 1 if j < len(a) and a[j] == 'and' else j
        if k < len(a):
            rest, more = _under_100(a, k)
            if more:
                value, j = value + rest, k + more
    if j < len(a) and a[j] == 'thousand' and 0 < value < 1000:
        value, j = value * 1000, j + 1
        k = j + 1 if j < len(a) and a[j] == 'and' else j
        if k < len(a):
            rest, more = _number(a, k)
            if more and rest < 1000:
                value, j = value + rest, k + more
    return value, j - i


def canonical(words):
    """Heard words (their display forms) -> [(token, first word index, last word index)]."""
    flat = [(atom, n) for n, shown in enumerate(words) for atom in atoms(shown)]
    only = [atom for atom, _n in flat]
    tokens, i = [], 0
    while i < len(flat):
        value, used = _number(only, i)
        if used:
            tokens.append((str(value), flat[i][1], flat[i + used - 1][1]))
            i += used
        else:
            tokens.append((flat[i][0], flat[i][1], flat[i][1]))
            i += 1
    # "three dollars and fifty cents" is "$3.50": the "and" is not in the written form.
    tokens = [t for n, t in enumerate(tokens)
              if not (t[0] == 'and' and 0 < n < len(tokens) - 2 and
                      tokens[n - 1][0] in ('dollars', 'pounds', 'euros') and
                      tokens[n + 1][0].isdigit() and tokens[n + 2][0] in ('cents', 'pence'))]
    # Digits read out one at a time are one number: "4 8 3 9" is 4839.
    out = []
    for tok in tokens:
        single = len(tok[0]) == 1 and tok[0].isdigit()
        if single and out and out[-1][3]:
            out[-1] = (out[-1][0] + tok[0], out[-1][1], tok[2], True)
        else:
            out.append((tok[0], tok[1], tok[2], single))
    return [(t, a, b) for t, a, b, _run in out]


def canonical_text(text):
    """Expected words, as typed by a test -> canonical token strings."""
    return [t for t, _a, _b in canonical(clean(text).split())]


# ------------------------------------------------- Transcribe's own JSON ----
def _ms(value):
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def digest(doc):
    """Amazon Transcribe's output document -> the facts kept about one listening.

    words carry 'shown' (the word with its punctuation) and 'end' (a sentence
    ends here) for searching; public_words() is what a response returns.
    """
    results = (doc or {}).get('results') or {}
    text = clean(((results.get('transcripts') or [{}])[0] or {}).get('transcript'))
    words = []
    for item in results.get('items') or []:
        alt = ((item.get('alternatives') or [{}])[0]) or {}
        content = str(alt.get('content') or '')
        if item.get('type') == 'punctuation':
            if words and content:
                words[-1]['shown'] += content
                if content[-1] in _SENTENCE_END:
                    words[-1]['end'] = True
            continue
        if not content.strip():
            continue
        try:
            confidence = round(float(alt.get('confidence')), 3)
        except (TypeError, ValueError):
            confidence = None
        words.append({'word': content, 'shown': content, 'startMs': _ms(item.get('start_time')),
                      'endMs': _ms(item.get('end_time')), 'confidence': confidence})
    if not words and text:
        # A transcript with no item list: still searchable, just without timings.
        words = [{'word': w.strip(_EDGES) or w, 'shown': w, 'startMs': None, 'endMs': None,
                  'confidence': None} for w in text.split()]
        for w in words:
            if w['shown'][-1] in _SENTENCE_END:
                w['end'] = True
    if not text and words:
        text = ' '.join(w['shown'] for w in words)
    out = {'text': text, 'words': words, 'speech': bool(words)}
    scores = [w['confidence'] for w in words if w['confidence'] is not None]
    if scores:
        out['confidence'] = round(sum(scores) / len(scores), 3)
        unsure = []
        for w in words:
            if w['confidence'] is not None and w['confidence'] < UNCERTAIN_BELOW and w['word'] not in unsure:
                unsure.append(w['word'])
        out['uncertain'] = unsure[:MAX_UNCERTAIN]
    timed = [w for w in words if w['startMs'] is not None and w['endMs'] is not None]
    if timed:
        out['speechStartMs'], out['speechEndMs'] = timed[0]['startMs'], timed[-1]['endMs']
        span = out['speechEndMs'] - out['speechStartMs']
        if len(timed) >= 3 and span >= 1000:
            out['wordsPerMinute'] = int(round(len(timed) * 60000.0 / span))
        gaps = [b['startMs'] - a['endMs'] for a, b in zip(timed, timed[1:])]
        out['longestPauseMs'] = max([g for g in gaps if g > 0] or [0])
    if results.get('language_code'):
        out['language'] = results['language_code']
    return out


def public_words(words):
    return [{k: w[k] for k in ('word', 'startMs', 'endMs', 'confidence') if w.get(k) is not None}
            for w in words]


# --------------------------------------------------------------- finding ----
def _phrase(words, first, last):
    """The heard words first..last as they were shown, without the punctuation
    that closes the last one (its own dots stay: "3 p.m.")."""
    return ' '.join([w['shown'] for w in words[first:last]] + [words[last]['word']])


def _similar(a, b):
    if a == b:
        return 1.0
    if a.isdigit() or b.isdigit():
        return 0.0                      # a number is right or it is wrong
    return SequenceMatcher(None, a, b).ratio()


def _result(words, first, last, context, similarity=None):
    lo, hi = max(0, first - context), min(len(words) - 1, last + context)
    r = {'word': first + 1, 'words': last - first + 1, 'matched': _phrase(words, first, last),
         'context': ' '.join(w['shown'] for w in words[lo:hi + 1])}
    if words[first].get('startMs') is not None:
        r['startMs'] = words[first]['startMs']
    if words[last].get('endMs') is not None:
        r['endMs'] = words[last]['endMs']
    scores = [w['confidence'] for w in words[first:last + 1] if w.get('confidence') is not None]
    if scores:
        r['confidence'] = min(scores)
    if similarity is not None:
        r['similarity'] = round(similarity, 3)
    return r


def _windows(tokens, wanted, threshold):
    """Yield (start token index, similarity) wherever `wanted` sits in `tokens`."""
    n, m = len(tokens), len(wanted)
    i = 0
    while m and i + m <= n:
        total = 0.0
        for k in range(m):
            s = _similar(tokens[i + k][0], wanted[k])
            if s < threshold:
                break
            total += s
        else:
            yield i, total / m
            i += m
            continue
        i += 1


def closest(words, wanted_text):
    """The stretch of heard words most like what was wanted, for a miss:
    'I asked for Nguyen; the nearest thing heard was Newin (0.55)'."""
    tokens, wanted = canonical([w['shown'] for w in words]), canonical_text(wanted_text)
    if not tokens or not wanted:
        return None
    m = min(len(wanted), len(tokens))
    best = None
    for i in range(len(tokens) - m + 1):
        score = sum(_similar(tokens[i + k][0], wanted[k]) for k in range(m)) / len(wanted)
        if best is None or score > best[0]:
            best = (score, i)
    score, i = best
    first, last = tokens[i][1], tokens[i + m - 1][2]
    out = {'heard': _phrase(words, first, last), 'similarity': round(score, 3), 'word': first + 1}
    if words[first].get('startMs') is not None:
        out['startMs'] = words[first]['startMs']
    return out


def find_text(words, text, fuzzy=1.0, context=CONTEXT_DEFAULT, limit=20):
    """Where the expected words were heard -> (count, results)."""
    tokens, wanted = canonical([w['shown'] for w in words]), canonical_text(text)
    results, count = [], 0
    for i, similarity in _windows(tokens, wanted, fuzzy):
        count += 1
        if len(results) < limit:
            results.append(_result(words, tokens[i][1], tokens[i + len(wanted) - 1][2], context,
                                   similarity if fuzzy < 1.0 else None))
    return count, results


def find_between(words, left, right=None, fuzzy=1.0, context=CONTEXT_DEFAULT, limit=20):
    """What was said after `left` (and before `right`, or to the end of the
    sentence) -> (count, results with 'value', empty captures)."""
    tokens = canonical([w['shown'] for w in words])
    lefts, rights = canonical_text(left), canonical_text(right) if right else []
    results, count, empty = [], 0, 0
    for i, similarity in _windows(tokens, lefts, fuzzy):
        start = i + len(lefts)
        end = None
        if rights:
            for j, _s in _windows(tokens[start:], rights, fuzzy):
                end = start + j
                break
            if end is None:
                continue                # no closing marker: not a match
        else:
            end = len(tokens)
            for k in range(start, len(tokens)):
                if words[tokens[k][2]].get('end'):
                    end = k + 1
                    break
            # the left marker itself may close the sentence: then nothing follows it
            if start and words[tokens[start - 1][2]].get('end'):
                end = start
        if end <= start:
            empty += 1
            continue
        count += 1
        if len(results) < limit:
            first, last = tokens[start][1], tokens[end - 1][2]
            r = _result(words, first, last, context, similarity if fuzzy < 1.0 else None)
            r['value'] = r['matched']
            r['matched'] = _phrase(words, tokens[i][1], last)
            results.append(r)
    return count, results, empty


def normalised(words):
    """The canonical transcript as one string: what find searches, and what a regex runs on."""
    return ' '.join(t for t, _a, _b in canonical([w['shown'] for w in words]))


def written(words):
    """The transcript as it was written, one space between words: what exact=true searches."""
    return ' '.join(w['shown'] for w in words)


def at(words, offset, length, context=CONTEXT_DEFAULT, exact=False):
    """Characters offset .. offset+length of the searched string (normalised(),
    or written() when exact) -> the heard words they fall in, as a result.
    This is how a regex match gets its words, timings and confidence."""
    pieces, pos = [], 0
    if exact:
        for n, w in enumerate(words):
            pieces.append((pos, pos + len(w['shown']), n, n))
            pos += len(w['shown']) + 1
    else:
        for token, first, last in canonical([w['shown'] for w in words]):
            pieces.append((pos, pos + len(token), first, last))
            pos += len(token) + 1
    if not pieces:
        return None
    end = offset + max(length, 1) - 1
    first = last = None
    for a, b, f, l in pieces:
        if first is None and offset < b:
            first = f
        if a <= end:
            last = l
    if first is None:
        first = pieces[-1][2]
    if last is None or last < first:
        last = first
    return _result(words, first, last, context)


# ------------------------------------------------------------- comparing ----
def compare(expected_text, words):
    """Expected words against heard words, on canonical tokens.

    wordErrorRate is the standard one: (substitutions + deletions + insertions)
    / expected words, from a true edit distance. similarity is 1 - that, floored
    at 0. differences groups each run of edits into one entry.
    """
    want = canonical_text(expected_text)
    tokens = canonical([w['shown'] for w in words])
    got = [t for t, _a, _b in tokens]
    n, m = len(want), len(got)
    # Edit distance with a full table: transcripts are a few hundred words at most.
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i
    for j in range(1, m + 1):
        cost[0][j] = j
    for i in range(1, n + 1):
        row, above, w = cost[i], cost[i - 1], want[i - 1]
        for j in range(1, m + 1):
            row[j] = min(above[j] + 1, row[j - 1] + 1, above[j - 1] + (w != got[j - 1]))
    # Walk back to recover which words differ.
    steps, i, j = [], n, m
    while i or j:
        if i and j and want[i - 1] == got[j - 1] and cost[i][j] == cost[i - 1][j - 1]:
            steps.append(('same', i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and j and cost[i][j] == cost[i - 1][j - 1] + 1:
            steps.append(('replace', i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + 1:
            steps.append(('missing', i - 1, None))
            i -= 1
        else:
            steps.append(('extra', None, j - 1))
            j -= 1
    steps.reverse()
    differences, run = [], None
    for op, wi, gj in steps:
        if op == 'same':
            run = None
            continue
        if run is None:
            run = {'expected': [], 'heard': [], 'at': gj}
            differences.append(run)
        if wi is not None:
            run['expected'].append(want[wi])
        if gj is not None:
            run['heard'].append(got[gj])
            if run['at'] is None:
                run['at'] = gj
    shown = []
    for d in differences[:MAX_DIFFERENCES]:
        item = {'expected': ' '.join(d['expected']), 'heard': ' '.join(d['heard'])}
        if d['at'] is not None:
            word = words[tokens[d['at']][1]]
            item['word'] = tokens[d['at']][1] + 1
            if word.get('startMs') is not None:
                item['startMs'] = word['startMs']
        shown.append(item)
    edits = cost[n][m]
    rate = edits / float(n) if n else (1.0 if m else 0.0)
    return {'same': edits == 0, 'similarity': round(max(0.0, 1.0 - rate), 3),
            'wordErrorRate': round(rate, 3), 'expectedWords': n, 'heardWords': m,
            'differences': shown, 'differencesTruncated': len(differences) > MAX_DIFFERENCES,
            'expected': ' '.join(want), 'heard': ' '.join(got)}
