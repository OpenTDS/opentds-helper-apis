"""perf - performance results and statistics (v1).

GET /v1/perf/add?key=Login&value=248.5[,301,...][&ok=false&time=EPOCH&raw=true]
GET /v1/perf/stats?key=Login[&p=99.5&since=&until=]
GET /v1/perf/series?key=Login[&interval=60&p=&since=&until=]
GET /v1/perf/assert?key=Login&p90lt=800&errorratelt=0.01[&strict=true]
GET /v1/perf/keys[?prefix=]
GET /v1/perf/clear?key=Login
GET /v1/perf/export?key=Login[&format=csv|json&limit=&since=&until=]

Table (env PERF_TABLE): PK (S) / SK (S), TTL attribute 'ttl'.
  PK "<key>#<shard>"  SK "AGG"                     lifetime accumulators
  PK "<key>#<shard>"  SK "BKT#<epoch minute>"      one-minute interval bucket (TTL)
  PK "<key>#<shard>"  SK "RAW#<epoch ms>#<nnnn>"   individual point, raw=true only (TTL)
  PK "__KEYS__"       SK "<key>"                   key registry (t0 / t1)
Accumulator attributes: n count, s sum, q sum of squares, e errors, z exact
zeros, mn / mx bounds, t0 / t1 first / last point time, bNNN histogram buckets.
"""
import math
import os
import random
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import boto3
from boto3.dynamodb.conditions import Key as K
from botocore.exceptions import ClientError

from common import ApiError, raw, respond, run

SERVICE = 'perf'

# --------------------------------------------------------------------------
# Write sharding. Every item for one series would otherwise share a single
# DynamoDB partition key, and a partition tops out near 1,000 writes/second -
# which a load test reaches with ease. The shard goes in the PARTITION key
# (PK = "<key>#<shard>"), because sharding the sort key would not help.
#
# Reads always sweep 0..READ_SHARDS-1, so WRITE_SHARDS can be lowered later
# without stranding data. Everything accumulated here is additive, so merging
# the shards on read is exact rather than approximate.
# --------------------------------------------------------------------------
WRITE_SHARDS = 16
READ_SHARDS = 16

# Log-spaced histogram. A fixed bucket RATIO fixes the *relative* error, which
# is the right guarantee for durations: 2% of a 40 ms p50 is 0.8 ms and 2% of
# a 4 s p99 is 80 ms. Storing every point instead would be exact but could not
# be read back inside an API Gateway timeout once a key held a million rows.
RATIO = 1.04
LOG_RATIO = math.log(RATIO)
LO = 1e-3
HI = 1e9
NBUCKETS = int(math.ceil(math.log(HI / LO) / LOG_RATIO)) + 1

BUCKET_SECONDS = 60          # write-time granularity of the time series
RETENTION_DAYS = 7           # TTL on interval buckets and raw points
MAX_VALUE = Decimal('1e12')
MAX_DECIMALS = 6             # keeps sums well inside DynamoDB's 38 digits
MAX_BATCH_VALUES = 100       # value=1,2,3 - one call, many points
MAX_SERIES_INTERVALS = 500
MAX_SERIES_ITEMS = 20000
MAX_EXPORT = 50000
MAX_CLEAR = 5000
MAX_KEYS = 2000
REGISTRY = '__KEYS__'        # cannot collide: v1 keys start with a letter, digit or +
# Epoch seconds above this are almost certainly milliseconds sent by mistake.
MAX_EPOCH_S = 99999999999
# How often one container refreshes a key's lastTime in the registry.
REGISTRY_REFRESH_S = 60

STANDARD_PCTS = (50, 75, 90, 95, 99, 99.9)
RENAMED = {'from': 'since', 'to': 'until', 'ts': 'time'}

# Container-local: when this container last touched a key's registry entry.
# Only throttles the lastTime refresh; a freshly created shard aggregate always
# (re)registers the key, so a clear() elsewhere cannot leave it unregistered.
_registered = {}

_res = None
_tbl = None


def _table():
    global _res, _tbl
    if _tbl is None:
        _res = boto3.resource('dynamodb')
        _tbl = _res.Table(os.environ['PERF_TABLE'])
    return _tbl


