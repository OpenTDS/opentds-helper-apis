"""Tests for the perf service (DynamoDB via moto)."""
import csv
import io
import json
import os
import sys
import time

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ['AWS_DEFAULT_REGION'] = 'ap-southeast-2'
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
os.environ['AWS_SECURITY_TOKEN'] = 'testing'
os.environ['AWS_SESSION_TOKEN'] = 'testing'
os.environ['BASE_URL'] = 'https://api.example.com/v1'
os.environ['PERF_TABLE'] = 'test-perf'

import perf  # noqa: E402

T0 = 1767225600          # 2026-01-01T00:00:00Z, a minute boundary


@pytest.fixture(autouse=True)
def aws():
    with mock_aws():
        perf._res = perf._tbl = None
        perf._registered.clear()
        boto3.client('dynamodb').create_table(
            TableName='test-perf', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': 'PK', 'AttributeType': 'S'},
                                  {'AttributeName': 'SK', 'AttributeType': 'S'}],
            KeySchema=[{'AttributeName': 'PK', 'KeyType': 'HASH'},
                       {'AttributeName': 'SK', 'KeyType': 'RANGE'}])
        yield
        perf._res = perf._tbl = None


def event(action, qs=None, method='GET'):
    return {
        'httpMethod': method,
        'resource': '/perf/{proxy+}',
        'path': '/v1/perf/' + action,
        'pathParameters': {'proxy': action} if action else None,
        'queryStringParameters': qs,
        'headers': {'Host': 'api.example.com'},
        'body': None,
        'isBase64Encoded': False,
        'requestContext': {'identity': {'sourceIp': '203.0.113.10'}},
    }


def call_raw(action, qs=None, method='GET'):
    return perf.handler(event(action, qs, method), None)


def call(action, qs=None, method='GET'):
    res = call_raw(action, qs, method)
    return res['statusCode'], json.loads(res['body'])


def add(key, values, **kw):
    qs = {'key': key, 'value': ','.join(str(v) for v in values)}
    qs.update({k: str(v) for k, v in kw.items()})
    s, b = call('add', qs)
    assert s == 200, b
    return b


def test_add_response_and_time_param():
    b = add('Login', [248.5], time=T0)
    assert b == {'key': 'Login', 'recorded': 1, 'time': T0, 'ok': True, 'raw': False}
    b = add('Login', [1, 2, 3], ok='false')
    assert b['recorded'] == 3 and b['ok'] is False and abs(b['time'] - time.time()) < 5


def test_stats_lifetime_exact_values_and_percentiles():
    vals = list(range(1, 101))                         # 1..100
    for i in range(0, 100, 25):                        # several calls, several shards
        add('Search', vals[i:i + 25], time=T0 + i)
    add('Search', [0], ok='false', time=T0 + 100)
    s, b = call('stats', {'key': 'Search', 'p': '99.5,10'})
    assert s == 200
    assert b['key'] == 'Search' and b['source'] == 'lifetime'
    assert b['count'] == 101 and b['errors'] == 1
    assert b['errorRate'] == round(1 / 101, 6)
    assert b['min'] == 0 and b['max'] == 100
    assert b['sum'] == 5050 and b['mean'] == round(5050 / 101, 4)
    for f in ('p50', 'p75', 'p90', 'p95', 'p99', 'p99_9', 'p99_5', 'p10', 'stdDev'):
        assert f in b
    # histogram percentiles are within ~2% relative
    assert abs(b['p50'] - 50) / 50 < 0.03
    assert abs(b['p90'] - 90) / 90 < 0.03
    assert b['p99_9'] <= 100
    assert b['firstTime'] == T0 and b['lastTime'] == T0 + 100
    assert b['durationSeconds'] == 100 and b['throughput'] == 1.01
    # no PascalCase anywhere
    assert not [k for k in b if k[0].isupper()]


def test_stats_empty_key():
    s, b = call('stats', {'key': 'nothing'})
    assert s == 200 and b['count'] == 0 and 'comment' in b


def test_windowed_stats_have_min_max():
    add('W', [10, 20], time=T0)
    add('W', [5, 500], time=T0 + 120)
    add('W', [1000], time=T0 + 600)
    s, b = call('stats', {'key': 'W', 'since': T0, 'until': T0 + 179})
    assert b['source'] == 'window' and b['since'] == T0
    assert b['count'] == 4 and b['min'] == 5 and b['max'] == 500
    assert 'comment' in b


