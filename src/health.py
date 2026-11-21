"""health - which build is deployed, and an end-to-end self-test of the stack.

  /v1/health/get      the build, region and services; touches nothing, calls
                      nothing (plus a summary of the last check, if there is one)
  /v1/health/check    runs one probe per service through the public API - real
                      requests with known answers - and reports each one
      fresh=true      run now instead of serving the cached result (refused while
                      the cached result is under FRESH_FLOOR_S seconds old)
      strict=true     503 when status is not ok, for uptime monitors

Every probe goes through https://<api host>/v1 like any caller, so routing, the
certificate, the custom domain and IAM are tested too, not just the code. The
probes work in a private namespace (a random key created on first use and never
returned), so nobody can disturb them, and they clear up after themselves.

The result is cached in the health table for CACHE_SECONDS: the endpoint is
public and a check costs about 40 requests and one email, so a burst of callers
gets the cached copy (with ageSeconds) instead of multiplying that. A lease
stops two checks running at once.

Both actions return traceId / traceUrl: this request's AWS X-Ray trace as a
public page (trace/get?format=html - no sign-in), traceDataUrl the same as JSON,
and traceConsoleUrl the AWS console view for whoever operates the deployment.
The trace is written as the request finishes, so it appears a few seconds after
the response (the page waits for it). health/check
passes its trace header on its probe requests, so one trace holds the health
function and every service request it made; a cached result also gives
checkTraceUrl, the trace of the run that produced it.

status: ok        every probe passed and every function is on this build
        degraded  some probe failed or timed out, or a function is on another build
        down      the stateful core (lists, lock, webhook - all DynamoDB) failed,
                  or nothing passed at all
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures

import boto3
from botocore.exceptions import ClientError

from common import ApiError, base_url, build_info, respond, run

TABLE = os.environ.get('HEALTH_TABLE', 'health')
MAIL_DOMAIN = os.environ.get('MAIL_DOMAIN', '')

CACHE_SECONDS = 60          # a check younger than this is served from the cache
FRESH_FLOOR_S = 30          # fresh=true is refused while the cache is younger than this
DEADLINE_S = 20             # every probe must finish inside this (the gateway gives up at 29)
LEASE_S = 26                # longer than a check can take
MAIL_WAIT_S = 15            # how long the mail probe waits for its message to arrive
RESULT_TTL_S = 7 * 86400

# The services the API exposes.
SERVICES = ['lists', 'names', 'lock', 'mail', 'sms', 'webhook', 'totp', 'qr', 'perf',
            'recon', 'voice', 'health', 'trace']
CORE = ('lists', 'lock', 'webhook')

# Known answers. tests/test_health.py recomputes these from the service code, so a
# change to names or qr that alters their output fails the suite until updated here.
GOLDEN = {
    'names': {'region': 'AU', 'seed': 'health-check',
              'femaleFullName': 'Grace Debbie Gallo', 'maleFullName': 'Clive Neville Gallo'},
    'qr': {'text': 'HEALTH CHECK',
           'sha256': 'c7f11a94fa5c390539e0bb2fbea90e7c7a937f9dec598f34a516470e78c04a6f'},
    # RFC 6238 appendix B, SHA-1, 8 digits: secret = ASCII "12345678901234567890".
    'totp': {'secret': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',
             'vectors': [(59, '94287082'), (1111111109, '07081804'), (20000000000, '65353130')]},
    'voice': {'text': 'Health check.', 'heard': 'health check'},
}

_table = None
_lambda = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource('dynamodb').Table(TABLE)
    return _table


def lambda_client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client('lambda')
    return _lambda


# ------------------------------------------------------------------ X-Ray
CONSOLE_TRACE = 'https://console.aws.amazon.com/xray/home?region={region}#/traces/{trace}'


def trace_header(req=None):
    """This invocation's X-Ray header (Root=...;Parent=...;Sampled=...). Lambda sets
    _X_AMZN_TRACE_ID per invocation; the request header is the fallback."""
    h = os.environ.get('_X_AMZN_TRACE_ID')
    if not h and req is not None:
        h = req.headers.get('x-amzn-trace-id')
    return h or ''


def trace_info(header, base):
    """{'traceId', 'traceSampled', 'traceUrl', 'traceDataUrl', 'traceConsoleUrl'} from an
    X-Ray trace header. traceUrl is this API's own public trace page."""
    fields = dict(p.split('=', 1) for p in header.split(';') if '=' in p)
    root = (fields.get('Root') or '').lower() or None
    sampled = fields.get('Sampled') == '1'
    region = os.environ.get('AWS_REGION') or ''
    traced = bool(root and sampled)
    out = {'traceId': root, 'traceSampled': sampled,
           'traceUrl': '%s/trace/get?ref=%s&format=html' % (base, root) if traced else None,
           'traceDataUrl': '%s/trace/get?ref=%s' % (base, root) if traced else None,
           'traceConsoleUrl': CONSOLE_TRACE.format(region=region, trace=root)
           if traced and region else None}
    if not traced:
        out['traceComment'] = ('this request was not traced: tracing is switched off on this '
                               'deployment (Tracing=false), or X-Ray sampling skipped it')
    return out