# ------------------------------------------------------------- histogram
def bucket_index(v):
    if v <= LO:
        return 0
    i = int(math.floor(math.log(v / LO) / LOG_RATIO))
    return NBUCKETS - 1 if i >= NBUCKETS else i


def bucket_mid(i):
    # Geometric midpoint: the representative that minimises relative error.
    return LO * (RATIO ** i) * math.sqrt(RATIO)


def bucket_name(i):
    return 'b%03d' % i


def merge(items):
    out = {'n': 0, 's': Decimal(0), 'q': Decimal(0), 'e': 0, 'z': 0,
           'mn': None, 'mx': None, 't0': None, 't1': None, 'b': {}}
    for it in items:
        if not it:
            continue
        out['n'] += int(it.get('n', 0))
        out['s'] += Decimal(str(it.get('s', 0)))
        out['q'] += Decimal(str(it.get('q', 0)))
        out['e'] += int(it.get('e', 0))
        out['z'] += int(it.get('z', 0))
        for k, v in it.items():
            if len(k) == 4 and k[0] == 'b' and k[1:].isdigit():
                i = int(k[1:])
                out['b'][i] = out['b'].get(i, 0) + int(v)
        for key, fn in (('mn', min), ('mx', max), ('t0', min), ('t1', max)):
            v = it.get(key)
            if v is None:
                continue
            v = float(v) if key in ('mn', 'mx') else int(v)
            out[key] = v if out[key] is None else fn(out[key], v)
    return out


def percentile(a, p):
    n = a['n']
    if not n:
        return None
    if p <= 0:
        return a['mn']
    if p >= 100:
        return a['mx']
    target = max(1, int(math.ceil(p / 100.0 * n)))
    cum = a.get('z', 0)                      # exact zeros sort lowest
    if cum >= target:
        return 0.0
    for i in sorted(a['b']):
        cum += a['b'][i]
        if cum >= target:
            v = bucket_mid(i)
            # never report outside the exactly-known bounds
            if a['mn'] is not None and v < a['mn']:
                v = a['mn']
            if a['mx'] is not None and v > a['mx']:
                v = a['mx']
            return round(v, 4)
    return a['mx']


def pname(p):
    """99.9 -> 'p99_9' (the response field name for a percentile)."""
    return 'p' + ('%g' % p).replace('.', '_')


def summarise(a, extra=()):
    n = a['n']
    if not n:
        return {'count': 0, 'comment': 'no data points for this key'}
    mean = a['s'] / Decimal(n)
    # The sums are accumulated by DynamoDB as exact decimals, so the variance
    # avoids the cancellation a float accumulator would suffer at this scale.
    var = a['q'] / Decimal(n) - mean * mean
    if var < 0:
        var = Decimal(0)
    out = {'count': n,
           'errors': a['e'],
           'errorRate': round(a['e'] / float(n), 6),
           'min': a['mn'],
           'max': a['mx'],
           'mean': round(float(mean), 4),
           'stdDev': round(math.sqrt(float(var)), 4),
           'sum': round(float(a['s']), 4)}
    for p in STANDARD_PCTS:
        out[pname(p)] = percentile(a, p)
    for p in extra:
        out[pname(p)] = percentile(a, p)
    if a['t0'] is not None:
        dur = a['t1'] - a['t0']
        out['firstTime'] = a['t0']
        out['lastTime'] = a['t1']
        out['durationSeconds'] = dur
        out['throughput'] = round(n / float(dur), 4) if dur > 0 else None
    return out


# ------------------------------------------------------------ parameters
def pk(key, shard):
    return '%s#%d' % (key, shard)


def reject_renamed(req):
    """Pre-v1 parameter names would otherwise be silently ignored (from/to) or
    mistaken for assert checks - say what they are called now."""
    for old, new in RENAMED.items():
        if old in req.qs:
            raise ApiError(400, "'%s' was renamed to '%s' in v1." % (old, new),
                           hint='since/until/time are UTC epoch seconds')


def epoch(req, name):
    v = req.integer(name)
    if v is None:
        return None
    if v < 0 or v > MAX_EPOCH_S:
        raise ApiError(400, "'%s' must be UTC epoch SECONDS." % name, received=req.get(name),
                       hint='a 13-digit value is milliseconds - divide by 1000')
    return v


