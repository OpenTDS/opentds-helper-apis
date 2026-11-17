"""Tests for the webhook and totp services (DynamoDB via moto)."""
import base64
import json
import os
import sys
import time

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.update({
    'AWS_DEFAULT_REGION': 'ap-southeast-2', 'AWS_ACCESS_KEY_ID': 'testing',
    'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_SESSION_TOKEN': 'testing',
    'BASE_URL': 'https://api.example.com/v1',
    'WEBHOOK_TABLE': 'test-webhook', 'TOTP_TABLE': 'test-totp'})

import totp     # noqa: E402
import webhook  # noqa: E402


@pytest.fixture(autouse=True)
def aws():
    with mock_aws():
        webhook._table = None
        totp._table = None
        c = boto3.client('dynamodb')
        for name, sk in (('test-webhook', 'seq'), ('test-totp', 'user')):
            c.create_table(
                TableName=name, BillingMode='PAY_PER_REQUEST',
                AttributeDefinitions=[{'AttributeName': 'key', 'AttributeType': 'S'},
                                      {'AttributeName': sk, 'AttributeType': 'S'}],
                KeySchema=[{'AttributeName': 'key', 'KeyType': 'HASH'},
                           {'AttributeName': sk, 'KeyType': 'RANGE'}])
        yield


def event(service, action, qs=None, method='GET', body=None, headers=None, b64=False):
    return {
        'httpMethod': method, 'resource': f'/{service}/{{proxy+}}',
        'path': f'/v1/{service}/{action}',
        'pathParameters': {'proxy': action} if action else None,
        'queryStringParameters': qs,
        'headers': {'Host': 'api.example.com', **(headers or {})},
        'body': body, 'isBase64Encoded': b64,
        'requestContext': {'identity': {'sourceIp': '203.0.113.10'}},
    }


def call(mod, service, action, qs=None, **kw):
    res = mod.handler(event(service, action, qs, **kw), None)
    return res['statusCode'], (json.loads(res['body']) if res['body'] else None)


# ---------------------------------------------------------------- webhook
def test_webhook_receive_list_clear():
    s, b = call(webhook, 'webhook', 'receive', {'key': 'run1', 'orderId': '42'},
                method='POST', body='{"event":"paid","amount":10.50}',
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Basic dXNlcjpwYXNz'})
    assert s == 200 and b['received'] is True and b['key'] == 'run1'
    call(webhook, 'webhook', 'receive', {'key': 'run1'}, method='GET')
    s, b = call(webhook, 'webhook', 'list', {'Key': 'run1'})     # case-insensitive name
    assert s == 200 and b['count'] == 2 and b['more'] is False
    newest, oldest = b['events']
    assert newest['method'] == 'GET'
    assert oldest['method'] == 'POST' and oldest['json'] == {'event': 'paid', 'amount': 10.5}
    assert oldest['query'] == {'orderId': '42'}
    assert oldest['headers']['authorization'] == 'Basic dXNlcjpwYXNz'
    assert oldest['receivedTime'] <= int(time.time())
    s, b = call(webhook, 'webhook', 'list', {'key': 'run1', 'limit': '1'})
    assert b['count'] == 1 and b['more'] is True
    s, b = call(webhook, 'webhook', 'clear', {'key': 'run1'})
    assert s == 200 and b['deleted'] == 2
    s, b = call(webhook, 'webhook', 'list', {'key': 'run1'})
    assert b['count'] == 0


def test_webhook_status_and_binary_and_since():
    s, b = call(webhook, 'webhook', 'receive', {'key': 'k', 'status': '503'}, method='PUT',
                body=base64.b64encode(b'\xff\xfe\x00bin').decode(), b64=True)
    assert s == 503 and b['status'] == 503
    s, _ = call(webhook, 'webhook', 'receive', {'key': 'k', 'status': '204'}, method='POST')
    assert s == 204
    s, b = call(webhook, 'webhook', 'list', {'key': 'k'})
    assert any('bodyBase64' in e for e in b['events'])
    future = int(time.time()) + 60
    s, b = call(webhook, 'webhook', 'list', {'key': 'k', 'since': str(future)})
    assert b['count'] == 0
    t0 = time.time()
    s, b = call(webhook, 'webhook', 'list', {'key': 'k', 'since': str(future), 'wait': '1'})
    assert b['count'] == 0 and time.time() - t0 >= 0.9


def test_webhook_errors():
    assert call(webhook, 'webhook', 'list', {})[0] == 400
    s, b = call(webhook, 'webhook', 'list', {'key': 'bad#key'})
    assert s == 400 and b['status'] == 400
    s, b = call(webhook, 'webhook', 'nope', {'key': 'k'})
    assert s == 404 and 'receive' in b['actions']
    s, b = call(webhook, 'webhook', '', None)
    assert s == 200 and b['service'] == 'webhook'


# ------------------------------------------------------------------ totp
RFC_SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'   # RFC 6238 SHA1 seed


def test_totp_rfc_vectors():
    assert totp.totp(RFC_SECRET, 59, digits=8) == '94287082'
    assert totp.totp(RFC_SECRET, 1111111109, digits=8) == '07081804'
    assert totp.totp(RFC_SECRET, 20000000000, digits=8) == '65353130'


def test_totp_set_get():
    s, b = call(totp, 'totp', 'set', {'key': 'run1', 'user': 'alice', 'secret': RFC_SECRET})
    assert s == 200 and b['saved'] is True
    s, b = call(totp, 'totp', 'get', {'key': 'run1', 'user': 'alice'})
    assert s == 200 and len(b['code']) == 6 and len(b['nextCode']) == 6
    assert b['code'] == totp.totp(RFC_SECRET, time.time()) or b['secondsRemaining'] > 29
    assert 0 < b['secondsRemaining'] <= 30 and b['period'] == 30


def test_totp_stateless_and_options():
    s, b = call(totp, 'totp', 'get', {'secret': RFC_SECRET, 'digits': '8'})
    assert s == 200 and len(b['code']) == 8 and 'key' not in b
    s, b = call(totp, 'totp', 'set', {'key': 'k', 'user': 'u', 'secret': RFC_SECRET,
                                      'algorithm': 'SHA256', 'period': '60'})
    assert s == 200 and b['algorithm'] == 'sha256'
    s, b = call(totp, 'totp', 'get', {'key': 'k', 'user': 'u'})
    assert b['period'] == 60 and b['algorithm'] == 'sha256'


def test_totp_errors():
    assert call(totp, 'totp', 'get', {'key': 'k', 'user': 'nobody'})[0] == 404
    assert call(totp, 'totp', 'set', {'key': 'k', 'user': 'u', 'secret': '!!!'})[0] == 400
    assert call(totp, 'totp', 'set', {'key': 'k', 'user': 'u'})[0] == 400
    assert call(totp, 'totp', 'get', {'secret': RFC_SECRET, 'digits': '9'})[0] == 400