# ------------------------------------------------------------------ HTTP to our own API
class Fail(Exception):
    def __init__(self, message, hint=None, status='fail', **detail):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status
        self.detail = detail


class Resp:
    __slots__ = ('status', 'headers', 'body')

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body if isinstance(body, bytes) else (body or '').encode('utf-8')

    def json(self):
        try:
            return json.loads(self.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise Fail('HTTP %d with a body that is not JSON' % self.status,
                       body=self.body[:200].decode('utf-8', 'replace'))


def http(method, url, body=None, headers=None, timeout=10):
    """One request; never raises for an HTTP status. Replaced in tests."""
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    req.add_header('User-Agent', 'testhelper-health/1')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Resp(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return Resp(e.code, dict(e.headers or {}), e.read() or b'')


class Ctx:
    """What one probe needs: our base URL, the private namespace, the deadline."""

    def __init__(self, base, ns, run_id, deadline, trace=''):
        self.base, self.ns, self.run_id, self.deadline = base, ns, run_id, deadline
        self.trace = trace           # X-Ray header to pass on, so probes join this trace
        self.builds = set()
        self.calls = 0
        self._lock = threading.Lock()

    def left(self):
        return self.deadline - time.time()

    def call(self, path, params=None, method='GET', body=None, headers=None, expect=200,
             as_json=True):
        left = self.left()
        if left <= 0.5:
            raise TimeoutError('deadline reached before ' + path)
        url = self.base + '/' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        headers = dict(headers or {})
        if self.trace:
            headers['X-Amzn-Trace-Id'] = self.trace
        try:
            r = http(method, url, body, headers, timeout=min(left, 18))
        except (TimeoutError, OSError) as e:
            if self.left() <= 0.5:
                raise TimeoutError(path) from e
            raise Fail('%s: %s' % (path, e)) from e
        with self._lock:
            self.calls += 1
            if r.headers.get('x-build'):
                self.builds.add(r.headers['x-build'])
        if expect is not None and r.status != expect:
            detail = {}
            try:
                detail = json.loads(r.body.decode('utf-8')) or {}
            except (ValueError, UnicodeDecodeError, AttributeError):
                pass
            gw = detail.get('gatewayError') if isinstance(detail, dict) else None
            raise Fail('%s returned HTTP %d (expected %d)' % (path, r.status, expect),
                       hint=('API Gateway throttled the probe: ThrottleRateLimit / '
                             'ThrottleBurstLimit are too low for a check (about 40 requests '
                             'in a few seconds)' if gw == 'THROTTLED' else None),
                       error=detail.get('error') if isinstance(detail, dict) else None,
                       gatewayError=gw)
        return r.json() if as_json else r


def expect(cond, message, **detail):
    if not cond:
        raise Fail(message, **detail)


# ------------------------------------------------------------------ probes
def probe_lists(c):
    k = c.ns + '.lists'
    c.call('lists/clear', {'key': k})
    for n in ('1', '2'):
        c.call('lists/push', {'key': k, 'n': n, 'run': c.run_id})
    for n in ('1', '2'):
        b = c.call('lists/pop', {'key': k})
        rec = b.get('record') or {}
        expect(not b.get('exhausted') and str(rec.get('n')) == n and rec.get('run') == c.run_id,
               'lists/pop returned the wrong record', expected=n, received=rec)
    b = c.call('lists/pop', {'key': k})
    expect(b.get('exhausted') is True, 'lists/pop of an empty list did not report exhausted')
    c.call('lists/clear', {'key': k})
    c.call('lists/vars/set', {'key': k, 'name': 'probe', 'value': c.run_id})
    b = c.call('lists/vars/get', {'key': k, 'name': 'probe'})
    expect(b.get('value') == c.run_id, 'lists/vars/get returned the wrong value',
           received=b.get('value'))
    added = c.call('lists/counters/add', {'key': k, 'name': 'probe'})
    b = c.call('lists/counters/get', {'key': k, 'name': 'probe'})
    expect(b.get('value') == added.get('value'), 'lists/counters/get disagrees with counters/add',
           added=added.get('value'), received=b.get('value'))
    return {'checked': 'push, pop, exhausted pop, clear, vars, counters'}


def probe_lock(c):
    k = c.ns + '.lock'
    a = c.call('lock/acquire', {'key': k, 'wait': 0})
    expect(a.get('locked') is True and a.get('ref'), 'lock/acquire on a free key did not lock')
    try:
        b = c.call('lock/acquire', {'key': k, 'wait': 0})
        expect(b.get('locked') is False, 'a second lock/acquire took a held lock')
    finally:
        r = c.call('lock/release', {'ref': a['ref']})
    expect(r.get('released') is True, 'lock/release did not release', reason=r.get('reason'))
    return {'checked': 'acquire, contention, release'}


def probe_names(c):
    g = GOLDEN['names']
    b = c.call('names/get', {'region': g['region'], 'seed': g['seed']})
    got = {k: b.get(k) for k in ('femaleFullName', 'maleFullName')}
    want = {k: g[k] for k in got}
    expect(got == want, 'names/get with a fixed seed returned a different persona',
           expected=want, received=got)
    return {'checked': 'seeded persona matches the known answer'}


def probe_qr(c):
    g = GOLDEN['qr']
    r = c.call('qr/get', {'text': g['text'], 'format': 'svg'}, as_json=False)
    got = hashlib.sha256(r.body).hexdigest()
    expect(got == g['sha256'], 'qr/get returned a different SVG for a fixed text',
           expected=g['sha256'], received=got)
    return {'checked': 'SVG matches the known answer'}


def probe_totp(c):
    g = GOLDEN['totp']
    for t, code in g['vectors']:
        b = c.call('totp/get', {'secret': g['secret'], 'digits': 8, 'time': t})
        expect(b.get('code') == code, 'totp/get disagrees with the RFC 6238 test vector',
               time=t, expected=code, received=b.get('code'))
    return {'checked': 'RFC 6238 test vectors'}


def probe_webhook(c):
    k = c.ns + '.webhook'
    c.call('webhook/clear', {'key': k})
    payload = json.dumps({'run': c.run_id}).encode('utf-8')
    b = c.call('webhook/receive', {'key': k}, method='POST', body=payload,
               headers={'Content-Type': 'application/json'})
    expect(b.get('received') is True, 'webhook/receive did not store the event')
    b = c.call('webhook/list', {'key': k})
    ev = (b.get('events') or [{}])[0]
    expect(b.get('count', 0) >= 1 and (ev.get('json') or {}).get('run') == c.run_id,
           'webhook/list did not return the event', received=ev.get('body'))
    c.call('webhook/clear', {'key': k})
    return {'checked': 'receive (POST), list, clear'}


def probe_perf(c):
    k = c.ns + '.perf'
    c.call('perf/clear', {'key': k})
    c.call('perf/add', {'key': k, 'value': ','.join(str(n) for n in range(1, 11))})
    b = c.call('perf/stats', {'key': k})
    got = {f: b.get(f) for f in ('count', 'min', 'max', 'sum')}
    want = {'count': 10, 'min': 1, 'max': 10, 'sum': 55}
    expect({f: float(v or 0) for f, v in got.items()} == {f: float(v) for f, v in want.items()},
           'perf/stats of 1..10 is wrong', expected=want, received=got)
    c.call('perf/clear', {'key': k})
    return {'checked': 'add, stats, clear'}


def probe_recon(c):
    """Count matching on duplicate keys, with full values expected and masked ones
    confirmed (suffix=4): 2 vs 2 reconciles by count, 3 vs 2 is short by one with
    three candidates, 1 vs 2 is over by one."""
    k = c.ns + '.recon'
    c.call('recon/clear', {'key': k})
    expected = ['111110001', '222220001', '111110002', '222220002', '333330002', '111110003']
    confirmed = ['xxxxx0001', 'xxxxx0001', 'xxxxx0002', 'xxxxx0002', 'xxxxx0003', 'xxxxx0003']
    for side, values, extra in (('expect', expected, {'suffix': 4}), ('confirm', confirmed, {})):
        b = c.call('recon/' + side, dict({'key': k}, **extra), method='POST',
                   body='\n'.join(values).encode('ascii'), headers={'Content-Type': 'text/plain'})
        expect(b.get('recorded') == len(values), 'recon/%s did not record every value' % side,
               expected=len(values), received=b.get('recorded'))
    b = c.call('recon/list', {'key': k, 'status': 'all'})
    got = {g.get('value'): (g.get('status'), g.get('expected'), g.get('confirmed'),
                            g.get('shortBy') or g.get('overBy'), len(g.get('candidates') or []))
           for g in b.get('groups') or []}
    want = {'0001': ('reconciledByCount', 2, 2, None, 2), '0002': ('short', 3, 2, 1, 3),
            '0003': ('over', 1, 2, 1, 1)}
    expect(got == want, 'recon/list groups are wrong', expected=want, received=got)
    b = c.call('recon/get', {'key': k})
    fields = ('expected', 'confirmed', 'matched', 'outstanding', 'unexpected', 'ambiguousGroups')
    got = {f: b.get(f) for f in fields}
    want = dict(zip(fields, (6, 6, 5, 1, 1, 3)))
    expect(got == want, 'recon/get counts are wrong', expected=want, received=got)
    c.call('recon/clear', {'key': k})
    return {'checked': 'count matching: 2 vs 2, 3 vs 2, 1 vs 2 (suffix=4)'}


def probe_mail(c):
    if not MAIL_DOMAIN:
        raise Fail('no MAIL_DOMAIN configured')
    addr = '%s.mail@%s' % (c.ns, MAIL_DOMAIN)
    marker = 'HEALTH-' + c.run_id
    c.call('mail/clear', {'address': addr})
    sent = time.time()
    c.call('mail/send', {'from': 'health@' + MAIL_DOMAIN, 'to': addr,
                         'subject': 'health check ' + c.run_id, 'body': marker,
                         'attachmentkb': 1, 'filename': 'health.txt',
                         'prepend64': base64.b64encode(marker.encode('ascii')).decode('ascii')})
    wait = int(max(0, min(MAIL_WAIT_S, c.left() - 3)))
    b = c.call('mail/pop', {'address': addr, 'wait': wait})
    if b.get('exhausted'):
        raise Fail('mail/send was accepted but nothing arrived within %d s' % wait,
                   hint='check the SES receipt rule set is active and the MX record points '
                        'at SES inbound for this region')
    expect(c.run_id in (b.get('subject') or ''), 'mail/pop returned another message',
           received=b.get('subject'))
    delivery_ms = int((time.time() - sent) * 1000)
    atts = b.get('attachments') or []
    expect(atts and atts[0].get('ref'), 'the message arrived without its attachment')
    f = c.call('mail/attachments/find', {'ref': atts[0]['ref'], 'text': marker})
    expect(f.get('count', 0) >= 1, 'mail/attachments/find did not find the marker')
    c.call('mail/clear', {'address': addr})
    return {'checked': 'send (SES), receive (MX, receipt rule, S3, parser), pop, attachment find',
            'deliveryMs': delivery_ms}


def probe_sms(c):
    # 0491 570 xxx is the ACMA range reserved for fiction: never a real handset.
    digits = str(int(hashlib.sha256(c.ns.encode()).hexdigest(), 16) % 1000).zfill(3)
    number = '+61491570' + digits
    b = c.call('sms/mocksend', {'number': number, 'body': c.run_id})
    expect(b.get('mocked') is True, 'sms/mocksend did not store the message')
    g = c.call('sms/get', {'number': number, 'since': int(b.get('sentTime') or time.time()) - 1})
    expect(g.get('found') and g.get('body') == c.run_id, 'sms/get did not return the message',
           received=g.get('body'))
    return {'checked': 'mocksend, get (no real text is sent)'}


def probe_voice(c):
    b = c.call('voice/voices')
    expect(b.get('count', 0) > 0, 'voice/voices listed no voices', region=b.get('region'))
    # The same request always returns the same stored audio, so after the first check
    # this reads S3 and costs nothing; Polly is only called if the copy has expired.
    r = c.call('voice/say', {'text': GOLDEN['voice']['text'], 'url': 'true'}, expect=None,
               as_json=False)
    if r.status == 429:
        raise Fail('voice/say is over its daily budget', hint='raise VoiceDailyCharacters, '
                   'or wait for the UTC day to roll over; stored audio still works')
    expect(r.status == 200, 'voice/say returned HTTP %d' % r.status)
    s = r.json()
    expect(s.get('ref') and s.get('url'), 'voice/say returned no audio')
    out = {'checked': 'Polly voices, say (stored audio), listen + find (Transcribe)',
           'cached': bool(s.get('cached')), 'pollyRegion': b.get('region')}
    out.update(_listen(c, s['ref']))
    return out


def _listen(c, ref):
    """Hear the probe's own utterance and find its words in it. The same audio is
    transcribed once, so after the first check (and again only when the stored
    transcript expires, after 30 days) this reads S3 and costs nothing."""
    wait = int(min(15, c.left() - 3))
    if wait < 2:
        raise TimeoutError('no time left to listen')
    r = c.call('voice/find', {'ref': ref, 'text': GOLDEN['voice']['heard'], 'wait': wait},
               expect=None, as_json=False)
    if r.status == 403:
        # VoiceDailyListenSeconds=0: listening is off by choice, which is not a fault.
        return {'listen': 'switched off on this deployment (VoiceDailyListenSeconds=0)'}
    if r.status == 429:
        raise Fail('voice/find (listening) is over its daily budget',
                   hint='raise VoiceDailyListenSeconds, or wait for the UTC day to roll over; '
                        'audio already transcribed still works')
    if r.status == 422:
        raise Fail('Amazon Transcribe could not make words of the probe utterance',
                   detail=r.json().get('detail'))
    expect(r.status == 200, 'voice/find returned HTTP %d' % r.status)
    f = r.json()
    if f.get('pending'):
        raise Fail('the probe utterance is still being transcribed', status='timeout',
                   hint='normal on the first check after a deploy, or once a month when the '
                        'stored transcript expires: the job carries on and the next check '
                        'reads it. If it persists, look at Amazon Transcribe in this region')
    expect(f.get('found'), 'voice/find did not hear the words of the probe utterance',
           expected=GOLDEN['voice']['heard'], received=f.get('text'))
    return {'listen': 'heard: ' + (f.get('text') or '')}


PROBES = {'lists': probe_lists, 'names': probe_names, 'lock': probe_lock, 'mail': probe_mail,
          'sms': probe_sms, 'webhook': probe_webhook, 'totp': probe_totp, 'qr': probe_qr,
          'perf': probe_perf, 'recon': probe_recon, 'voice': probe_voice}


def _probe(name, fn, ctx):
    started = time.time()
    sub = Ctx(ctx.base, ctx.ns, ctx.run_id, ctx.deadline, ctx.trace)
    try:
        out = {'status': 'ok'}
        out.update(fn(sub) or {})
    except Fail as e:
        out = {'status': e.status, 'error': e.message}
        if e.hint:
            out['hint'] = e.hint
        detail = {k: v for k, v in e.detail.items() if v is not None}
        if detail:
            out['detail'] = detail
    except TimeoutError:
        out = {'status': 'timeout', 'error': 'did not finish within %d s' % DEADLINE_S}
    except Exception as e:     # noqa: BLE001 - a probe bug is a failed probe, not a 500
        out = {'status': 'fail', 'error': '%s: %s' % (type(e).__name__, e)}
    out['elapsedMs'] = int((time.time() - started) * 1000)
    out['requests'] = sub.calls
    if sub.builds:
        out['build'] = sorted(sub.builds)[0] if len(sub.builds) == 1 else sorted(sub.builds)
    return name, out


# ------------------------------------------------------------------ build drift
def _functions():
    """{'lists': '<function name>', ...} from HEALTH_FUNCTIONS (key=function,...)."""
    out = {}
    for part in (os.environ.get('HEALTH_FUNCTIONS') or '').split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def _function_builds(fns):
    """{'lists': '20261120.0412', ...} from each function's own configuration, so the
    functions no request reaches directly (lists_worker, mail_receive) are covered too."""
    def one(item):
        name, fn = item
        try:
            cfg = lambda_client().get_function_configuration(FunctionName=fn)
            env = (cfg.get('Environment') or {}).get('Variables') or {}
            return name, env.get('BUILD_ID') or 'dev'
        except ClientError as e:
            return name, 'unknown (%s)' % e.response.get('Error', {}).get('Code', 'error')
        except Exception as e:     # noqa: BLE001
            return name, 'unknown (%s)' % type(e).__name__
    if not fns:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(fns))) as ex:
        return dict(ex.map(one, sorted(fns.items())))