def window(req):
    since, until = epoch(req, 'since'), epoch(req, 'until')
    if since is not None and until is not None and since > until:
        raise ApiError(400, "'since' must not be after 'until'.", since=since, until=until)
    return since, until


def want_pcts(req):
    raw_p = req.text('p') or ''
    out = []
    for part in raw_p.replace(' ', '').split(','):
        if not part:
            continue
        try:
            v = float(part)
        except ValueError:
            raise ApiError(400, "'p' must be a comma-separated list of percentiles, "
                                "e.g. p=99.5,99.99.", received=raw_p)
        if not 0 <= v <= 100 or math.isnan(v):
            raise ApiError(400, "percentiles in 'p' must be between 0 and 100.",
                           received=raw_p)
        if v not in out:
            out.append(v)
    return out


def parse_value(raw_v):
    """Returns a Decimal; raises 400 for what the histogram cannot represent."""
    try:
        v = Decimal(raw_v.strip())
    except (InvalidOperation, AttributeError):
        raise ApiError(400, 'value is not a number.', received=raw_v)
    if not v.is_finite():
        raise ApiError(400, 'value must be finite.', received=raw_v)
    if v < 0:
        raise ApiError(400, 'negative values are not supported - the histogram is '
                            'log-spaced; record the magnitude and the sign separately.',
                       received=raw_v)
    if v > MAX_VALUE:
        raise ApiError(400, 'value exceeds the maximum of 1e12.', received=raw_v)
    if -v.as_tuple().exponent > MAX_DECIMALS:
        v = v.quantize(Decimal('1e-%d' % MAX_DECIMALS), rounding=ROUND_HALF_UP)
    return v


def num(v):
    """Decimal from DynamoDB -> plain JSON/CSV number (no 1E+3 forms)."""
    if v is None:
        return None
    d = Decimal(str(v))
    return int(d) if d == d.to_integral_value() else float(d)


# ---------------------------------------------------------------- writes
def _bump(pkv, sk, v, vv, one_err, now, ttl):
    """Fold one point into an accumulator item. No read-modify-write, so
    concurrent virtual users cannot lose each other's updates.

    mn/mx are seeded with if_not_exists so the first point into a fresh item
    sets them, and ALL_NEW hands back the stored bounds at no capacity cost -
    the caller only issues the conditional bound write when this point is a
    new extreme. (ALL_NEW rather than UPDATED_NEW: whether an if_not_exists
    that changed nothing counts as 'updated' is not something to rely on.)
    Returns the item as it is after the update."""
    names = {'#n': 'n', '#s': 's', '#q': 'q', '#t0': 't0', '#t1': 't1',
             '#mn': 'mn', '#mx': 'mx'}
    vals = {':o': 1, ':v': v, ':q': vv, ':now': now}
    add = ['#n :o', '#s :v', '#q :q']
    setc = ['#t0 = if_not_exists(#t0, :now)', '#t1 = if_not_exists(#t1, :now)',
            '#mn = if_not_exists(#mn, :v)', '#mx = if_not_exists(#mx, :v)']
    if v == 0:
        names['#z'] = 'z'
        add.append('#z :o')
    else:
        names['#b'] = bucket_name(bucket_index(float(v)))
        add.append('#b :o')
    if one_err:
        names['#e'] = 'e'
        add.append('#e :o')
    if ttl:
        names['#ttl'] = 'ttl'
        vals[':ttl'] = ttl
        setc.append('#ttl = :ttl')
    res = _table().update_item(
        Key={'PK': pkv, 'SK': sk},
        UpdateExpression='SET ' + ', '.join(setc) + ' ADD ' + ', '.join(add),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
        ReturnValues='ALL_NEW')
    return res.get('Attributes') or {}


def _bound(pkv, sk, attr, v, cmp_op):
    try:
        _table().update_item(
            Key={'PK': pkv, 'SK': sk},
            UpdateExpression='SET #a = :v',
            ConditionExpression='attribute_not_exists(#a) OR #a %s :v' % cmp_op,
            ExpressionAttributeNames={'#a': attr},
            ExpressionAttributeValues={':v': v})
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        # another writer already holds a better bound


