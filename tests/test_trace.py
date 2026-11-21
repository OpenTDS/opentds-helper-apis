"""Tests for trace/get: the public, cleaned view of an X-Ray trace of this deployment."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.update({'AWS_DEFAULT_REGION': 'us-west-1', 'AWS_ACCESS_KEY_ID': 'testing',
                   'AWS_SECRET_ACCESS_KEY': 'testing', 'BASE_URL': 'https://api.example.com/v1'})

import trace_view as trace_mod  # noqa: E402



NOW = int(time.time())
REF = '1-%08x-5f2a8c3e9d1b4a6f7e8c9d0a' % (NOW - 60)
SECRET_NS = 'health-0123456789ab'


def docs(t0=None):
    """A health/check trace as X-Ray stores it: the check's API Gateway and function
    segments, one probe request made from inside the function, and a segment of
    another application that called in (which must not be shown)."""
    t0 = t0 or NOW - 60.0
    return [
        {'id': 'a1', 'name': 'testhelper-v1-v1/live', 'origin': 'AWS::ApiGateway::Stage',
         'start_time': t0, 'end_time': t0 + 6.1, 'trace_id': REF,
         'http': {'request': {'method': 'GET', 'url': 'https://abc123.execute-api.us-west-1.amazonaws.com/live/health/check?fresh=true',
                              'client_ip': '203.0.113.9', 'user_agent': 'curl/8'},
                  'response': {'status': 200}},
         'aws': {'account_id': '123456789012', 'api_id': 'abc123', 'request_id': 'r-1'},
         'subsegments': [{'id': 'a1s', 'name': 'Lambda', 'namespace': 'aws', 'start_time': t0 + 0.01,
                          'end_time': t0 + 6.09, 'aws': {'operation': 'Invoke',
                                                         'function_name': 'arn:aws:lambda:us-west-1:123456789012:function:x'}}]},
        {'id': 'l1', 'name': 'testhelper-v1-HealthFunction-AbC', 'origin': 'AWS::Lambda', 'parent_id': 'a1s',
         'start_time': t0 + 0.02, 'end_time': t0 + 6.08,
         'aws': {'request_id': 'r-2'}},
        {'id': 'f1', 'name': 'testhelper-v1-HealthFunction-AbC', 'origin': 'AWS::Lambda::Function', 'parent_id': 'l1',
         'start_time': t0 + 0.03, 'end_time': t0 + 6.07,
         'aws': {'function_arn': 'arn:aws:lambda:us-west-1:123456789012:function:testhelper-v1-HealthFunction-AbC'},
         'subsegments': [{'id': 'f1i', 'name': 'Initialization', 'start_time': t0 - 0.4, 'end_time': t0 + 0.02},
                         {'id': 'f1v', 'name': 'Invocation', 'start_time': t0 + 0.03, 'end_time': t0 + 6.05},
                         {'id': 'f1o', 'name': 'Overhead', 'start_time': t0 + 6.05, 'end_time': t0 + 6.07}]},
        # A probe request the health function made, carrying its trace header.
        {'id': 'a2', 'name': 'testhelper-v1-v1/live', 'origin': 'AWS::ApiGateway::Stage', 'parent_id': 'f1',
         'start_time': t0 + 0.5, 'end_time': t0 + 0.9, 'fault': True,
         'http': {'request': {'method': 'GET', 'url': 'https://api.example.com/v1/lists/pop?key=%s.lists' % SECRET_NS},
                  'response': {'status': 502}}},
        {'id': 'x1', 'name': 'mc-v1-web', 'origin': 'AWS::Lambda::Function', 'start_time': t0 - 1,
         'end_time': t0 + 7},
    ]


class FakeXRay:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def batch_get_traces(self, TraceIds):
        self.calls += 1
        d = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if d is None:
            return {'Traces': [], 'UnprocessedTraceIds': []}
        return {'Traces': [{'Id': TraceIds[0], 'Duration': 6.1,
                            'Segments': [{'Id': x['id'], 'Document': json.dumps(x)} for x in d]}]}


class _ClockFromNow:
    """trace_view's time module, with time.time() counted from this file's NOW.

    The trace ids above are built from NOW when the file is collected. Run after
    a slow module (test_health takes ~50 s), real time would already have aged a
    '5 s old' trace past SETTLE_S. Elapsed real time still passes, so waits work.
    """
    def __init__(self):
        self._started = time.time()

    def time(self):
        return NOW + (time.time() - self._started)

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def xr(monkeypatch):
    monkeypatch.setattr(trace_mod, 'PREFIX', 'testhelper-v1-')
    monkeypatch.setattr(trace_mod, 'POLL_S', 0.01)
    monkeypatch.setattr(trace_mod, 'time', _ClockFromNow())

    def use(*answers):
        fake = FakeXRay(answers)
        monkeypatch.setattr(trace_mod, 'xray', lambda: fake)
        return fake
    return use


def call(qs):
    ev = {'httpMethod': 'GET', 'resource': '/trace/{proxy+}', 'path': '/v1/trace/get',
          'pathParameters': {'proxy': 'get'}, 'queryStringParameters': qs,
          'headers': {'Host': 'api.example.com'}, 'body': None, 'isBase64Encoded': False}
    res = trace_mod.handler(ev, None)
    ctype = res['headers']['Content-Type']
    return res['statusCode'], (json.loads(res['body']) if ctype.startswith('application/json') else res['body']), ctype


def test_trace_json_is_cleaned_and_scoped(xr):
    xr(docs())
    s, b, _ = call({'ref': REF})
    assert s == 200 and b['pending'] is False and b['traceId'] == REF
    text = json.dumps({k: v for k, v in b.items() if k != 'links'})   # links are our own URLs
    for secret in ('123456789012', 'abc123', 'execute-api', '203.0.113.9', 'curl/8', 'r-1', 'r-2',
                   'arn:aws', SECRET_NS, '?', 'mc-v1-web'):
        assert secret not in text, secret
    paths = [r['path'] for r in b['requests']]
    assert paths == ['/v1/health/check', '/v1/lists/pop']
    top, probe = b['requests']
    assert top['function'] == 'testhelper-v1-HealthFunction-AbC' and top['coldStart'] is True
    assert top['initMs'] == pytest.approx(420, abs=1) and top['status'] == 200
    assert probe['status'] == 502 and probe['failed'] is True and probe['startMs'] == pytest.approx(900, abs=1)   # the trace starts at the cold start (t0 - 0.4 s)
    # The tree: the probe's API Gateway span hangs under the health function.
    spans = b['spans']
    assert spans[0]['kind'] == 'api' and spans[0]['parent'] is None
    fn = next(sp for sp in spans if sp['kind'] == 'function')
    probe_span = spans[probe['span']]
    assert probe_span['parent'] == fn['index'] and probe_span['depth'] == fn['depth'] + 1
    assert {sp['kind'] for sp in spans} >= {'api', 'lambda', 'function', 'init', 'invocation', 'overhead'}
    assert any('failed' in c for c in b['clues']) and any('cold start' in c for c in b['clues'])
    assert b['links']['page'] == 'https://api.example.com/v1/trace/get?ref=%s&format=html' % REF
    assert b['settled'] is True


def test_trace_page(xr):
    xr(docs())
    s, body, ctype = call({'ref': REF, 'format': 'html'})
    assert s == 200 and ctype.startswith('text/html')
    assert 'GET /v1/health/check' in body and 'What stands out' in body and '/v1/lists/pop' in body
    assert SECRET_NS not in body and '123456789012' not in body and 'mc-v1-web' not in body
    assert 'http-equiv="refresh"' not in body       # settled: no refresh


def test_young_trace_is_pending_then_arrives_with_wait(xr):
    fake = xr(None)
    s, b, _ = call({'ref': REF})
    assert s == 200 and b['pending'] is True and b['retryAfterSeconds'] == 5 and fake.calls == 1
    s, page, _ = call({'ref': REF, 'format': 'html'})
    assert s == 200 and 'http-equiv="refresh"' in page and 'still receiving' in page
    fake = xr(None, None, docs())
    s, b, _ = call({'ref': REF, 'wait': '5'})
    assert s == 200 and b['pending'] is False and fake.calls == 3


def test_unsettled_trace_page_refreshes(xr):
    young = '1-%08x-5f2a8c3e9d1b4a6f7e8c9d0a' % (NOW - 5)
    xr(docs(t0=NOW - 7.0))
    s, b, _ = call({'ref': young})
    assert b['settled'] is False
    s, page, _ = call({'ref': young, 'format': 'html'})
    assert 'http-equiv="refresh"' in page and 'may still be arriving' in page


def test_old_missing_or_foreign_trace_is_404(xr):
    old = '1-%08x-5f2a8c3e9d1b4a6f7e8c9d0a' % (NOW - 3600)
    xr(None)
    s, b, _ = call({'ref': old})
    assert s == 404 and 'from this deployment' in b['error']
    xr([d for d in docs() if d['name'] == 'mc-v1-web'])     # only another app's segments
    s, b, _ = call({'ref': REF})
    assert s == 404
    s, page, ctype = call({'ref': old, 'format': 'html'})
    assert ctype.startswith('text/html') and 'not found' in page.lower()


def test_bad_input(xr):
    xr(docs())
    assert call({'ref': 'nope'})[0] == 400
    assert call({})[0] == 400
    assert call({'ref': REF, 'format': 'xml'})[0] == 400
    assert call({'ref': REF.upper()})[0] == 200          # ids are matched case-insensitively


def test_a_request_fails_on_its_own_account_and_spans_start_at_zero(xr):
    xr(docs())
    _s, b, _ = call({'ref': REF})
    top, probe = b['requests']
    assert 'failed' not in top and probe['failed'] is True
    assert min(sp['startMs'] for sp in b['spans']) == 0
    assert '1 request(s) failed: GET /v1/lists/pop -> 502.' in b['clues']
    assert '1 request ran inside GET /v1/health/check.' in b['clues']