# ------------------------------------------------------------------ state (health table)
def _get(sk):
    try:
        return table().get_item(Key={'PK': 'health', 'SK': sk}).get('Item')
    except ClientError:
        return None


def _namespace():
    """The private probe namespace: random, made once, never returned."""
    it = _get('config')
    if it and it.get('namespace'):
        return it['namespace']
    ns = 'health-' + secrets.token_hex(6)
    try:
        table().put_item(Item={'PK': 'health', 'SK': 'config', 'namespace': ns,
                               'createdTime': int(time.time())},
                         ConditionExpression='attribute_not_exists(PK)')
        return ns
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
            raise
        return _get('config')['namespace']


def _take_lease(now):
    try:
        table().put_item(Item={'PK': 'health', 'SK': 'lease', 'until': now + LEASE_S,
                               'ttl': now + 3600},
                         ConditionExpression='attribute_not_exists(PK) OR #u < :now',
                         ExpressionAttributeNames={'#u': 'until'},
                         ExpressionAttributeValues={':now': now})
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return False
        raise


def _drop_lease():
    try:
        table().delete_item(Key={'PK': 'health', 'SK': 'lease'})
    except ClientError:
        pass


def _cached():
    it = _get('result')
    if not it or not it.get('result'):
        return None
    try:
        return json.loads(it['result'])
    except ValueError:
        return None