def _fold(pkv, sk, v, ok, now, ttl):
    got = _bump(pkv, sk, v, v * v, not ok, now, ttl)
    # t0/t1 are the earliest/latest POINT times, not write times: a point
    # sent with time= in the past (or out of order) widens them, never narrows.
    t0, t1 = got.get('t0'), got.get('t1')
    if t0 is not None and now < t0:
        _bound(pkv, sk, 't0', now, '>')
    if t1 is not None and now > t1:
        _bound(pkv, sk, 't1', now, '<')
    mn, mx = got.get('mn'), got.get('mx')
    if mn is not None and v < mn:
        _bound(pkv, sk, 'mn', v, '>')
    if mx is not None and v > mx:
        _bound(pkv, sk, 'mx', v, '<')
    return got


def do_add(req):
    reject_renamed(req)
    key = req.key()
    raw_v = req.text('value')
    if raw_v is None:
        raise ApiError(400, "Missing required query parameter 'value'.",
                       hint='value=248.5, or value=248.5,301,199 for several points')
    parts = [p for p in raw_v.split(',') if p.strip() != '']
    if len(parts) > MAX_BATCH_VALUES:
        raise ApiError(400, 'Too many values in one call.',
                       maximum=MAX_BATCH_VALUES, received=len(parts))
    values = [parse_value(p) for p in parts]
    if not values:
        raise ApiError(400, "Missing required query parameter 'value'.")

    ok = req.boolean('ok', True)
    keep_raw = req.boolean('raw', False)

    wall = int(time.time())
    given = epoch(req, 'time')
    now = wall if given is None else given
    minute = now - (now % BUCKET_SECONDS)
    # Retention runs from when the point was WRITTEN, so a backfilled run
    # (time= in the past) is still readable for the full retention period.
    ttl = wall + RETENTION_DAYS * 86400
    shard = random.randrange(WRITE_SHARDS)
    pkv = pk(key, shard)

    fresh = False
    for v in values:
        agg = _fold(pkv, 'AGG', v, ok, now, None)
        if int(agg.get('n', 0)) == 1:
            fresh = True
        _fold(pkv, 'BKT#%013d' % minute, v, ok, now, ttl)
        if keep_raw:
            # Stored against the point's own time, so export windows and
            # ordering follow time= when a run is backfilled.
            ms = now * 1000 if given is not None else int(time.time() * 1000)
            _table().put_item(Item={'PK': pkv,
                                    'SK': 'RAW#%015d#%04d' % (ms, random.randrange(10000)),
                                    'v': v, 'ok': ok, 'ttl': ttl})

    last = _registered.get(key)
    # A point with an explicit time= may widen the key's first/last time in any
    # direction, so it always refreshes the registry; plain points refresh it at
    # most once a minute (lastTime in perf/keys can lag by up to that).
    if fresh or given is not None or last is None or wall - last >= REGISTRY_REFRESH_S:
        if len(_registered) > 5000:
            _registered.clear()
        _registered[key] = wall
        try:
            _table().update_item(
                Key={'PK': REGISTRY, 'SK': key},
                UpdateExpression='SET #t0 = if_not_exists(#t0, :now), #t1 = if_not_exists(#t1, :now)',
                ExpressionAttributeNames={'#t0': 't0', '#t1': 't1'},
                ExpressionAttributeValues={':now': now})
            _bound(REGISTRY, key, 't0', now, '>')
            _bound(REGISTRY, key, 't1', now, '<')
        except ClientError:
            pass    # the registry is a convenience index; never fail a write for it

    return respond(200, {'key': key, 'recorded': len(values), 'time': now,
                         'ok': ok, 'raw': keep_raw})


# ----------------------------------------------------------------- reads
def read_agg(key):
    """One BatchGetItem across the shards, so a summary is a single round trip."""
    t = _table()
    keys = [{'PK': pk(key, i), 'SK': 'AGG'} for i in range(READ_SHARDS)]
    items, pending = [], {t.name: {'Keys': keys}}
    for _ in range(5):
        resp = _res.batch_get_item(RequestItems=pending)
        items.extend(resp['Responses'].get(t.name, []))
        pending = resp.get('UnprocessedKeys') or {}
        if not pending:
            break
    return items


