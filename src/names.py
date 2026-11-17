"""names - regional personas for test data (v1).

GET /v1/names/get?region=AU[&seed=...]   or  ?notregion=AU,NZ   or  ?region=ANY
GET /v1/names/regions

Pure in-memory: the curated lists live in names_data.py, parse once at cold
start and stay in module globals, so a warm call is a few dict lookups and
random.choice calls. No AWS resources.
"""
import hashlib
import random
import unicodedata

from common import ApiError, respond, run
from names_data import REGIONS

SERVICE = 'names'


# Characters NFKD will not decompose - these need spelling out.
_FOLD = {'ß': 'ss', 'æ': 'ae', 'Æ': 'Ae', 'œ': 'oe', 'Œ': 'Oe',
         'ø': 'o', 'Ø': 'O', 'ł': 'l', 'Ł': 'L', 'đ': 'd',
         'Đ': 'D', 'þ': 'th', 'Þ': 'Th', 'ð': 'd', 'Ð': 'D',
         'ı': 'i'}


def ascii_fold(s):
    """Chloe with an acute -> Chloe, Wisniewski with diacritics -> Wisniewski.

    Returned alongside the true form so a test can drive both: plenty of
    systems quietly mangle a name the moment it leaves ASCII, and having the
    pair in one response makes that a one-line assertion.
    """
    s = ''.join(_FOLD.get(c, c) for c in s)
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))


def feminine(surname, gendered):
    """Polish adjectival surnames inflect: Kowalski -> Kowalska.

    Only the -ski / -cki / -dzki family changes; Nowak, Mazur and Kowalczyk
    are the same for everyone, so a blanket rule would be wrong.
    """
    if gendered and surname.endswith(('ski', 'cki', 'dzki')):
        return surname[:-1] + 'a'
    return surname


def _pick_two(rnd, pool):
    """A first and a middle name from the same pool, not the same name twice.

    Two independent draws collide once every len(pool) calls, which is rare but
    looks like a defect when it happens ("Kalliope Kalliope Stewart"), so the
    middle name is redrawn a few times before giving up.
    """
    first = rnd.choice(pool)
    middle = first
    for _ in range(8):
        middle = rnd.choice(pool)
        if middle != first:
            break
    return first, middle


_CACHE = {}


def _lists(code):
    """Split once per container, not per request."""
    got = _CACHE.get(code)
    if got is None:
        disp, order, gsur, f, m, l = REGIONS[code]
        got = _CACHE[code] = (disp, order, gsur, f.split('|'), m.split('|'), l.split('|'))
    return got


def do_regions(req):
    out = []
    for c in REGIONS:
        disp, order, gsur, f, m, l = _lists(c)
        out.append({'region': c, 'regionName': disp,
                    'femaleFirstNames': len(f), 'maleFirstNames': len(m), 'lastNames': len(l),
                    'familyNameFirst': order == 'F',
                    'genderedSurnames': gsur == 'Y',
                    'combinations': len(f) * len(m) * len(l)})
    return respond(200, {'count': len(out),
                         'regions': out,
                         'comment': 'use names/get?region=CODE, region=ANY, or '
                                    'notregion=CODE,CODE'})


def do_get(req):
    code = (req.get('region') or '').strip().upper()
    notregion = (req.get('notregion') or '').strip().upper()
    known = sorted(REGIONS)

    if code and notregion:
        raise ApiError(400, "Pass either 'region' or 'notregion', not both.",
                       region=code, notregion=notregion)
    if not code and not notregion:
        raise ApiError(400, "Missing query parameter: pass 'region', or 'notregion' "
                            "to get anywhere else, or region=ANY for no preference.",
                       allowed=known + ['ANY'], hint='see names/regions')

    # A seed makes the draw reproducible, so a failing iteration can be re-run
    # with the same persona instead of a different one. Created before the
    # region is chosen so that with notregion the CHOICE OF REGION is seeded
    # too - otherwise the same seed would wander between regions.
    # The raw (unstripped) value is used so a seed gives exactly the same
    # persona as it did before v1.
    seed = req.get('seed')
    # Hashed rather than fed to Random() raw: small integer seeds land on
    # degenerate early streams, and seed=1 is the first thing anyone tries.
    rnd = (random.Random(hashlib.sha256(str(seed).encode('utf-8')).hexdigest())
           if seed not in (None, '') else random.Random())

    excluded = []
    if notregion:
        # Comma-separated, so 'not anywhere English-speaking' is one call.
        excluded = [x.strip() for x in notregion.split(',') if x.strip()]
        unknown = [x for x in excluded if x not in REGIONS]
        if unknown:
            # Deliberately an error rather than a silent no-op: notregion=AUS
            # would otherwise exclude nothing and quietly hand back Australians.
            raise ApiError(404, 'Unknown region in notregion.', unknown=unknown,
                           allowed=known)
        pool = sorted(c for c in REGIONS if c not in excluded)
        if not pool:
            raise ApiError(400, 'notregion excludes every region there is.',
                           excluded=sorted(set(excluded)), allowed=known)
        code = rnd.choice(pool)
    elif code == 'ANY':
        code = rnd.choice(known)
    elif code not in REGIONS:
        raise ApiError(404, 'Unknown region.', region=code, allowed=known + ['ANY'])

    disp, order, gsur, fem, mal, lst = _lists(code)
    gendered = gsur == 'Y'
    family_first = order == 'F'

    ff, fm = _pick_two(rnd, fem)
    mf, mm = _pick_two(rnd, mal)
    base = rnd.choice(lst)
    fl, ml = feminine(base, gendered), base

    def full(first, middle, last):
        # Japan and China write the family name first and do not use a middle
        # name, so one is still returned in its own field but left out here.
        if family_first:
            return last + ' ' + first
        return first + ' ' + middle + ' ' + last

    core = {'femaleFirstName': ff, 'femaleMiddleName': fm, 'femaleLastName': fl,
            'femaleFullName': full(ff, fm, fl),
            'maleFirstName': mf, 'maleMiddleName': mm, 'maleLastName': ml,
            'maleFullName': full(mf, mm, ml)}
    folded = {k: ascii_fold(v) for k, v in core.items()}

    body = {'region': code, 'regionName': disp,
            'familyNameFirst': family_first,
            'genderedSurname': gendered and fl != ml}
    if excluded:
        # Echo what was excluded and how many were left, so a caller can assert
        # on it and a log makes sense months later.
        body['excludedRegions'] = sorted(set(excluded))
        body['chosenFrom'] = len(REGIONS) - len(set(excluded))
    body.update(core)
    body['hasDiacritics'] = folded != core
    body['ascii'] = folded
    body['seed'] = seed if seed not in (None, '') else None
    if family_first:
        body['comment'] = ('family name is written first here; the middle name is supplied '
                           'for systems that require one but is not part of the full name')
    return respond(200, body)


ROUTES = {'get': do_get, 'regions': do_regions}
INDEX = {'description': 'Regional personas: a female and a male name sharing a surname, '
                        'with ASCII-folded forms. Repeatable with seed.'}


def handler(event, context):
    return run(event, SERVICE, ROUTES, INDEX)
