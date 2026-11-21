"""Tests for the build stamp (X-Build, version.json, the /v1 index) and the health
service: health/get, and health/check end to end with its probes answered by the
real service code where it is Python (lock, names, qr, totp, webhook, perf, recon, under
moto) and by small fakes for the rest (lists is Node; mail, sms, voice need SES,
SNS and Polly).
"""
import hashlib
import json
import os
import sys
import time
import urllib.parse

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.update({'AWS_DEFAULT_REGION': 'ap-southeast-2', 'AWS_ACCESS_KEY_ID': 'testing',
                   'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_SESSION_TOKEN': 'testing',
                   'BASE_URL': 'https://api.example.com/v1'})
for k, v in (('LOCK_TABLE', 'test-lock'), ('PERF_TABLE', 'test-perf'),
             ('HEALTH_TABLE', 'test-health'), ('MAIL_DOMAIN', 'example.com')):
    os.environ.setdefault(k, v)
# webhook and totp read their table name once, at import. If this file is the first
# to import them, they must get the names tests/test_webhook_totp.py expects.
os.environ.update({'WEBHOOK_TABLE': 'test-webhook', 'TOTP_TABLE': 'test-totp'})

import common     # noqa: E402
import discovery  # noqa: E402
import health     # noqa: E402
import lock       # noqa: E402
import names      # noqa: E402
import perf       # noqa: E402
import qr         # noqa: E402
import recon      # noqa: E402
import totp       # noqa: E402
import webhook    # noqa: E402

BUILD = '20261120.0412'
ROOT = '1-673d9a1b-5f2a8c3e9d1b4a6f7e8c9d0a'
TRACE = 'Root=%s;Parent=53995c3f42cd8ad8;Sampled=1' % ROOT
TRACE_URL = 'https://api.example.com/v1/trace/get?ref=%s&format=html' % ROOT
TRACE_DATA = 'https://api.example.com/v1/trace/get?ref=%s' % ROOT
CONSOLE_URL = 'https://console.aws.amazon.com/xray/home?region=us-west-1#/traces/' + ROOT


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    monkeypatch.setenv('BUILD_ID', BUILD)
    monkeypatch.setenv('BUILD_COMMIT', 'a3f9c2e')
    monkeypatch.setenv('DEPLOYED_TIME', '1795672320')
    monkeypatch.setenv('AWS_REGION', 'us-west-1')
    monkeypatch.setenv('_X_AMZN_TRACE_ID', TRACE)
    monkeypatch.setattr(common, '_build', None)
    # Other test files set these first (some to 'unused'): pin this file's own.
    monkeypatch.setattr(webhook, 'TABLE', 'health-test-webhook')
    monkeypatch.setattr(totp, 'TABLE', 'health-test-totp')
    monkeypatch.setattr(health, 'TABLE', 'health-test-health')
    monkeypatch.setattr(health, 'MAIL_DOMAIN', 'example.com')
    monkeypatch.setenv('LOCK_TABLE', 'health-test-lock')
    monkeypatch.setenv('PERF_TABLE', 'health-test-perf')
    monkeypatch.setenv('RECON_TABLE', 'health-test-recon')
    # moto's updates are not atomic across threads (DynamoDB's are): the recon probe's
    # batches write to the same groups, so write them one at a time here.
    monkeypatch.setattr(recon, 'WRITERS', 1)
    with mock_aws():
        for mod, attr in ((webhook, '_table'), (totp, '_table'), (health, '_table'),
                          (health, '_lambda'), (lock, '_ddb'), (perf, '_tbl'), (perf, '_res')):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, None)
        c = boto3.client('dynamodb')

        def table(name, hk, sk=None):
            attrs = [{'AttributeName': hk, 'AttributeType': 'S'}]
            keys = [{'AttributeName': hk, 'KeyType': 'HASH'}]
            if sk:
                attrs.append({'AttributeName': sk, 'AttributeType': 'S'})
                keys.append({'AttributeName': sk, 'KeyType': 'RANGE'})
            c.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                           AttributeDefinitions=attrs, KeySchema=keys)
        table(webhook.TABLE, 'key', 'seq')
        table(totp.TABLE, 'key', 'user')
        table(os.environ['LOCK_TABLE'], 'LockKey')
        table(os.environ['PERF_TABLE'], 'PK', 'SK')
        recon._reset()
        c.create_table(
            TableName=os.environ['RECON_TABLE'], BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': n, 'AttributeType': t}
                                  for n, t in (('PK', 'S'), ('SK', 'S'), ('gk', 'S'), ('gt', 'N'))],
            KeySchema=[{'AttributeName': 'PK', 'KeyType': 'HASH'},
                       {'AttributeName': 'SK', 'KeyType': 'RANGE'}],
            GlobalSecondaryIndexes=[{'IndexName': 'ByState', 'Projection': {'ProjectionType': 'ALL'},
                                     'KeySchema': [{'AttributeName': 'gk', 'KeyType': 'HASH'},
                                                   {'AttributeName': 'gt', 'KeyType': 'RANGE'}]}])
        table(health.TABLE, 'PK', 'SK')
        yield
    monkeypatch.setattr(common, '_build', None)