def read_buckets(key, since, until, budget=MAX_SERIES_ITEMS):
    """Interval items across the shards, bounded by an item budget.

    A bucket is included when its minute STARTS inside [since, until]."""
    t = _table()
    lo = 'BKT#%013d' % (since if since is not None else 0)
    hi = 'BKT#%013d' % (until if until is not None else 9999999999)
    out, truncated = [], False
    for i in range(READ_SHARDS):
        kw = {'KeyConditionExpression': K('PK').eq(pk(key, i)) & K('SK').between(lo, hi)}
        while True:
            resp = t.query(**kw)
            out.extend(resp.get('Items', []))
            if len(out) >= budget:
                truncated = True
                break
            lek = resp.get('LastEvaluatedKey')
            if not lek:
                break
            kw['ExclusiveStartKey'] = lek
        if truncated:
            break
    return out, truncated


def _aggregate(key, since, until):
    """(accumulator, source, truncated) - lifetime unless a window is given."""
    if since is None and until is None:
        return merge(read_agg(key)), 'lifetime', False
    items, trunc = read_buckets(key, since, until)
    return merge(items), 'window', trunc


def do_stats(req):
    reject_renamed(req)
    key = req.key()
    since, until = window(req)
    pcts = want_pcts(req)
    a, src, trunc = _aggregate(key, since, until)
    out = {'key': key, 'source': src}
    if src == 'window':
        out['since'] = since
        out['until'] = until
    out.update(summarise(a, pcts))
    if trunc:
        out['truncated'] = True
        out['comment'] = ('too many interval buckets in this window; narrow since/until. '
                          'Lifetime figures (no since/until) are never truncated.')
    if src == 'window':
        out['comment'] = out.get('comment', '') or (
            'windowed figures come from %ds interval buckets, which are kept for %d days'
            % (BUCKET_SECONDS, RETENTION_DAYS))
    return respond(200, out)


def do_series(req):
    reject_renamed(req)
    key = req.key()
    interval = req.integer('interval', BUCKET_SECONDS)
    if interval < BUCKET_SECONDS or interval % BUCKET_SECONDS:
        raise ApiError(400, "'interval' must be a multiple of %d seconds." % BUCKET_SECONDS,
                       received=req.get('interval'))
    since, until = window(req)
    pcts = want_pcts(req) or [90.0]
    items, trunc = read_buckets(key, since, until)
    if not items:
        return respond(200, {'key': key, 'intervalSeconds': interval, 'buckets': 0,
                             'series': [],
                             'comment': 'no interval data - either nothing recorded, or older '
                                        'than the %d-day retention on interval buckets'
                                        % RETENTION_DAYS})
    groups = {}
    for it in items:
        start = int(it['SK'].split('#')[1])
        groups.setdefault(start - (start % interval), []).append(it)

    rows = []
    for start in sorted(groups):
        a = merge(groups[start])
        row = {'startTime': start, 'count': a['n'], 'errors': a['e'],
               'errorRate': round(a['e'] / float(a['n']), 6) if a['n'] else 0,
               'min': a['mn'], 'max': a['mx'],
               'mean': round(float(a['s'] / Decimal(a['n'])), 4) if a['n'] else None,
               'throughput': round(a['n'] / float(interval), 4)}
        for p in pcts:
            row[pname(p)] = percentile(a, p)
        rows.append(row)
    if len(rows) > MAX_SERIES_INTERVALS:
        rows = rows[-MAX_SERIES_INTERVALS:]
        trunc = True
    out = {'key': key, 'intervalSeconds': interval, 'buckets': len(rows), 'series': rows}
    if trunc:
        out['truncated'] = True
        out['comment'] = ('capped at %d intervals / %d interval items; widen interval= or '
                          'narrow since/until' % (MAX_SERIES_INTERVALS, MAX_SERIES_ITEMS))
    return respond(200, out)


