"""recon - reconcile what a test submitted with what was confirmed processed (v1).

GET  /v1/recon/expect?key=run42&value=609829094[&suffix=4][&ignorecase=true][&<field>=...][&perfkey=]
POST /v1/recon/expect?key=run42[&suffix=4]   body: JSON array, CSV with a 'value' column, or one value per line
GET  /v1/recon/confirm?key=run42&value=xxxxx9094[&<field>=...][&perfkey=run42.lag]
POST /v1/recon/confirm?key=run42
GET  /v1/recon/get?key=run42[&value=][&wait=][&strict=][&grace=][&sla=][&fields=]
GET  /v1/recon/list?key=run42[&status=][&start=&limit=][&format=json|csv][&fields=]
GET  /v1/recon/cyclepop?key=run42
GET  /v1/recon/clear?key=run42

COUNT MATCHING. Every expect and every confirm is one record, and records are
matched by COUNT per match key (multiset semantics): a key expected 3 times needs
3 confirmations. Nothing is ever deduplicated. The match key is the value itself,
or - with suffix=N, fixed by the key's first write - its last N characters, so the
expected side can send full values (609829094) and the confirmed side masked ones
(xxxxx9094). The values given are kept and reported as candidates.

A group (one match key) is in one state:
  matched            expected 1, confirmed 1, and the fields given on both sides agree
  mismatched         expected 1, confirmed 1, and a field given on both sides differs
  reconciledByCount  expected = confirmed > 1: they balance, but which confirmation
                     belongs to which record cannot be told
  short              confirmed < expected (shortBy = expected - confirmed)
  over               confirmed > expected (overBy = confirmed - expected)
Fields can only be paired when a key is unique on both sides, so they are compared
only in matched/mismatched groups; elsewhere they are shown, not compared.

Table (env RECON_TABLE): PK (S) / SK (S), TTL 'ttl', GSI ByState (gk S / gt N).
  PK "<key>#G<shard>"  SK "<match key>"  one group: counts and candidates
  PK "<key>#S<shard>"  SK "S"            summary counters (additive; summed on read)
  PK "<key>#M"         SK "M"            key settings: ignorecase, suffix, expiry, schema
Group attributes: e/c expected/confirmed count, ev/cv sets of the values given,
ef/cf first fields per side, et/ct first expected/confirmed time (ms), st state,
gk/gt the ByState index keys ("<key>#<state>#<shard>", and the ms the group entered
that state - for short, when cyclepop last handed it out), k cyclepop count.

ATOMICITY. Each record is one UpdateItem that ADDs to its group's count - no
read-modify-write anywhere. ALL_NEW returns the counts after this record, so the
counts before it are known exactly too, and the summary counters are moved by
exactly the difference this record made. The group's state (for the index) is set
by a follow-up conditional on the counts still being what this record left; if
another record got in between, that record's follow-up sets the state.
Records are spread over SHARDS partitions by a hash of the match key, and summary
updates over SHARDS partitions at random, so a load test cannot push one partition
past DynamoDB's ~1,000 writes/second.
"""
import csv
import io
import json
import os
import random
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

import boto3
from boto3.dynamodb.conditions import Key as K
from botocore.exceptions import ClientError

from common import ApiError, raw, respond, run

SERVICE = 'recon'

SCHEMA = 2                     # 1 = the first (value-per-record) layout; keys from it are refused
SHARDS = 16
RETENTION_DAYS = 30            # a key lives this long from its first write
MAX_VALUE_BYTES = 1000         # DynamoDB sort keys stop at 1024 bytes
MAX_SUFFIX = 64
MAX_CANDIDATES = 100           # values kept per side per group (counts are never capped)
MAX_FIELDS = 50
MAX_FIELD_NAME = 64
MAX_FIELD_CHARS = 2000
MAX_BATCH = 5000               # values in one POST
WRITERS = 16                   # parallel writers for a POST batch
MAX_SCAN = 50000               # groups read for fields= (an exact recount)
MAX_LIMIT = 1000
MAX_WINDOW = 10000             # start + limit on recon/list
MAX_CLEAR = 10000
MAX_WAIT = 20
POLL_S = 1.0

STATES = ('matched', 'mismatched', 'reconciledByCount', 'short', 'over')
UNRECONCILED = ('short', 'over', 'mismatched')
LIST_STATUSES = STATES + ('unreconciled', 'all')
RENAMED_STATUS = {'outstanding': 'short', 'unexpected': 'over'}
GROUP_COUNTER = {'matched': 'gm', 'mismatched': 'gx', 'reconciledByCount': 'gr', 'short': 'gs', 'over': 'go'}

# Parameters of the write actions; every other query parameter is a record field.
WRITE_PARAMS = {'key', 'value', 'value64', 'ignorecase', 'suffix', 'perfkey', 'format'}
# Parameters of the other actions: never fields, so a URL edited from recon/get
# fails loudly instead of storing wait=20 as a field.
OTHER_PARAMS = {'wait', 'strict', 'grace', 'sla', 'fields', 'status', 'start', 'limit'}

_local = threading.local()


def _res():
    r = getattr(_local, 'res', None)
    if r is None:
        # boto3 resources are not thread-safe: one per thread (batches and reads run in parallel).
        r = boto3.resource('dynamodb')
        _local.res = r
    return r


def _table():
    t = getattr(_local, 'tbl', None)
    if t is None:
        t = _res().Table(os.environ['RECON_TABLE'])
        _local.tbl = t
    return t


