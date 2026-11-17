"""Tests for the lock service (DynamoDB via moto)."""
import json
import os
import sys
import threading
import time
import urllib.parse

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
os.environ['LOCK_TABLE'] = 'test-lock'

import lock  # noqa: E402


@pytest.fixture(autouse=True)
def aws():
    with mock_aws():
        lock._ddb = None
        boto3.client('dynamodb').create_table(
            TableName='test-lock', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': 'LockKey', 'AttributeType': 'S'}],
            KeySchema=[{'AttributeName': 'LockKey', 'KeyType': 'HASH'}])
        yield
        lock._ddb = None


def event(action, qs=None, method='GET'):
    return {
        'httpMethod': method,
        'resource': '/lock/{proxy+}',
        'path': '/v1/lock/' + action,
        'pathParameters': {'proxy': action} if action else None,
        'queryStringParameters': qs,
        'headers': {'Host': 'api.example.com'},
        'body': None,
        'isBase64Encoded': False,
        'requestContext': {'identity': {'sourceIp': '203.0.113.10'}},
    }


def call(action, qs=None, method='GET'):
    res = lock.handler(event(action, qs, method), None)
    return res['statusCode'], json.loads(res['body']) if res['body'] else None


def test_acquire_and_release():
    s, b = call('acquire', {'key': 'test-account-07'})
    assert s == 200 and b['locked'] is True and b['key'] == 'test-account-07'
    ref = b['ref']
    assert ref.startswith('test-account-07:') and len(ref.rsplit(':', 1)[1]) == 8
    assert b['maxSeconds'] == 60
    assert abs(b['expiresTime'] - (int(time.time()) + 60)) <= 2
    assert b['waitedMs'] >= 0
    assert b['releaseUrl'] == ('https://api.example.com/v1/lock/release?ref='
                               + urllib.parse.quote(ref, safe=''))

    s, b = call('release', {'ref': ref})
    assert s == 200 and b['released'] is True and b['key'] == 'test-account-07'
    assert b['ref'] == ref and b['heldSeconds'] >= 0

    # released twice -> honest failure, still 200
    s, b = call('release', {'ref': ref})
    assert s == 200 and b['released'] is False and b['reason'] == 'already released'

    # free again
    s, b = call('acquire', {'KEY': 'test-account-07'})
    assert b['locked'] is True


def test_busy_is_200_locked_false():
    _, first = call('acquire', {'key': 'shared'})
    t0 = time.time()
    s, b = call('acquire', {'key': 'shared', 'wait': '1'})
    assert s == 200 and b['locked'] is False and b['ref'] is None
    assert b['key'] == 'shared'
    assert 55 <= b['freeInSeconds'] <= 60
    assert b['waitedMs'] >= 900 and time.time() - t0 < 3
    assert 'comment' in b


def test_wait_is_clamped_and_malformed_rejected():
    call('acquire', {'key': 'k'})
    s, b = call('acquire', {'key': 'k', 'wait': '-5'})
    assert s == 200 and b['locked'] is False and b['waitedMs'] < 900
    s, b = call('acquire', {'key': 'k', 'wait': 'soon'})
    assert s == 400


def test_waiter_gets_lock_when_holder_releases():
    _, first = call('acquire', {'key': 'dev-1'})
    timer = threading.Timer(0.8, lambda: call('release', {'ref': first['ref']}))
    timer.start()
    try:
        s, b = call('acquire', {'key': 'dev-1', 'wait': '5'})
    finally:
        timer.join()
    assert b['locked'] is True and b['ref'] != first['ref']
    assert 500 <= b['waitedMs'] < 4000


def test_expired_lock_can_be_taken_and_old_ref_cannot_release(monkeypatch):
    monkeypatch.setattr(lock, 'MAX_HOLD_S', 1)
    _, first = call('acquire', {'key': 'k2'})
    s, second = call('acquire', {'key': 'k2', 'wait': '4'})
    assert second['locked'] is True
    monkeypatch.setattr(lock, 'MAX_HOLD_S', 60)
    s, b = call('release', {'ref': first['ref']})
    assert s == 200 and b['released'] is False
    assert b['reason'].startswith('not the current holder')
    # the real holder still releases fine
    assert call('release', {'ref': second['ref']})[1]['released'] is True


def test_expired_unreleased_reason(monkeypatch):
    monkeypatch.setattr(lock, 'MAX_HOLD_S', 0)
    _, first = call('acquire', {'key': 'k3'})
    time.sleep(1.1)
    monkeypatch.setattr(lock, 'MAX_HOLD_S', 60)
    # ref still stored but expired; a forged ref for the same key fails with that reason
    _, b = call('release', {'ref': 'k3:00000000'})
    assert b['released'] is False and 'already expired' in b['reason']


def test_release_unknown_key_and_key_with_colon():
    _, b = call('release', {'ref': 'never-locked:deadbeef'})
    assert b['released'] is False and b['reason'] == 'no lock has ever been taken on this key'
    _, a = call('acquire', {'key': 'env:uat:user@example.com'})
    assert a['locked'] is True
    _, b = call('release', {'ref': a['ref']})
    assert b['released'] is True and b['key'] == 'env:uat:user@example.com'


def test_errors():
    assert call('acquire')[0] == 400
    s, b = call('acquire', {'key': 'bad/key'})
    assert s == 400 and b['received'] == 'bad/key'
    assert call('acquire', {'key': 'x' * 129})[0] == 400
    assert call('release')[0] == 400
    assert call('release', {'lockid': 'k:abc'})[0] == 400       # old name is gone
    assert call('release', {'ref': 'nocolon'})[0] == 400
    assert call('release', {'ref': 'bad/key:abcd'})[0] == 400
    s, b = call('lock', {'key': 'k'})                           # old action is gone
    assert s == 404 and b['actions'] == ['acquire', 'release']
    assert call('')[1]['service'] == 'lock'
    assert lock.handler(event('acquire', method='OPTIONS'), None)['statusCode'] == 204