def _store(result):
    table().put_item(Item={'PK': 'health', 'SK': 'result', 'result': json.dumps(result),
                           'checkedTime': result['checkedTime'],
                           'ttl': result['checkedTime'] + RESULT_TTL_S})


# ------------------------------------------------------------------ the check
def overall(services, mismatch):
    states = {n: s['status'] for n, s in services.items()}
    if not any(v == 'ok' for v in states.values()) or all(states.get(n) != 'ok' for n in CORE):
        return 'down'
    if any(v != 'ok' for v in states.values()) or mismatch:
        return 'degraded'
    return 'ok'


def run_check(base, trace=''):
    started = time.time()
    me = build_info()
    traced = trace_info(trace, base)
    # Pass the header on only when this request is being traced: an unsampled
    # header would tell every probe's API Gateway stage not to trace either.
    ctx = Ctx(base, _namespace(), secrets.token_hex(4), started + DEADLINE_S,
              trace if traced['traceSampled'] else '')
    ex = ThreadPoolExecutor(max_workers=len(PROBES) + 1)
    futures = {ex.submit(_probe, n, fn, ctx): n for n, fn in PROBES.items()}
    fn_future = ex.submit(_function_builds, _functions())
    done, _ = wait_futures(list(futures) + [fn_future], timeout=DEADLINE_S + 2)
    services = {}
    for f, name in futures.items():
        services[name] = f.result()[1] if f in done else {
            'status': 'timeout', 'error': 'did not finish within %d s' % DEADLINE_S}
    functions = fn_future.result() if fn_future in done else {}
    ex.shutdown(wait=False, cancel_futures=True)

    mismatch = [{'function': n, 'build': b} for n, b in sorted(functions.items())
                if b != me['build']]
    for name, s in sorted(services.items()):
        seen = s.get('build')
        for b in ([seen] if isinstance(seen, str) else (seen or [])):
            if b != me['build']:
                mismatch.append({'service': name, 'build': b})

    passed = sum(1 for s in services.values() if s['status'] == 'ok')
    result = dict(me)
    result.update({
        'status': overall(services, mismatch),
        'region': os.environ.get('AWS_REGION') or None,
        'checkedTime': int(started),
        'elapsedMs': int((time.time() - started) * 1000),
        'summary': '%d of %d probes passed%s' % (
            passed, len(services), '; %d build mismatch(es)' % len(mismatch) if mismatch else ''),
        'services': services,
        'functionsChecked': len(functions),
        'buildMismatch': mismatch,
        # The trace of the run that produced this result (kept with the cached copy).
        'checkTraceId': traced['traceId'],
        'checkTraceUrl': traced['traceUrl'],
        'checkTraceDataUrl': traced['traceDataUrl'],
    })
    return result