def _reset():
    """Tests: forget the per-thread handles (a new moto mock each test)."""
    global _local
    _local = threading.local()


def now_ms():
    return int(time.time() * 1000)


def _cond_failed(exc):
    return exc.response['Error']['Code'] == 'ConditionalCheckFailedException'


# ------------------------------------------------------------------ keys and values
def gpk(key, shard):
    return '%s#G%d' % (key, shard)


def spk(key, shard):
    return '%s#S%d' % (key, shard)


def mpk(key):
    return '%s#M' % key


def gk(key, state, shard):
    return '%s#%s#%d' % (key, state, shard)


def shard_of(match):
    return zlib.crc32(match.encode('utf-8')) % SHARDS


def match_key(value, cfg):
    """The key a value is matched on: trimmed, lower-cased with ignorecase, and its
    last `suffix` characters when the key has a suffix."""
    v = value.strip()
    if cfg['ignorecase']:
        v = v.lower()
    n = cfg['suffix']
    return v[-n:] if n and len(v) > n else v


def check_value(value, where=None):
    """The value as given (trimmed); 400 when it cannot be stored."""
    at = ' (%s)' % where if where else ''
    v = (value or '').strip()
    if not v:
        raise ApiError(400, 'value is empty%s.' % at,
                       hint='value is the thing being reconciled, e.g. value=jane.doe@example.com')
    if len(v.encode('utf-8')) > MAX_VALUE_BYTES:
        raise ApiError(400, 'value is longer than %d bytes%s.' % (MAX_VALUE_BYTES, at))
    return v


def field_text(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(',', ':'), sort_keys=True)
    return str(v).strip()


def clean_fields(pairs, where=None):
    """{name: text} with lower-case names; an empty value means 'not given'."""
    at = ' (%s)' % where if where else ''
    out = {}
    for name, v in pairs:
        n = str(name).strip().lower()
        if not n:
            continue
        if n in WRITE_PARAMS or n in OTHER_PARAMS:
            raise ApiError(400, "'%s' cannot be a field name%s." % (n, at),
                           reserved=sorted(WRITE_PARAMS | OTHER_PARAMS))
        if len(n) > MAX_FIELD_NAME:
            raise ApiError(400, 'field name longer than %d characters%s.' % (MAX_FIELD_NAME, at),
                           received=n)
        t = field_text(v)
        if t == '':
            continue
        if len(t) > MAX_FIELD_CHARS:
            raise ApiError(400, "field '%s' is longer than %d characters%s." % (n, MAX_FIELD_CHARS, at))
        out[n] = t
    if len(out) > MAX_FIELDS:
        raise ApiError(400, 'more than %d fields on one record%s.' % (MAX_FIELDS, at))
    return out


# ------------------------------------------------------------------ comparison and state
def same(a, b):
    """Equal as text, or equal as numbers (120.00 and 120 are the same amount)."""
    if a == b:
        return True
    try:
        da, db = Decimal(a), Decimal(b)
        return da.is_finite() and db.is_finite() and da == db
    except (InvalidOperation, TypeError, ValueError):
        return False


def differences(ef, cf, fields=None):
    """[{field, expected, confirmed}]. By default only fields given on both sides
    are compared; with fields= every named field must be on both sides and agree."""
    ef, cf = ef or {}, cf or {}
    names = sorted(set(ef) & set(cf)) if fields is None else fields
    out = []
    for n in names:
        a, b = ef.get(n), cf.get(n)
        if a is None or b is None or not same(a, b):
            out.append({'field': n, 'expected': a, 'confirmed': b})
    return out


def counts(it):
    return int((it or {}).get('e', 0)), int((it or {}).get('c', 0))


def state_for(e, c, ef=None, cf=None, fields=None):
    if not e and not c:
        return None
    if c < e:
        return 'short'
    if c > e:
        return 'over'
    if e == 1:
        return 'mismatched' if differences(ef, cf, fields) else 'matched'
    return 'reconciledByCount'


def state_of(it, fields=None):
    if not it:
        return None
    e, c = counts(it)
    return state_for(e, c, it.get('ef'), it.get('cf'), fields)


def ambiguous(e, c):
    return 1 if max(e, c) > 1 else 0


def tally(e, c, st):
    """What one group contributes to the summary counters."""
    out = {'m': min(e, c), 'o': max(0, e - c), 'u': max(0, c - e),
           'x': 1 if st == 'mismatched' else 0, 'a': ambiguous(e, c)}
    if st:
        out[GROUP_COUNTER[st]] = 1
    return out


def sorted_values(s):
    return sorted(str(v) for v in (s or ()))


def out_group(it, fields=None):
    e, c = counts(it)
    st = state_of(it, fields)
    g = {'value': it['SK'], 'status': st, 'expected': e, 'confirmed': c}
    if st == 'short':
        g['shortBy'] = e - c
    if st == 'over':
        g['overBy'] = c - e
    ev, cv = sorted_values(it.get('ev')), sorted_values(it.get('cv'))
    g['candidates'] = ev
    if cv and not set(cv) <= set(ev):
        g['confirmedValues'] = cv
    if 'et' in it:
        g['expectedTime'] = int(it['et']) // 1000
    if 'ct' in it:
        g['confirmedTime'] = int(it['ct']) // 1000
    if 'et' in it and 'ct' in it:
        g['latencyMs'] = int(it['ct']) - int(it['et'])
    if it.get('ef'):
        g['expectedFields'] = dict(it['ef'])
    if it.get('cf'):
        g['confirmedFields'] = dict(it['cf'])
    if st == 'mismatched':
        g['differences'] = differences(it.get('ef'), it.get('cf'), fields)
    if it.get('k'):
        g['checks'] = int(it['k'])
    return g