# ---------------------------------------------------------------- assert
CHECK_RE = re.compile(r'^(count|errors|errorrate|mean|min|max|stddev|sum|throughput'
                      r'|p\d+(?:[._]\d+)?)(lt|le|gt|ge|eq)$')
OPS = {'lt': ('<', lambda a, b: a < b), 'le': ('<=', lambda a, b: a <= b),
       'gt': ('>', lambda a, b: a > b), 'ge': ('>=', lambda a, b: a >= b),
       'eq': ('==', lambda a, b: a == b)}
STAT_FIELD = {'count': 'count', 'errors': 'errors', 'errorrate': 'errorRate',
              'mean': 'mean', 'min': 'min', 'max': 'max', 'stddev': 'stdDev',
              'sum': 'sum', 'throughput': 'throughput'}
# Parameters that are never checks (other perf actions' parameters are
# tolerated so a URL edited from stats/series does not fail).
RESERVED = {'key', 'p', 'since', 'until', 'interval', 'strict', 'value',
            'ok', 'raw', 'time', 'prefix', 'format', 'limit'}
CHECK_HINT = ('use <statistic><operator>=<number>, e.g. p90lt=800, errorratelt=0.01, '
              'countgt=1000. Statistics: count, errors, errorrate, mean, min, max, stddev, '
              'sum, throughput, or any percentile such as p99 or p99.9. '
              'Operators: lt, le, gt, ge, eq.')


def _pct_of(stat):
    return float(stat[1:].replace('_', '.'))


def do_assert(req):
    reject_renamed(req)
    key = req.key()

    checks = []
    for name, raw_t in req.qs.items():
        if name in RESERVED:
            continue
        m = CHECK_RE.match(name)
        if not m:
            raise ApiError(400, 'Unrecognised check: %s' % name, hint=CHECK_HINT)
        try:
            threshold = float(raw_t)
        except (TypeError, ValueError):
            raise ApiError(400, 'Threshold for %s is not a number.' % name, received=raw_t)
        stat = m.group(1)
        if stat.startswith('p') and not 0 <= _pct_of(stat) <= 100:
            raise ApiError(400, 'Percentile in %s must be between 0 and 100.' % name)
        checks.append((name, stat, m.group(2), threshold))
    if not checks:
        raise ApiError(400, 'No checks supplied.',
                       hint='e.g. perf/assert?key=K&p90lt=800&errorratelt=0.01')

    since, until = window(req)
    a, src, _ = _aggregate(key, since, until)
    extra = [_pct_of(s) for _, s, _, _ in checks if s.startswith('p')]
    summary = summarise(a, extra)

    # A key with no data FAILS: a run that recorded nothing must not pass a gate.
    if not summary.get('count'):
        out = {'key': key, 'source': src, 'pass': False, 'failed': len(checks),
               'checks': [{'check': n, 'pass': False,
                           'comment': 'no data points for this key'}
                          for n, _, _, _ in checks],
               'summary': summary}
    else:
        results, failed = [], 0
        for name, stat, op, threshold in checks:
            field = STAT_FIELD.get(stat) or pname(_pct_of(stat))
            actual = summary.get(field)
            sym, fn = OPS[op]
            passed = actual is not None and fn(float(actual), threshold)
            if not passed:
                failed += 1
            results.append({'check': name, 'statistic': field, 'actual': actual,
                            'operator': sym, 'threshold': threshold, 'pass': passed})
        out = {'key': key, 'source': src, 'pass': failed == 0, 'failed': failed,
               'checks': results, 'summary': summary}
    strict = req.boolean('strict', False)
    return respond(412 if (strict and not out['pass']) else 200, out)


# ------------------------------------------------------------ keys / clear
def do_keys(req):
    prefix = req.text('prefix') or ''
    cond = K('PK').eq(REGISTRY)
    if prefix:
        cond = cond & K('SK').begins_with(prefix)
    out, kw, truncated = [], {'KeyConditionExpression': cond}, False
    while True:
        resp = _table().query(**kw)
        for it in resp.get('Items', []):
            out.append({'key': it['SK'],
                        'firstTime': int(it.get('t0', 0)),
                        'lastTime': int(it.get('t1', 0))})
        lek = resp.get('LastEvaluatedKey')
        if len(out) >= MAX_KEYS:
            truncated = len(out) > MAX_KEYS or bool(lek)
            out = out[:MAX_KEYS]
            break
        if not lek:
            break
        kw['ExclusiveStartKey'] = lek
    body = {'count': len(out), 'prefix': prefix or None, 'keys': out}
    if truncated:
        body['truncated'] = True
    return respond(200, body)