def event(service, action, qs=None, method='GET', body=None, headers=None):
    return {'httpMethod': method, 'resource': f'/{service}/{{proxy+}}',
            'path': f'/v1/{service}/{action}',
            'pathParameters': {'proxy': action} if action else None,
            'queryStringParameters': qs, 'headers': {'Host': 'api.example.com', **(headers or {})},
            'body': body, 'isBase64Encoded': False,
            'requestContext': {'identity': {'sourceIp': '203.0.113.10'}}}


def call(mod, service, action, qs=None, **kw):
    res = mod.handler(event(service, action, qs, **kw), None)
    return res['statusCode'], json.loads(res['body']), res['headers']


# ------------------------------------------------------------------ fakes for the API
class FakeApi:
    """Answers health's HTTP calls: Python services in-process, the rest faked."""

    IN_PROCESS = {'lock': lock, 'names': names, 'qr': qr, 'totp': totp, 'webhook': webhook,
                  'perf': perf, 'recon': recon}

    def __init__(self):
        self.lists, self.vars, self.counters, self.mail, self.sms = {}, {}, {}, {}, {}
        self.build = {}          # service -> X-Build to answer with (default: BUILD)
        self.broken = {}         # service -> callable(action, qs) returning (status, body)
        self.delay = {}          # service -> seconds to sleep before answering
        self.log = []
        self.traces = []         # the X-Amzn-Trace-Id each probe request carried

    def __call__(self, method, url, body=None, headers=None, timeout=10):
        u = urllib.parse.urlsplit(url)
        assert u.netloc == 'api.example.com' and u.path.startswith('/v1/'), url
        service, action = u.path[len('/v1/'):].split('/', 1)
        qs = dict(urllib.parse.parse_qsl(u.query))
        self.log.append((service, action))
        self.traces.append((headers or {}).get('X-Amzn-Trace-Id'))
        if service in self.delay:
            time.sleep(min(self.delay[service], timeout))
            if self.delay[service] >= timeout:
                raise TimeoutError('timed out')
        if service in self.broken:
            status, out = self.broken[service](action, qs)
        elif service in self.IN_PROCESS:
            res = self.IN_PROCESS[service].handler(
                event(service, action, qs, method=method,
                      body=body.decode('utf-8') if body else None, headers=headers), None)
            hdrs = dict(res['headers'])
            if service in self.build:
                hdrs['X-Build'] = self.build[service]
            return health.Resp(res['statusCode'], hdrs, res['body'])
        else:
            status, out = getattr(self, 'fake_' + service)(action, qs)
        hdrs = {'Content-Type': 'application/json', 'X-Build': self.build.get(service, BUILD)}
        return health.Resp(status, hdrs, json.dumps(out))

    def fake_lists(self, action, qs):
        k = qs['key']
        if action == 'clear':
            self.lists[k] = []
            return 200, {'key': k, 'deleted': 0}
        if action == 'push':
            rec = {f: v for f, v in qs.items() if f != 'key'}
            self.lists.setdefault(k, []).append(rec)
            return 200, {'key': k, 'index': len(self.lists[k])}
        if action == 'pop':
            q = self.lists.get(k) or []
            if not q:
                return 200, {'key': k, 'exhausted': True, 'record': None}
            return 200, {'key': k, 'exhausted': False, 'record': q.pop(0)}
        if action == 'vars/set':
            self.vars[(k, qs['name'])] = qs['value']
            return 200, {'key': k, 'name': qs['name'], 'value': qs['value']}
        if action == 'vars/get':
            return 200, {'key': k, 'name': qs['name'], 'value': self.vars.get((k, qs['name']))}
        if action == 'counters/add':
            self.counters[k] = self.counters.get(k, 0) + 1
            return 200, {'key': k, 'value': self.counters[k]}
        if action == 'counters/get':
            return 200, {'key': k, 'value': self.counters.get(k, 0)}
        raise AssertionError(action)

    def fake_mail(self, action, qs):
        if action == 'clear':
            self.mail.pop(qs['address'], None)
            return 200, {'address': qs['address'], 'deleted': 0}
        if action == 'send':
            assert qs['from'].endswith('@example.com') and qs['to'].endswith('@example.com')
            marker = __import__('base64').b64decode(qs['prepend64']).decode()
            self.mail[qs['to']] = {'subject': qs['subject'], 'body': qs['body'], 'marker': marker}
            return 200, {'result': 'sent'}
        if action == 'pop':
            m = self.mail.pop(qs['address'], None)
            if not m:
                return 200, {'address': qs['address'], 'exhausted': True}
            self.mail['ref:r1'] = m
            return 200, {'address': qs['address'], 'exhausted': False, 'subject': m['subject'],
                         'body': m['body'], 'attachments': [{'filename': 'health.txt', 'ref': 'r1'}]}
        if action == 'attachments/find':
            m = self.mail.get('ref:' + qs['ref']) or {}
            return 200, {'ref': qs['ref'], 'count': int(m.get('marker') == qs['text'])}
        raise AssertionError(action)

    def fake_sms(self, action, qs):
        assert qs['number'].startswith('+61491570')
        if action == 'mocksend':
            self.sms[qs['number']] = qs['body']
            return 200, {'mocked': True, 'sentTime': int(time.time())}
        if action == 'get':
            b = self.sms.get(qs['number'])
            return 200, {'found': b is not None, 'body': b}
        raise AssertionError(action)

    def fake_voice(self, action, qs):
        if action == 'voices':
            return 200, {'region': 'us-west-2', 'count': 3}
        if action == 'say':
            assert qs['text'] == health.GOLDEN['voice']['text']
            return 200, {'ref': 'abc', 'cached': True, 'url': 'https://s3.example/abc'}
        if action == 'find':
            assert qs['ref'] == 'abc' and qs['text'] == 'health check' and int(qs['wait']) >= 2
            return 200, {'ref': 'h123', 'pending': False, 'found': True, 'count': 1,
                         'text': 'Health check.'}
        raise AssertionError(action)