# ------------------------------------------------------------------ key settings
def settings(key, want_ic=None, want_suffix=None, create=False):
    """{'ignorecase', 'suffix', 'expires', 'created'} for the key, or None if nothing
    was ever written. The first write fixes ignorecase, suffix and the expiry."""
    t = _table()
    it = t.get_item(Key={'PK': mpk(key), 'SK': 'M'}, ConsistentRead=True).get('Item')
    if it is None:
        if not create:
            return None
        now = int(time.time())
        it = {'PK': mpk(key), 'SK': 'M', 'ic': bool(want_ic), 'sfx': int(want_suffix or 0),
              'schema': SCHEMA, 'created': now, 'exp': now + RETENTION_DAYS * 86400}
        it['ttl'] = it['exp']
        try:
            t.put_item(Item=it, ConditionExpression='attribute_not_exists(PK)')
        except ClientError as exc:
            if not _cond_failed(exc):
                raise
            it = t.get_item(Key={'PK': mpk(key), 'SK': 'M'}, ConsistentRead=True)['Item']
    if int(it.get('schema', 1)) != SCHEMA:
        raise ApiError(400, "key '%s' was written by an earlier version of recon, which matched "
                            "values one record each." % key,
                       hint='use a new key (lists/codes gives you a random one), or recon/clear this one')
    cfg = {'ignorecase': bool(it.get('ic')), 'suffix': int(it.get('sfx', 0)),
           'expires': int(it.get('exp', 0)), 'created': int(it.get('created', 0))}
    if want_ic is not None and bool(want_ic) != cfg['ignorecase']:
        raise ApiError(400, "key '%s' already matches values with ignorecase=%s." %
                       (key, 'true' if cfg['ignorecase'] else 'false'),
                       hint='the first write to a key fixes ignorecase; recon/clear the key '
                            'or use a new key to change it')
    if want_suffix is not None and int(want_suffix) != cfg['suffix']:
        raise ApiError(400, "key '%s' already matches on %s." %
                       (key, 'the last %d characters' % cfg['suffix'] if cfg['suffix'] else 'whole values'),
                       hint='the first write to a key fixes suffix; recon/clear the key or use a new key')
    if create and cfg['expires'] and cfg['expires'] < time.time():
        raise ApiError(400, "key '%s' expired %d days after its first write." % (key, RETENTION_DAYS),
                       hint='use a new key for this run (lists/codes gives you a random one)')
    return cfg


# ------------------------------------------------------------------ writes
def _update_group(key, match, side, value, fields, ms, ttl, with_values=True):
    shard = shard_of(match)
    mine, other = ('e', 'c') if side == 'expect' else ('c', 'e')
    tkey, fkey, vkey = ('et', 'ef', 'ev') if side == 'expect' else ('ct', 'cf', 'cv')
    names = {'#mine': mine, '#other': other, '#t': tkey, '#ttl': 'ttl'}
    vals = {':one': 1, ':zero': 0, ':ms': ms, ':ttl': ttl}
    sets = ['#t = if_not_exists(#t, :ms)', '#ttl = :ttl']
    adds = ['#mine :one', '#other :zero']
    if fields:
        names['#f'] = fkey
        vals[':f'] = fields
        sets.append('#f = if_not_exists(#f, :f)')
    kw = {}
    if with_values:
        # Keep at most MAX_CANDIDATES values per side; the count is never capped.
        names['#v'] = vkey
        vals[':v'] = {value}
        vals[':cap'] = MAX_CANDIDATES
        adds.append('#v :v')
        kw['ConditionExpression'] = 'attribute_not_exists(#v) OR size(#v) < :cap OR contains(#v, :val)'
        vals[':val'] = value
    return _table().update_item(
        Key={'PK': gpk(key, shard), 'SK': match},
        UpdateExpression='SET ' + ', '.join(sets) + ' ADD ' + ', '.join(adds),
        ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
        ReturnValues='ALL_NEW', **kw)['Attributes']