def test_series():
    add('S', [100, 200], time=T0 + 5)
    add('S', [300], ok='false', time=T0 + 65)
    add('S', [50], time=T0 + 125)
    s, b = call('series', {'key': 'S', 'p': '50'})
    assert s == 200 and b['intervalSeconds'] == 60 and b['buckets'] == 3
    r0, r1 = b['series'][0], b['series'][1]
    assert r0['startTime'] == T0 and r0['count'] == 2
    assert r0['min'] == 100 and r0['max'] == 200 and r0['mean'] == 150
    assert r0['throughput'] == round(2 / 60, 4) and 'p50' in r0
    assert r1['errors'] == 1 and r1['errorRate'] == 1
    s, b = call('series', {'key': 'S', 'interval': '120'})
    assert b['buckets'] == 2 and b['series'][0]['count'] == 3 and 'p90' in b['series'][0]
    s, b = call('series', {'key': 'S', 'since': T0 + 60, 'until': T0 + 60})
    assert b['buckets'] == 1 and b['series'][0]['startTime'] == T0 + 60


def test_series_errors_and_empty():
    assert call('series', {'key': 'S', 'interval': '90'})[0] == 400
    assert call('series', {'key': 'S', 'interval': 'x'})[0] == 400
    s, b = call('series', {'key': 'none'})
    assert s == 200 and b['buckets'] == 0 and b['series'] == []


def test_assert_pass_fail_and_strict():
    add('A', [100, 200, 300, 400], time=T0)
    add('A', [500], ok='false', time=T0 + 10)
    s, b = call('assert', {'key': 'A', 'p90lt': '800', 'countge': '5', 'errorratelt': '0.5',
                           'p99.9le': '500'})
    assert s == 200 and b['pass'] is True and b['failed'] == 0 and b['key'] == 'A'
    by = {c['check']: c for c in b['checks']}
    assert by['p90lt']['statistic'] == 'p90' and by['p90lt']['operator'] == '<'
    assert by['errorratelt']['statistic'] == 'errorRate' and by['errorratelt']['actual'] == 0.2
    assert by['p99.9le']['statistic'] == 'p99_9'
    assert b['summary']['count'] == 5

    s, b = call('assert', {'key': 'A', 'meanlt': '100', 'maxle': '500'})
    assert s == 200 and b['pass'] is False and b['failed'] == 1
    s, b = call('assert', {'key': 'A', 'MeanLT': '100', 'strict': 'true'})
    assert s == 412 and b['pass'] is False

    # a key with no data fails rather than passes
    s, b = call('assert', {'key': 'empty', 'countge': '0', 'strict': 'yes'})
    assert s == 412 and b['pass'] is False and b['checks'][0]['pass'] is False


def test_assert_errors():
    add('A', [1])
    s, b = call('assert', {'key': 'A'})
    assert s == 400 and 'hint' in b
    s, b = call('assert', {'key': 'A', 'p90less': '5'})
    assert s == 400 and 'Unrecognised' in b['error']
    assert call('assert', {'key': 'A', 'p90lt': 'fast'})[0] == 400
    assert call('assert', {'key': 'A', 'p900lt': '1'})[0] == 400


def test_renamed_params_rejected():
    s, b = call('stats', {'key': 'A', 'from': str(T0)})
    assert s == 400 and "'since'" in b['error']
    s, b = call('add', {'key': 'A', 'value': '1', 'ts': str(T0)})
    assert s == 400 and "'time'" in b['error']
    s, b = call('assert', {'key': 'A', 'to': str(T0), 'p90lt': '1'})
    assert s == 400


def test_keys_and_clear():
    add('Login', [1])
    add('Login.step2', [1])
    add('Search', [1])
    s, b = call('keys')
    assert s == 200 and b['count'] == 3 and b['prefix'] is None
    assert sorted(k['key'] for k in b['keys']) == ['Login', 'Login.step2', 'Search']
    assert all(k['firstTime'] > 0 and k['lastTime'] >= k['firstTime'] for k in b['keys'])
    s, b = call('keys', {'prefix': 'Login'})
    assert b['count'] == 2 and b['prefix'] == 'Login'

    s, b = call('clear', {'key': 'Login'})
    assert s == 200 and b['key'] == 'Login' and b['deleted'] >= 2 and b['more'] is False
    assert call('stats', {'key': 'Login'})[1]['count'] == 0
    assert [k['key'] for k in call('keys', {'prefix': 'Login'})[1]['keys']] == ['Login.step2']

    # re-adding after a clear re-registers the key even from a warm container
    add('Login', [7])
    assert 'Login' in [k['key'] for k in call('keys')[1]['keys']]
    s, b = call('stats', {'key': 'Login'})
    assert b['count'] == 1 and b['min'] == 7 and b['max'] == 7