class FakeLambda:
    def __init__(self, builds):
        self.builds = builds

    def get_function_configuration(self, FunctionName):
        return {'Environment': {'Variables': {'BUILD_ID': self.builds.get(FunctionName, BUILD)}}}


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(health, 'http', fake)
    monkeypatch.setenv('HEALTH_FUNCTIONS', 'lists=fn-lists,mail_receive=fn-mail-receive')
    monkeypatch.setattr(health, 'lambda_client', lambda: FakeLambda({}))
    return fake


def check(qs=None):
    return call(health, 'health', 'check', qs)


# ------------------------------------------------------------------ build stamp
def test_build_info_from_environment():
    b = common.build_info()
    version = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'src', 'version.json')))
    assert b == {'version': version['version'], 'build': BUILD, 'commit': 'a3f9c2e',
                 'deployedTime': 1795672320}


def test_build_defaults_when_unstamped(monkeypatch):
    for k in ('BUILD_ID', 'BUILD_COMMIT', 'DEPLOYED_TIME'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(common, '_build', None)
    b = common.build_info()
    assert b['build'] == 'dev' and b['commit'] is None and b['deployedTime'] is None


def test_every_response_carries_x_build():
    assert common.respond(200, {})['headers']['X-Build'] == BUILD
    assert common.error(400, 'x')['headers']['X-Build'] == BUILD
    assert common.raw(200, '', 'text/plain')['headers']['X-Build'] == BUILD
    _s, _b, h = call(totp, 'totp', 'get', {'secret': health.GOLDEN['totp']['secret']})
    assert h['X-Build'] == BUILD


def test_openapi_version_is_version_json():
    spec = json.loads(discovery.openapi_text())
    version = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'src', 'version.json')))
    assert spec['info']['version'] == version['version']