def write_one(key, cfg, side, value, fields, ms):
    """Add one record to its group. One atomic UpdateItem; no read-modify-write."""
    match = match_key(value, cfg)
    shard = shard_of(match)
    try:
        new = _update_group(key, match, side, value, fields, ms, cfg['expires'])
    except ClientError as exc:
        # The candidate list is full (or the item would grow too large): count the
        # record without adding its value. Nothing was written by the failed call.
        if not _cond_failed(exc) and exc.response['Error']['Code'] != 'ValidationException':
            raise
        new = _update_group(key, match, side, value, fields, ms, cfg['expires'], with_values=False)
    e1, c1 = counts(new)
    e0, c0 = (e1 - 1, c1) if side == 'expect' else (e1, c1 - 1)
    # The fields as they were before this record: only the first record of a side sets them.
    ef1, cf1 = new.get('ef'), new.get('cf')
    ef0 = ef1 if (side != 'expect' or e0) else None
    cf0 = cf1 if (side != 'confirm' or c0) else None
    st0, st1 = state_for(e0, c0, ef0, cf0), state_for(e1, c1, ef1, cf1)

    # The summary moves by exactly what this record changed.
    delta = {'e' if side == 'expect' else 'c': 1}
    before, after = tally(e0, c0, st0), tally(e1, c1, st1)
    for k in set(before) | set(after):
        d = after.get(k, 0) - before.get(k, 0)
        if d:
            delta[k] = delta.get(k, 0) + d

    # Point the state index at the new state, unless another record has moved the
    # counts since (its own follow-up then sets the state for its counts).
    if new.get('st') != st1:
        try:
            _table().update_item(
                Key={'PK': gpk(key, shard), 'SK': match},
                UpdateExpression='SET #st = :st, #gk = :gk, #gt = :ms',
                ConditionExpression='#e = :e AND #c = :c',
                ExpressionAttributeNames={'#st': 'st', '#gk': 'gk', '#gt': 'gt', '#e': 'e', '#c': 'c'},
                ExpressionAttributeValues={':st': st1, ':gk': gk(key, st1, shard), ':ms': ms,
                                           ':e': e1, ':c': c1})
        except ClientError as exc:
            if not _cond_failed(exc):
                raise

    # Processing lag: when this record pairs an expectation with a confirmation.
    lat = None
    if min(e1, c1) > min(e0, c0) and 'et' in new and 'ct' in new:
        lat = (ms - int(new['et'])) if side == 'confirm' else (int(new['ct']) - ms)
    result = {'value': value, 'match': match, 'status': st1, 'previousStatus': st0,
              'expected': e1, 'confirmed': c1}
    if lat is not None:
        result['latencyMs'] = lat
    if st1 == 'mismatched':
        result['differences'] = differences(ef1, cf1)
    return result, delta, lat


def _bound(pkv, attr, v, op):
    try:
        _table().update_item(
            Key={'PK': pkv, 'SK': 'S'}, UpdateExpression='SET #a = :v',
            ConditionExpression='attribute_not_exists(#a) OR #a %s :v' % op,
            ExpressionAttributeNames={'#a': attr}, ExpressionAttributeValues={':v': v})
    except ClientError as exc:
        if not _cond_failed(exc):
            raise


def apply_counters(key, cfg, delta, lats):
    """One additive update to a random summary shard for a whole call or batch."""
    delta = {k: v for k, v in delta.items() if v}
    if lats:
        delta['ln'] = len(lats)
        delta['ls'] = sum(lats)
    if not delta:
        return
    pkv = spk(key, random.randrange(SHARDS))
    names = {'#%s' % k: k for k in delta}
    vals = {':%s' % k: v for k, v in delta.items()}
    names['#ttl'] = 'ttl'
    vals[':ttl'] = cfg['expires']
    _table().update_item(
        Key={'PK': pkv, 'SK': 'S'},
        UpdateExpression='SET #ttl = :ttl ADD ' + ', '.join('#%s :%s' % (k, k) for k in delta),
        ExpressionAttributeNames=names, ExpressionAttributeValues=vals)
    if lats:
        _bound(pkv, 'lmn', min(lats), '>')
        _bound(pkv, 'lmx', max(lats), '<')


def feed_perf(perfkey, lats):
    """Processing lag (ms; a confirmation that beat its expectation counts as 0)
    into a perf key, in-process, so perf/stats, series and assert work on it."""
    if not perfkey or not lats:
        return
    import perf
    perf.record(perfkey, [Decimal(max(0, int(v))) for v in lats])


def perfkey_param(req):
    p = req.key('perfkey', required=False)
    if p and not os.environ.get('PERF_TABLE'):
        raise ApiError(400, 'perfkey is not available on this deployment (no perf table).')
    return p


def suffix_param(req):
    if 'suffix' not in req.qs:
        return None
    n = req.integer('suffix', None, clamp=False)
    if n is None or n < 0 or n > MAX_SUFFIX:
        raise ApiError(400, "'suffix' must be a whole number from 0 to %d." % MAX_SUFFIX,
                       received=req.get('suffix'),
                       hint='suffix=4 matches on the last 4 characters; 0 (the default) on whole values')
    return n


def parse_batch(req):
    """([(item, value, field pairs)], format) from a POST body: JSON, CSV or one value per line."""
    data = req.body_bytes()
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('cp1252', errors='replace')
    fmt = (req.text('format') or '').lower()
    stripped = text.lstrip()
    if not fmt:
        # JSON by its first character; CSV when the content type says so or the first
        # line has a delimiter (then a header with a 'value' column is required, rather
        # than silently treating "name,email" rows as values); otherwise one per line.
        ctype = (req.headers.get('content-type') or '').lower()
        first = stripped.split('\n', 1)[0]
        if stripped[:1] in ('[', '{'):
            fmt = 'json'
        elif 'csv' in ctype or any(d in first for d in ',;\t|'):
            fmt = 'csv'
        else:
            fmt = 'text'
    if fmt not in ('json', 'csv', 'text'):
        raise ApiError(400, "'format' must be json, csv or text.", received=fmt,
                       allowed=['json', 'csv', 'text'])
    rows = []
    if fmt == 'json':
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise ApiError(400, 'The body is not valid JSON: %s' % exc)
        if isinstance(doc, dict):
            doc = doc.get('values', doc.get('records'))
        if not isinstance(doc, list):
            raise ApiError(400, 'A JSON body must be an array of values, an array of objects with '
                                'a "value", or {"values": [...]}.')
        for i, item in enumerate(doc, 1):
            if isinstance(item, dict):
                low = {str(k).strip().lower(): v for k, v in item.items()}
                v = low.pop('value', None)
                rows.append((i, field_text(v), list(low.items())))
            else:
                rows.append((i, field_text(item), []))
    elif fmt == 'csv':
        first = text.lstrip().split('\n', 1)[0]
        delim = max(',;\t|', key=first.count) if any(d in first for d in ',;\t|') else ','
        header, vi = None, 0
        for n, cells in enumerate(csv.reader(io.StringIO(text), delimiter=delim), 1):
            if not any(c.strip() for c in cells):
                continue
            if header is None:
                header = [c.strip().lower() for c in cells]
                if 'value' not in header:
                    raise ApiError(400, "A CSV body needs a header row with a column named 'value'.",
                                   received=header[:20])
                vi = header.index('value')
                continue
            v = cells[vi] if vi < len(cells) else ''
            pairs = [(h, cells[j]) for j, h in enumerate(header) if j != vi and j < len(cells) and h]
            rows.append((n, v, pairs))
    else:
        for n, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s and not s.startswith('#'):
                rows.append((n, s, []))
    if not rows:
        raise ApiError(400, 'The body holds no values.',
                       hint='send a JSON array, a CSV with a value column, or one value per line')
    if len(rows) > MAX_BATCH:
        raise ApiError(400, 'Too many values in one call.', maximum=MAX_BATCH, received=len(rows))
    return rows, fmt