def test_first_and_last_time_follow_point_times_not_write_order(monkeypatch):
    # All writes into one shard, so out-of-order points share an accumulator.
    monkeypatch.setattr(perf.random, 'randrange', lambda n: 0)
    perf._registered.clear()
    now = int(time.time())
    add('Ooo', [10], time=now - 60)
    add('Ooo', [20], time=now)
    add('Ooo', [30], time=now - 600)     # back-dated, after the others
    add('Ooo', [40], time=now - 300)
    b = call('stats', {'key': 'Ooo'})[1]
    assert b['firstTime'] == now - 600 and b['lastTime'] == now and b['durationSeconds'] == 600
    k = [k for k in call('keys', {'prefix': 'Ooo'})[1]['keys']][0]
    assert k['firstTime'] == now - 600 and k['lastTime'] == now


def test_min_max_correct_after_clear_on_warm_container():
    add('M', [5])
    call('clear', {'key': 'M'})
    add('M', [9])
    add('M', [7])
    b = call('stats', {'key': 'M'})[1]
    assert b['min'] == 7 and b['max'] == 9


def test_export_csv_and_json():
    add('E', [10, 20], raw='true', time=T0)
    add('E', [30], raw='true', ok='false', time=T0 + 5)
    add('E', [99])                                     # not raw -> not exported
    res = call_raw('export', {'key': 'E'})
    assert res['statusCode'] == 200
    assert res['headers']['Content-Type'].startswith('text/csv')
    rows = list(csv.reader(io.StringIO(res['body'])))
    assert rows[0] == ['timestampMs', 'value', 'ok']
    # 10 and 20 share a timestamp, so only their set is fixed; 30 is last
    assert sorted(r[1] for r in rows[1:]) == ['10', '20', '30']
    assert rows[-1] == [str((T0 + 5) * 1000), '30', 'false']

    s, b = call('export', {'key': 'E', 'format': 'json', 'limit': '2'})
    assert s == 200 and b['key'] == 'E' and b['count'] == 2 and b['truncated'] is True
    assert set(b['points'][0]) == {'timestampMs', 'value', 'ok'}
    assert all(p['timestampMs'] == T0 * 1000 for p in b['points'])

    s, b = call('export', {'key': 'E', 'format': 'json', 'since': T0 + 1})
    assert b['count'] == 1 and b['points'][0]['value'] == 30 and b['points'][0]['ok'] is False

    res = call_raw('export', {'key': 'none'})
    assert '# no raw points' in res['body']
    assert call('export', {'key': 'E', 'format': 'xml'})[0] == 400


def test_add_validation():
    assert call('add', {'value': '1'})[0] == 400                       # no key
    s, b = call('add', {'key': 'a/b', 'value': '1'})                  # '/' no longer allowed
    assert s == 400 and b['received'] == 'a/b'
    assert call('add', {'key': 'x' * 129, 'value': '1'})[0] == 400
    assert call('add', {'key': 'k'})[0] == 400
    assert call('add', {'key': 'k', 'value': 'abc'})[0] == 400
    assert call('add', {'key': 'k', 'value': '-1'})[0] == 400
    assert call('add', {'key': 'k', 'value': '1e13'})[0] == 400
    assert call('add', {'key': 'k', 'value': 'NaN'})[0] == 400
    assert call('add', {'key': 'k', 'value': ','.join(['1'] * 101)})[0] == 400
    assert call('add', {'key': 'k', 'value': '1', 'ok': 'maybe'})[0] == 400
    s, b = call('add', {'key': 'k', 'value': '1', 'time': str(T0 * 1000)})
    assert s == 400 and 'milliseconds' in b['hint']
    assert call('stats', {'key': 'k', 'since': '5', 'until': '4'})[0] == 400
    assert call('stats', {'key': 'k', 'p': 'abc'})[0] == 400
    assert call('stats', {'key': 'k', 'p': '101'})[0] == 400


def test_value_rounding_and_zero():
    add('R', ['0.1234567', '0'])
    b = call('stats', {'key': 'R'})[1]
    assert b['count'] == 2 and b['min'] == 0 and b['max'] == 0.123457
    assert b['p50'] == 0


def test_routing():
    s, b = call('nope')
    assert s == 404 and b['actions'] == sorted(perf.ROUTES)
    assert call('')[1]['service'] == 'perf'
    assert call_raw('stats', method='OPTIONS')['statusCode'] == 204