def test_v1_index_reports_build():
    res = discovery.handler({'httpMethod': 'GET', 'pathParameters': None, 'headers': {}}, None)
    body = json.loads(res['body'])
    assert body['build']['build'] == BUILD and res['headers']['X-Build'] == BUILD
    assert 'health' in [s['name'] for s in body['services']]
    assert body['docs']['health'].endswith('/health/check')


# ------------------------------------------------------------------ totp time=
def test_totp_time_gives_rfc_vectors():
    g = health.GOLDEN['totp']
    for t, code in g['vectors']:
        s, b, _ = call(totp, 'totp', 'get', {'secret': g['secret'], 'digits': '8', 'time': str(t)})
        assert s == 200 and b['code'] == code and b['atTime'] == t
    s, b, _ = call(totp, 'totp', 'get', {'secret': g['secret'], 'time': '59'})
    assert b['secondsRemaining'] == 1 and b['previousCode'] == totp.totp(g['secret'], 29)


def test_totp_time_with_stored_secret_and_errors():
    g = health.GOLDEN['totp']
    call(totp, 'totp', 'set', {'key': 'k', 'user': 'u', 'secret': g['secret'], 'digits': '8'})
    s, b, _ = call(totp, 'totp', 'get', {'key': 'k', 'user': 'u', 'time': '1111111109'})
    assert s == 200 and b['code'] == '07081804' and b['atTime'] == 1111111109
    s, b, _ = call(totp, 'totp', 'get', {'key': 'k', 'user': 'u'})
    assert 'atTime' not in b
    assert call(totp, 'totp', 'get', {'secret': g['secret'], 'time': 'soon'})[0] == 400
    assert call(totp, 'totp', 'get', {'secret': g['secret'], 'time': '-1'})[0] == 400


# ------------------------------------------------------------------ known answers
def test_golden_values_match_the_service_code():
    """If this fails, names or qr output changed: update health.GOLDEN to match."""
    g = health.GOLDEN['names']
    _s, b, _ = call(names, 'names', 'get', {'region': g['region'], 'seed': g['seed']})
    assert (b['femaleFullName'], b['maleFullName']) == (g['femaleFullName'], g['maleFullName'])
    res = qr.handler(event('qr', 'get', {'text': health.GOLDEN['qr']['text'], 'format': 'svg'}), None)
    assert hashlib.sha256(res['body'].encode('utf-8')).hexdigest() == health.GOLDEN['qr']['sha256']
    for t, code in health.GOLDEN['totp']['vectors']:
        assert totp.totp(health.GOLDEN['totp']['secret'], t, digits=8) == code