def do_write(req, side):
    key = req.key()
    ic_param = req.boolean('ignorecase', None) if 'ignorecase' in req.qs else None
    sfx_param = suffix_param(req)
    perfkey = perfkey_param(req)
    has_body = req.method == 'POST' and bool(req.body)
    value_q, _ = req.text_or64('value')
    for name in req.qs:
        if name in OTHER_PARAMS:
            raise ApiError(400, "'%s' is not a parameter of recon/%s and cannot be a field name."
                           % (name, side), reserved=sorted(WRITE_PARAMS | OTHER_PARAMS))
    if has_body and value_q is not None:
        raise ApiError(400, 'Send values as the request body OR one value= parameter, not both.')
    ms = now_ms()

    if not has_body:
        if value_q is None:
            raise ApiError(400, "Missing required query parameter 'value' (or 'value64').",
                           hint='or POST several values as the body')
        value = check_value(value_q)
        fields = clean_fields((n, v) for n, v in req.qs.items() if n not in WRITE_PARAMS)
        cfg = settings(key, ic_param, sfx_param, create=True)
        result, delta, lat = write_one(key, cfg, side, value, fields, ms)
        lats = [lat] if lat is not None else []
        apply_counters(key, cfg, delta, lats)
        feed_perf(perfkey, lats)
        body = {'key': key, 'ignorecase': cfg['ignorecase'], 'suffix': cfg['suffix']}
        body.update(result)
        return respond(200, body)

    extra = sorted(n for n in req.qs if n not in WRITE_PARAMS)
    if extra:
        raise ApiError(400, 'With a POST body, fields go in the body, not the query string.',
                       received=extra)
    rows, fmt = parse_batch(req)
    jobs, warnings = [], []
    for item, v, pairs in rows:
        try:
            jobs.append((check_value(v, 'item %d' % item), clean_fields(pairs, 'item %d' % item)))
        except ApiError as e:
            warnings.append({'item': item, 'error': e.message})
    if not jobs:
        raise ApiError(400, 'None of the values in the body could be used.', warnings=warnings[:20])
    cfg = settings(key, ic_param, sfx_param, create=True)

    def one(job):
        return write_one(key, cfg, side, job[0], job[1], ms)

    with ThreadPoolExecutor(max_workers=min(WRITERS, len(jobs))) as pool:
        results = list(pool.map(one, jobs))
    delta, lats = {}, []
    for _, d, lat in results:
        for k, v in d.items():
            delta[k] = delta.get(k, 0) + v
        if lat is not None:
            lats.append(lat)
    apply_counters(key, cfg, delta, lats)
    feed_perf(perfkey, lats)
    body = {'key': key, 'ignorecase': cfg['ignorecase'], 'suffix': cfg['suffix'], 'format': fmt,
            'received': len(rows), 'recorded': len(results), 'skipped': len(warnings),
            'groups': len({r['match'] for r, _, _ in results}), 'paired': len(lats)}
    if warnings:
        body['warnings'] = warnings[:20]
    return respond(200, body)


def do_expect(req):
    return do_write(req, 'expect')


def do_confirm(req):
    return do_write(req, 'confirm')


# ------------------------------------------------------------------ reads
def _parallel(fn, args):
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(args)))) as pool:
        return list(pool.map(fn, args))


COUNTERS = ('e', 'c', 'm', 'o', 'u', 'x', 'a', 'gm', 'gx', 'gr', 'gs', 'go', 'ln', 'ls')


def read_counters(key):
    """The summary shards summed: one BatchGetItem, however many records the key holds."""
    name = _table().name
    pending = {name: {'Keys': [{'PK': spk(key, i), 'SK': 'S'} for i in range(SHARDS)],
                      'ConsistentRead': True}}
    items = []
    for _ in range(6):
        resp = _res().batch_get_item(RequestItems=pending)
        items.extend(resp['Responses'].get(name, []))
        pending = resp.get('UnprocessedKeys') or {}
        if not pending:
            break
    c = {k: 0 for k in COUNTERS}
    mn = mx = None
    for it in items:
        for k in c:
            c[k] += int(it.get(k, 0))
        if it.get('lmn') is not None:
            mn = int(it['lmn']) if mn is None else min(mn, int(it['lmn']))
        if it.get('lmx') is not None:
            mx = int(it['lmx']) if mx is None else max(mx, int(it['lmx']))
    c['lmn'], c['lmx'] = mn, mx
    return c