def _present(result, cached, trace, base, comment=None):
    out = dict(result)
    out.update(trace_info(trace, base))     # this request's own trace
    out['cached'] = cached
    out['ageSeconds'] = max(0, int(time.time()) - int(result.get('checkedTime', 0)))
    if comment:
        out['comment'] = comment
    return out


def do_check(req):
    fresh = req.boolean('fresh', False)
    strict = req.boolean('strict', False)

    def answer(out):
        # strict: the status code says it, for uptime monitors; otherwise 200 and
        # the body says it (CONVENTIONS 5: asked to be refused rather than degraded).
        return respond(503 if strict and out.get('status') != 'ok' else 200, out)

    trace = trace_header(req)
    base = base_url(req.event)
    now = int(time.time())
    cached = _cached()
    age = now - int(cached['checkedTime']) if cached else None
    if cached and age < FRESH_FLOOR_S:
        return answer(_present(cached, True, trace, base, (
            'fresh=true refused: the last check is under %d s old' % FRESH_FLOOR_S)
            if fresh else None))
    if cached and age < CACHE_SECONDS and not fresh:
        return answer(_present(cached, True, trace, base))

    if not _take_lease(now):
        # Another caller's check is running: wait for its result, do not start a second.
        until = time.time() + DEADLINE_S + 3
        while time.time() < until:
            time.sleep(0.5)
            latest = _cached()
            if latest and int(latest['checkedTime']) >= now - LEASE_S:
                return answer(_present(latest, True, trace, base, 'another caller started this check'))
        if cached:
            return answer(_present(cached, True, trace, base, 'a check is running; this is the last one'))
        raise ApiError(503, 'A health check is already running; call again in a few seconds.',
                       retryAfterSeconds=5)
    try:
        result = run_check(base, trace)
        _store(result)
    finally:
        _drop_lease()
    return answer(_present(result, False, trace, base))


def do_get(req):
    out = build_info()
    out.update({
        'region': os.environ.get('AWS_REGION') or None,
        'baseUrl': base_url(req.event),
        'mailDomain': MAIL_DOMAIN or None,
        'services': SERVICES,
        'checkUrl': base_url(req.event) + '/health/check',
        'lastCheck': None,
    })
    out.update(trace_info(trace_header(req), base_url(req.event)))
    last = _cached()
    if last:
        out['lastCheck'] = {
            'status': last.get('status'), 'summary': last.get('summary'),
            'build': last.get('build'), 'checkedTime': last.get('checkedTime'),
            'traceUrl': last.get('checkTraceUrl'),
            'ageSeconds': max(0, int(time.time()) - int(last.get('checkedTime', 0)))}
    return respond(200, out)


ROUTES = {'get': do_get, 'check': do_check}


def handler(event, context):
    return run(event, 'health', ROUTES, index={
        'description': 'Which build is deployed (health/get), and an end-to-end self-test that '
                       'sends a real request with a known answer to every service (health/check).'})