# ------------------------------------------------------------------ health/get
def test_get_reports_build_and_no_check_yet(api):
    s, b, h = call(health, 'health', 'get')
    assert s == 200 and h['X-Build'] == BUILD
    assert b['build'] == BUILD and b['commit'] == 'a3f9c2e' and b['lastCheck'] is None
    assert b['checkUrl'] == 'https://api.example.com/v1/health/check' and 'health' in b['services']
    assert api.log == []          # get calls nothing


# ------------------------------------------------------------------ health/check
def test_check_all_ok_then_cached(api):
    s, b, _ = check()
    assert s == 200, b
    assert b['status'] == 'ok', json.dumps(b['services'], indent=1)
    assert set(b['services']) == set(health.PROBES)
    assert all(p['status'] == 'ok' and p['build'] == BUILD for p in b['services'].values())
    assert b['cached'] is False and b['buildMismatch'] == [] and b['functionsChecked'] == 2
    assert b['summary'] == '11 of 11 probes passed' and b['build'] == BUILD
    assert 'namespace' not in json.dumps(b) and 'health-' not in json.dumps(b)
    # The probes leave nothing behind in the lists or mail fakes.
    assert all(not v for v in api.lists.values())
    assert not [a for a in api.mail if '@' in a]

    n = len(api.log)
    s, b2, _ = check()
    assert b2['cached'] is True and b2['checkedTime'] == b['checkedTime'] and len(api.log) == n
    s, b3, _ = check({'fresh': 'true'})
    assert b3['cached'] is True and 'refused' in b3['comment'] and len(api.log) == n
    s, g, _ = call(health, 'health', 'get')
    assert g['lastCheck']['status'] == 'ok' and g['lastCheck']['checkedTime'] == b['checkedTime']


def test_fresh_reruns_after_the_floor_and_cache_expires(api, monkeypatch):
    check()
    n = len(api.log)
    real = time.time
    monkeypatch.setattr(health.time, 'time', lambda: real() + health.FRESH_FLOOR_S + 1)
    _s, b, _ = check()
    assert b['cached'] is True and len(api.log) == n          # still inside CACHE_SECONDS
    _s, b, _ = check({'fresh': '1'})
    assert b['cached'] is False and len(api.log) > n
    monkeypatch.setattr(health.time, 'time', lambda: real() + 3 * health.CACHE_SECONDS)
    n = len(api.log)
    _s, b, _ = check()
    assert b['cached'] is False and len(api.log) > n


def test_wrong_answer_is_degraded_and_strict_is_503(api):
    api.broken['qr'] = lambda action, qs: (200, {'svg': 'not the right picture'})
    s, b, _ = check()
    assert s == 200 and b['status'] == 'degraded'
    q = b['services']['qr']
    assert q['status'] == 'fail' and 'different SVG' in q['error']
    assert q['detail']['expected'] == health.GOLDEN['qr']['sha256']
    s, b, _ = check({'strict': 'true'})
    assert s == 503 and b['status'] == 'degraded' and b['cached'] is True


def test_http_error_is_reported_with_its_message(api):
    api.broken['perf'] = lambda action, qs: (500, {'error': 'boom', 'status': 500})
    api.broken['sms'] = lambda action, qs: (429, {'error': 'Too Many Requests',
                                                  'gatewayError': 'THROTTLED'})
    _s, b, _ = check()
    assert b['services']['perf']['status'] == 'fail'
    assert b['services']['perf']['detail']['error'] == 'boom'
    assert 'ThrottleRateLimit' in b['services']['sms']['hint']


def test_core_failure_is_down(api):
    for svc in health.CORE:
        api.broken[svc] = lambda action, qs: (502, {'error': 'Internal server error',
                                                    'gatewayError': 'INTEGRATION_FAILURE'})
    _s, b, _ = check()
    assert b['status'] == 'down'
    s, _b, _ = check({'strict': 'yes'})
    assert s == 503