def query_state(key, state, gt_from=None, gt_to=None, limit=None):
    """Groups in one state (optionally entered since gt_from / before gt_to ms),
    oldest first, from every shard of the ByState index."""
    def one(shard):
        cond = K('gk').eq(gk(key, state, shard))
        if gt_from is not None:
            cond = cond & K('gt').gte(gt_from)
        if gt_to is not None:
            cond = cond & K('gt').lte(gt_to)
        kw = {'IndexName': 'ByState', 'KeyConditionExpression': cond, 'ScanIndexForward': True}
        if limit:
            kw['Limit'] = limit
        got = []
        while True:
            resp = _table().query(**kw)
            got.extend(it for it in resp.get('Items', []) if it.get('st') == state)
            lek = resp.get('LastEvaluatedKey')
            if not lek or (limit and len(got) >= limit):
                return got[:limit] if limit else got
            kw['ExclusiveStartKey'] = lek
    return [it for p in _parallel(one, list(range(SHARDS))) for it in p]


def scan_groups(key, cap=MAX_SCAN):
    """Every group under the key (up to cap), read from the groups themselves."""
    per = cap // SHARDS + 1

    def one(shard):
        kw = {'KeyConditionExpression': K('PK').eq(gpk(key, shard)), 'ConsistentRead': True}
        got = []
        while True:
            resp = _table().query(**kw)
            got.extend(resp.get('Items', []))
            lek = resp.get('LastEvaluatedKey')
            if not lek:
                return got, False
            if len(got) >= per:
                return got, True
            kw['ExclusiveStartKey'] = lek
    parts = _parallel(one, list(range(SHARDS)))
    items = [it for p, _ in parts for it in p]
    return items[:cap], any(m for _, m in parts) or len(items) > cap


def fields_param(req):
    raw_f = req.text('fields')
    if raw_f is None:
        return None
    names = []
    for part in raw_f.split(','):
        n = part.strip().lower()
        if n and n not in names:
            names.append(n)
    if not names:
        raise ApiError(400, "'fields' must name one or more fields, comma-separated.", received=raw_f)
    return names