def do_clear(req):
    key = req.key()
    t = _table()
    deleted, more = 0, False
    for i in range(READ_SHARDS):
        kw = {'KeyConditionExpression': K('PK').eq(pk(key, i)),
              'ProjectionExpression': 'PK, SK'}
        with t.batch_writer() as bw:
            while True:
                resp = t.query(**kw)
                for it in resp.get('Items', []):
                    bw.delete_item(Key={'PK': it['PK'], 'SK': it['SK']})
                    deleted += 1
                lek = resp.get('LastEvaluatedKey')
                if not lek or deleted >= MAX_CLEAR:
                    more = bool(lek)
                    break
                kw['ExclusiveStartKey'] = lek
        if deleted >= MAX_CLEAR:
            more = True
            break
    _registered.pop(key, None)
    if not more:
        t.delete_item(Key={'PK': REGISTRY, 'SK': key})
    return respond(200, {'key': key, 'deleted': deleted, 'more': more,
                         'comment': ('cleared' if not more else
                                     'partially cleared - call again to continue')})


def do_export(req):
    reject_renamed(req)
    key = req.key()
    fmt = (req.text('format') or 'csv').lower()
    if fmt not in ('csv', 'json'):
        raise ApiError(400, "'format' must be csv or json.", received=fmt,
                       allowed=['csv', 'json'])
    since, until = window(req)
    limit = req.integer('limit', 10000, 1, MAX_EXPORT)
    lo = 'RAW#%015d' % ((since or 0) * 1000)
    hi = 'RAW#%015d~' % (((until if until is not None else 9999999999) * 1000) + 999)

    # Each shard is read oldest-first up to limit+1 points and merged into a
    # running oldest-first list trimmed to limit+1, so the result is the
    # OLDEST `limit` points across all shards, memory stays bounded, and
    # truncation is detected exactly.
    t = _table()
    rows = []
    for i in range(READ_SHARDS):
        kw = {'KeyConditionExpression': K('PK').eq(pk(key, i)) & K('SK').between(lo, hi)}
        got = []
        while len(got) <= limit:
            resp = t.query(**kw)
            for it in resp.get('Items', []):
                got.append((int(it['SK'].split('#')[1]), num(it.get('v')),
                            bool(it.get('ok', True))))
            lek = resp.get('LastEvaluatedKey')
            if not lek:
                break
            kw['ExclusiveStartKey'] = lek
        rows = sorted(rows + got)[:limit + 1]
    truncated = len(rows) > limit
    rows = rows[:limit]
    if fmt == 'json':
        body = {'key': key, 'count': len(rows), 'truncated': truncated,
                'points': [{'timestampMs': a, 'value': b, 'ok': c} for a, b, c in rows]}
        if not rows:
            body['comment'] = ('no raw points - record them with raw=true; they are kept '
                               'for %d days' % RETENTION_DAYS)
        return respond(200, body)
    lines = ['timestampMs,value,ok']
    lines.extend('%d,%s,%s' % (a, b, 'true' if c else 'false') for a, b, c in rows)
    if truncated:
        lines.append('# truncated at limit=%d' % limit)
    if not rows:
        lines.append('# no raw points - record them with raw=true, kept for %d days'
                     % RETENTION_DAYS)
    return raw(200, '\n'.join(lines) + '\n', 'text/csv; charset=utf-8',
               {'Content-Disposition': 'inline; filename="%s.csv"' % key})


ROUTES = {'add': do_add, 'stats': do_stats, 'series': do_series,
          'assert': do_assert, 'keys': do_keys, 'clear': do_clear,
          'export': do_export}
INDEX = {'description': 'Record performance data points and read back exact counts, '
                        'means and bounds, histogram percentiles, time series and '
                        'pass/fail gates.'}


def handler(event, context):
    return run(event, SERVICE, ROUTES, INDEX)