def test_mail_that_never_arrives_has_a_hint(api):
    api.fake_mail_orig = api.fake_mail

    def no_delivery(action, qs):
        if action == 'pop':
            return 200, {'address': qs['address'], 'exhausted': True}
        return api.fake_mail_orig(action, qs)
    api.broken['mail'] = no_delivery
    _s, b, _ = check()
    m = b['services']['mail']
    assert m['status'] == 'fail' and 'receipt rule' in m['hint']
    assert b['status'] == 'degraded'


def test_voice_budget_exhausted(api):
    def budget(action, qs):
        if action == 'say':
            return 429, {'error': 'over budget', 'status': 429, 'budgetExhausted': True}
        return api.fake_voice(action, qs)
    api.broken['voice'] = budget
    _s, b, _ = check()
    assert b['services']['voice']['status'] == 'fail'
    assert 'VoiceDailyCharacters' in b['services']['voice']['hint']


def test_build_mismatch_from_function_config_and_headers(api, monkeypatch):
    monkeypatch.setattr(health, 'lambda_client',
                        lambda: FakeLambda({'fn-mail-receive': '20261101.0900'}))
    api.build['names'] = '20261101.0900'
    _s, b, _ = check()
    assert b['status'] == 'degraded'
    assert {'function': 'mail_receive', 'build': '20261101.0900'} in b['buildMismatch']
    assert {'service': 'names', 'build': '20261101.0900'} in b['buildMismatch']
    assert all(p['status'] == 'ok' for p in b['services'].values())
    assert '2 build mismatch' in b['summary']


def test_slow_probe_times_out_without_holding_the_rest(api, monkeypatch):
    monkeypatch.setattr(health, 'DEADLINE_S', 2)
    api.delay['voice'] = 5
    started = time.time()
    _s, b, _ = check()
    assert time.time() - started < 6
    assert b['services']['voice']['status'] == 'timeout'
    assert b['services']['lists']['status'] == 'ok' and b['status'] == 'degraded'


def test_a_running_check_is_not_started_twice(api):
    now = int(time.time())
    health.table().put_item(Item={'PK': 'health', 'SK': 'lease', 'until': now + 60})
    health._store({'status': 'ok', 'checkedTime': now - 120, 'summary': 'old', 'build': BUILD,
                   'version': '1.1.0', 'services': {}, 'buildMismatch': []})
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(b=check()[1]))
    original = health.DEADLINE_S
    try:
        health.DEADLINE_S = 0            # the waiting caller gives up fast
        t.start()
        t.join(10)
    finally:
        health.DEADLINE_S = original
    assert out['b']['cached'] is True and out['b']['summary'] == 'old'
    assert 'running' in out['b']['comment'] and api.log == []


def test_namespace_is_stable_and_private(api):
    check()
    ns = health._get('config')['namespace']
    assert ns.startswith('health-') and len(ns) == 19
    assert health._namespace() == ns
    keys = {a for s, a in api.log}
    assert keys       # probes ran
    assert all(k.startswith(ns) for k in api.lists)


def test_malformed_flag_is_400(api):
    assert check({'fresh': 'maybe'})[0] == 400
    assert check({'strict': 'maybe'})[0] == 400
    assert api.log == []


def test_service_index_and_unknown_action():
    s, b, _ = call(health, 'health', '')
    assert s == 200 and b['actions'] == ['check', 'get']
    s, b, _ = call(health, 'health', 'nope')
    assert s == 404 and 'check' in b['actions']


def _voice_with(api, find):
    def answer(action, qs):
        return find(qs) if action == 'find' else api.fake_voice(action, qs)
    api.broken['voice'] = answer


def test_listen_is_probed(api):
    _s, b, _ = check()
    v = b['services']['voice']
    assert v['status'] == 'ok' and v['listen'] == 'heard: Health check.'
    assert ('voice', 'find') in api.log