def pct(sorted_vals, p):
    """Nearest-rank percentile of an ascending list."""
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    rank = max(1, min(n, int(-(-p * n // 100))))
    return sorted_vals[rank - 1]


def _summary(c):
    out = {'expected': c['e'], 'confirmed': c['c'], 'matched': c['m'],
           'outstanding': c['o'], 'unexpected': c['u'], 'mismatched': c['x'],
           'ambiguousGroups': c['a'],
           'groups': {s: c[GROUP_COUNTER[s]] for s in STATES}}
    lat = {'count': c['ln']}
    if c['ln']:
        lat.update({'mean': round(c['ls'] / float(c['ln']), 1), 'min': c['lmn'], 'max': c['lmx']})
    out['latencyMs'] = lat
    return out


def summary_from_scan(items, fields):
    c = {k: 0 for k in COUNTERS}
    lats = []
    for it in items:
        e, cc = counts(it)
        st = state_of(it, fields)
        c['e'] += e
        c['c'] += cc
        for k, v in tally(e, cc, st).items():
            c[k] += v
        if 'et' in it and 'ct' in it and min(e, cc):
            lats.append(int(it['ct']) - int(it['et']))
    lats.sort()
    c['ln'], c['ls'] = len(lats), sum(lats)
    c['lmn'], c['lmx'] = (lats[0], lats[-1]) if lats else (None, None)
    out = _summary(c)
    if lats:
        out['latencyMs'].update({'p50': pct(lats, 50), 'p90': pct(lats, 90), 'p95': pct(lats, 95),
                                 'p99': pct(lats, 99)})
        out['latencyMs']['comment'] = 'first confirmation minus first expectation, per group'
    return out


def get_group(key, cfg, value):
    match = match_key(value, cfg)
    return match, _table().get_item(Key={'PK': gpk(key, shard_of(match)), 'SK': match},
                                    ConsistentRead=True).get('Item')


def _get_one(key, value, wait, strict, fields, started):
    deadline = started + wait
    cfg = settings(key)
    while True:
        match, it = get_group(key, cfg, value) if cfg else (value, None)
        st = state_of(it, fields)
        if (st and st != 'short') or time.time() + POLL_S > deadline:
            break
        time.sleep(POLL_S)
        if cfg is None:
            cfg = settings(key)
    waited = int((time.time() - started) * 1000)
    if it is None:
        if not wait:
            raise ApiError(404, "Nothing has been expected or confirmed for value %r under key '%s'."
                           % (value, key),
                           hint='check the key, the value, ignorecase and suffix; or add wait= to '
                                'wait for it to arrive')
        body = {'key': key, 'value': value, 'match': match, 'exists': False, 'status': None,
                'group': None, 'reconciled': False, 'waitedMs': waited}
        return respond(412 if strict else 200, body)
    g = out_group(it, fields)
    ok = g['status'] in ('matched', 'reconciledByCount')
    body = {'key': key, 'value': value, 'match': match, 'exists': True, 'status': g['status'],
            'group': g, 'reconciled': ok, 'waitedMs': waited}
    return respond(412 if (strict and not ok) else 200, body)


def do_get(req):
    key = req.key()
    value_q, _ = req.text_or64('value')
    wait = req.integer('wait', 0, 0, MAX_WAIT)
    strict = req.boolean('strict', False)
    grace = req.integer('grace', 0, 0, 86400 * RETENTION_DAYS)
    sla = req.integer('sla', None, 1, 86400 * RETENTION_DAYS)
    fields = fields_param(req)
    started = time.time()
    if value_q is not None:
        return _get_one(key, check_value(value_q), wait, strict, fields, started)

    deadline = started + wait
    cfg = settings(key)
    truncated = False
    while True:
        if cfg is None:
            out = summary_from_scan([], None)
        elif fields is not None:
            items, truncated = scan_groups(key)
            out = summary_from_scan(items, fields)
        else:
            out = _summary(read_counters(key))
        if (out['expected'] and not out['outstanding']) or time.time() + POLL_S > deadline:
            break
        time.sleep(POLL_S)
        if cfg is None:
            cfg = settings(key)

    body = {'key': key, 'source': 'scan' if fields is not None else 'counters'}
    body.update(out)
    late = 0
    if grace:
        if cfg and out['unexpected']:
            late = sum(max(0, counts(it)[1] - counts(it)[0])
                       for it in query_state(key, 'over', gt_from=now_ms() - grace * 1000))
        body['graceSeconds'] = grace
        body['withinGrace'] = late
    if sla is not None:
        overdue = 0
        if cfg and out['outstanding']:
            cutoff = now_ms() - sla * 1000
            overdue = sum(max(0, counts(it)[0] - counts(it)[1])
                          for it in query_state(key, 'short') if int(it.get('et', 0)) < cutoff)
        body['slaSeconds'] = sla
        body['overdue'] = overdue
    if fields is not None:
        body['fields'] = fields
        if truncated:
            body['truncated'] = True
            body['comment'] = 'more than %d groups: counted the first %d' % (MAX_SCAN, MAX_SCAN)
    body['reconciled'] = bool(out['expected'] and not out['outstanding'] and not out['mismatched']
                              and out['unexpected'] - late <= 0)
    if cfg:
        body['ignorecase'] = cfg['ignorecase']
        body['suffix'] = cfg['suffix']
        body['expiresTime'] = cfg['expires']
    else:
        body['comment'] = 'nothing has been expected or confirmed under this key'
    body['waitedMs'] = int((time.time() - started) * 1000)
    return respond(412 if (strict and not body['reconciled']) else 200, body)


def do_list(req):
    key = req.key()
    status = (req.text('status') or 'all')
    status = next((s for s in LIST_STATUSES if s.lower() == status.lower()), status)
    if status.lower() in RENAMED_STATUS:
        raise ApiError(400, "status '%s' is now '%s': recon matches by count, so a group is short "
                            "(confirmed < expected) or over (confirmed > expected)."
                       % (status, RENAMED_STATUS[status.lower()]), allowed=list(LIST_STATUSES))
    if status not in LIST_STATUSES:
        raise ApiError(400, "'status' must be one of %s." % ', '.join(LIST_STATUSES),
                       received=status, allowed=list(LIST_STATUSES))
    start = req.integer('start', 1, 1, MAX_WINDOW)
    limit = req.integer('limit', 100, 1, MAX_LIMIT)
    if start - 1 + limit > MAX_WINDOW:
        raise ApiError(400, 'start + limit may reach at most %d groups.' % MAX_WINDOW,
                       hint='narrow status=, or count the whole key with recon/get')
    fmt = (req.text('format') or 'json').lower()
    if fmt not in ('json', 'csv'):
        raise ApiError(400, "'format' must be json or csv.", received=fmt, allowed=['json', 'csv'])
    fields = fields_param(req)
    window = start - 1 + limit
    states = STATES if status == 'all' else UNRECONCILED if status == 'unreconciled' else (status,)
    cfg = settings(key)
    truncated = False

    if cfg is None:
        items, total = [], 0
    elif fields is not None:
        # Judged again with the named fields, so the stored state cannot be used.
        found, truncated = scan_groups(key)
        items = [it for it in found if state_of(it, fields) in states]
        items.sort(key=lambda it: (int(it.get('gt', 0)), it['SK']))
        total = len(items)
    else:
        def one(arg):
            st, shard = arg
            kw = {'IndexName': 'ByState', 'KeyConditionExpression': K('gk').eq(gk(key, st, shard)),
                  'ScanIndexForward': True, 'Limit': window}
            got = []
            while len(got) < window:
                resp = _table().query(**kw)
                got.extend(it for it in resp.get('Items', []) if it.get('st') == st)
                lek = resp.get('LastEvaluatedKey')
                if not lek:
                    break
                kw['ExclusiveStartKey'] = lek
            return got[:window]
        parts = _parallel(one, [(s, i) for s in states for i in range(SHARDS)])
        items = sorted((it for p in parts for it in p), key=lambda it: (int(it.get('gt', 0)), it['SK']))
        c = read_counters(key)
        total = sum(c[GROUP_COUNTER[s]] for s in states)
    groups = [out_group(it, fields) for it in items[start - 1:start - 1 + limit]]

    if fmt == 'csv':
        ef = sorted({n for g in groups for n in g.get('expectedFields', {})})
        cf = sorted({n for g in groups for n in g.get('confirmedFields', {})})
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator='\n')
        w.writerow(['value', 'status', 'expected', 'confirmed', 'shortBy', 'overBy', 'candidates',
                    'confirmedValues', 'expectedTime', 'confirmedTime', 'latencyMs', 'differences']
                   + ['expected.' + n for n in ef] + ['confirmed.' + n for n in cf])
        for g in groups:
            w.writerow([g['value'], g['status'], g['expected'], g['confirmed'], g.get('shortBy', ''),
                        g.get('overBy', ''), ' '.join(g['candidates']),
                        ' '.join(g.get('confirmedValues', [])), g.get('expectedTime', ''),
                        g.get('confirmedTime', ''), g.get('latencyMs', ''),
                        ';'.join(d['field'] for d in g.get('differences', []))]
                       + [g.get('expectedFields', {}).get(n, '') for n in ef]
                       + [g.get('confirmedFields', {}).get(n, '') for n in cf])
        return raw(200, buf.getvalue(), 'text/csv; charset=utf-8',
                   {'Content-Disposition': 'inline; filename="%s-%s.csv"' % (key, status)})
    body = {'key': key, 'status': status, 'total': total, 'start': start,
            'returned': len(groups), 'groups': groups}
    if fields is not None:
        body['fields'] = fields
    if truncated:
        body['truncated'] = True
    if not groups:
        body['comment'] = ('nothing has been expected or confirmed under this key' if cfg is None
                           else 'no groups with this status' if start == 1 else 'start is past the end')
    return respond(200, body)


def do_cyclepop(req):
    """The short group handed out least recently; handing it out moves it to the back.
    For a checker that looks each value up in the system under test and confirms what
    it finds. Not consuming: the group stays short until enough confirmations arrive."""
    key = req.key()
    if settings(key) is None:
        return respond(200, {'key': key, 'exhausted': True, 'group': None, 'value': None,
                             'comment': 'nothing has been expected under this key'})
    t = _table()
    for attempt in range(4):
        cands = sorted(query_state(key, 'short', limit=5),
                       key=lambda it: (int(it.get('gt', 0)), it['SK']))
        if not cands:
            return respond(200, {'key': key, 'exhausted': True, 'group': None, 'value': None})
        for it in cands[:8]:
            try:
                res = t.update_item(
                    Key={'PK': it['PK'], 'SK': it['SK']},
                    UpdateExpression='SET #gt = :now ADD #k :one',
                    ConditionExpression='#st = :s AND #gt = :old',
                    ExpressionAttributeNames={'#gt': 'gt', '#st': 'st', '#k': 'k'},
                    ExpressionAttributeValues={':now': now_ms(), ':old': it['gt'], ':s': 'short',
                                               ':one': 1},
                    ReturnValues='ALL_NEW')
            except ClientError as exc:
                if not _cond_failed(exc):
                    raise
                continue        # taken by another caller, or confirmed meanwhile
            g = out_group(res['Attributes'])
            return respond(200, {'key': key, 'exhausted': False, 'group': g,
                                 'value': g['value'], 'checks': g.get('checks', 1)})
        time.sleep(0.02 * (attempt + 1))
    return respond(200, {'key': key, 'exhausted': False, 'group': None, 'value': None,
                         'comment': 'every candidate was taken by other callers; call again'})


def do_clear(req):
    key = req.key()
    budget = [MAX_CLEAR]
    lock = threading.Lock()
    # Groups and summaries, and the first version's records (#R) and counters (#C).
    partitions = [p % (key, i) for i in range(SHARDS) for p in ('%s#G%d', '%s#S%d', '%s#R%d', '%s#C%d')]

    def one(pkv):
        tbl = _table()
        kw = {'KeyConditionExpression': K('PK').eq(pkv), 'ProjectionExpression': 'PK, SK'}
        n = 0
        with tbl.batch_writer() as bw:
            while True:
                resp = tbl.query(**kw)
                for it in resp.get('Items', []):
                    with lock:
                        if budget[0] <= 0:
                            return n, True
                        budget[0] -= 1
                    bw.delete_item(Key={'PK': it['PK'], 'SK': it['SK']})
                    n += 1
                lek = resp.get('LastEvaluatedKey')
                if not lek:
                    return n, False
                kw['ExclusiveStartKey'] = lek
    parts = _parallel(one, partitions)
    deleted = sum(n for n, _ in parts)
    more = any(m for _, m in parts)
    existed = deleted > 0
    if not more:
        # The settings go last, so a partly cleared key still reports itself.
        t = _table()
        existed = existed or 'Item' in t.get_item(Key={'PK': mpk(key), 'SK': 'M'})
        t.delete_item(Key={'PK': mpk(key), 'SK': 'M'})
    return respond(200, {'key': key, 'deleted': deleted, 'cleared': existed and not more,
                         'more': more,
                         'comment': ('partially cleared - call again until more is false' if more
                                     else 'cleared' if existed else 'nothing to clear')})


ROUTES = {'expect': do_expect, 'confirm': do_confirm, 'get': do_get, 'list': do_list,
          'cyclepop': do_cyclepop, 'clear': do_clear}
INDEX = {'description': 'Reconcile what a test submitted (expect) with what it saw processed '
                        '(confirm), matched by count per key - optionally on the last N characters '
                        '(suffix=) so full values can meet masked ones: records outstanding and '
                        'unexpected, groups short, over or reconciled by count with their '
                        'candidates, processing lag, and a pass/fail gate.'}


def handler(event, context):
    return run(event, SERVICE, ROUTES, INDEX)