def test_listen_still_transcribing_is_a_timeout_with_a_hint(api):
    _voice_with(api, lambda qs: (200, {'ref': 'h123', 'pending': True, 'found': None}))
    _s, b, _ = check()
    v = b['services']['voice']
    assert v['status'] == 'timeout' and 'first check after a deploy' in v['hint']
    assert b['status'] == 'degraded'


def test_listen_wrong_words_fails(api):
    _voice_with(api, lambda qs: (200, {'ref': 'h123', 'pending': False, 'found': False,
                                       'count': 0, 'text': 'Wealth deck.'}))
    _s, b, _ = check()
    v = b['services']['voice']
    assert v['status'] == 'fail' and v['detail']['received'] == 'Wealth deck.'


def test_listen_switched_off_is_not_a_fault(api):
    _voice_with(api, lambda qs: (403, {'error': 'Listening is switched off', 'status': 403,
                                       'listeningDisabled': True}))
    _s, b, _ = check()
    v = b['services']['voice']
    assert v['status'] == 'ok' and 'switched off' in v['listen'] and b['status'] == 'ok'


def test_listen_budget_exhausted(api):
    _voice_with(api, lambda qs: (429, {'error': 'budget', 'status': 429, 'budgetExhausted': True}))
    _s, b, _ = check()
    assert 'VoiceDailyListenSeconds' in b['services']['voice']['hint']


# ------------------------------------------------------------------ X-Ray trace URL
def test_get_returns_its_trace_url(api):
    _s, b, _ = call(health, 'health', 'get')
    assert b['traceId'] == ROOT and b['traceSampled'] is True and b['traceUrl'] == TRACE_URL
    assert b['traceDataUrl'] == TRACE_DATA and b['traceConsoleUrl'] == CONSOLE_URL
    assert 'traceComment' not in b


def test_check_returns_its_trace_and_probes_join_it(api):
    _s, b, _ = check()
    assert b['traceUrl'] == TRACE_URL and b['checkTraceUrl'] == TRACE_URL
    assert b['checkTraceDataUrl'] == TRACE_DATA
    assert api.traces and all(t == TRACE for t in api.traces)


def test_cached_check_has_its_own_trace_and_the_runs(api, monkeypatch):
    check()
    other = '1-673d9a2c-000000000000000000000001'
    monkeypatch.setenv('_X_AMZN_TRACE_ID', 'Root=%s;Parent=1;Sampled=1' % other)
    _s, b, _ = check()
    assert b['cached'] is True and b['traceId'] == other and b['checkTraceUrl'] == TRACE_URL
    _s, g, _ = call(health, 'health', 'get')
    assert g['traceId'] == other and g['lastCheck']['traceUrl'] == TRACE_URL


def test_untraced_request_has_no_url_and_does_not_pass_the_header_on(api, monkeypatch):
    monkeypatch.setenv('_X_AMZN_TRACE_ID', 'Root=%s;Parent=53995c3f42cd8ad8;Sampled=0' % ROOT)
    _s, b, _ = check()
    assert b['traceId'] == ROOT and b['traceSampled'] is False and b['traceUrl'] is None
    assert 'Tracing=false' in b['traceComment'] and b['checkTraceUrl'] is None
    assert b['traceDataUrl'] is None and b['traceConsoleUrl'] is None
    assert api.traces and all(t is None for t in api.traces)


def test_no_trace_header_at_all(api, monkeypatch):
    monkeypatch.delenv('_X_AMZN_TRACE_ID')
    _s, b, _ = call(health, 'health', 'get')
    assert b['traceId'] is None and b['traceUrl'] is None
    res = health.handler(event('health', 'get', headers={'X-Amzn-Trace-Id': TRACE}), None)
    assert json.loads(res['body'])['traceUrl'] == TRACE_URL
