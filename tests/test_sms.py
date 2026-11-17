"""Tests for the sms service (DynamoDB + SNS via moto)."""
import json
import os
import sys
import threading
import time

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.update({
    'AWS_DEFAULT_REGION': 'us-west-1', 'AWS_ACCESS_KEY_ID': 'testing',
    'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_SESSION_TOKEN': 'testing',
    'BASE_URL': 'https://api.example.com/v1', 'SMS_TABLE': 'test-sms',
    'SMS_REGION': 'ap-southeast-2', 'SMS_DEVICE_KEYS': 'old-key-1,device-key-2',
    'SMS_ALLOWED_NUMBERS': '+61400000001,+61400000002', 'SMS_SEND_INTERVAL': '10'})

import sms  # noqa: E402

HANDSET = '0400000001'          # registered
FAKE = '0491570156'             # fictitious, not registered
KEY = 'device-key-2'


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    # Other test modules set BASE_URL at import time; pin ours per test.
    monkeypatch.setenv('BASE_URL', 'https://api.example.com/v1')
    with mock_aws():
        sms._table = None
        sms._sns = None
        boto3.client('dynamodb').create_table(
            TableName='test-sms', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': a, 'AttributeType': 'S'}
                                  for a in ('Service', 'Received', 'Day')],
            KeySchema=[{'AttributeName': 'Service', 'KeyType': 'HASH'},
                       {'AttributeName': 'Received', 'KeyType': 'RANGE'}],
            GlobalSecondaryIndexes=[{
                'IndexName': 'ByDay',
                'KeySchema': [{'AttributeName': 'Day', 'KeyType': 'HASH'},
                              {'AttributeName': 'Received', 'KeyType': 'RANGE'}],
                'Projection': {'ProjectionType': 'ALL'}}])
        yield


def call(action, qs=None, method='GET', body=None, headers=None):
    ev = {'httpMethod': method, 'resource': '/sms/{proxy+}', 'path': '/v1/sms/' + action,
          'pathParameters': {'proxy': action}, 'queryStringParameters': qs,
          'headers': {'Host': 'api.example.com', **(headers or {})},
          'body': json.dumps(body) if isinstance(body, dict) else body,
          'isBase64Encoded': False, 'requestContext': {'identity': {'sourceIp': '203.0.113.9'}}}
    res = sms.handler(ev, None)
    return res['statusCode'], json.loads(res['body'])


def receive(number, text, key=KEY, sender='+61411111111'):
    return call('receive', {'number': number}, 'POST',
                {'sender': sender, 'time': int(time.time() * 1000), 'body': text},
                {'X-Device-Key': key} if key else {})


# ------------------------------------------------------------------ receive
def test_receive_requires_a_valid_device_key():
    s, b = receive(HANDSET, 'hello', key=None)
    assert s == 401 and 'X-Device-Key' in b['error']
    s, b = receive(HANDSET, 'hello', key='wrong')
    assert s == 401
    s, b = call('get', {'number': HANDSET})
    assert b['found'] is False                      # nothing was stored
    s, b = receive(HANDSET, 'hello', key='old-key-1')  # either configured key works
    assert s == 200 and b['saved'] is True and b['mocked'] is False


def test_receive_disabled_without_keys(monkeypatch):
    monkeypatch.setenv('SMS_DEVICE_KEYS', '')
    s, b = receive(HANDSET, 'hello')
    assert s == 403 and 'disabled' in b['error']


def test_receive_then_get_matches_on_last_nine_digits():
    receive('+61400000001', 'Your code is 482913')
    s, b = call('get', {'number': '0400000001'})
    assert s == 200 and b['found'] is True and b['mocked'] is False
    assert b['body'] == 'Your code is 482913' and b['number'] == '400000001'
    assert b['sender'] == '+61411111111' and b['handsetAgeSeconds'] >= 0


def test_receive_accepts_android_field_aliases_and_rejects_bad_bodies():
    s, b = call('receive', {'number': HANDSET}, 'POST',
                {'Sender': 'Bank', 'Date': '2026-10-06T01:02:03Z', 'Message': 'Hi'},
                {'X-Device-Key': KEY})
    assert s == 200 and b['handsetTime'] == 1791248523
    assert call('receive', {'number': HANDSET}, 'POST', 'not json', {'X-Device-Key': KEY})[0] == 400
    assert call('receive', {'number': HANDSET}, 'POST', {'sender': 'x'}, {'X-Device-Key': KEY})[0] == 400
    assert call('receive', {}, 'POST', {'body': 'x'}, {'X-Device-Key': KEY})[0] == 400


# ------------------------------------------------------------------ get
def test_get_wait_only_returns_messages_arriving_after_the_call():
    receive(HANDSET, 'stale code 111111')
    threading.Timer(1.0, lambda: receive(HANDSET, 'fresh code 222222')).start()
    t0 = time.time()
    s, b = call('get', {'number': HANDSET, 'wait': '5'})
    assert b['found'] is True and b['body'] == 'fresh code 222222'
    assert 0.8 < time.time() - t0 < 4


def test_get_wait_times_out_with_age_of_newest():
    receive(HANDSET, 'old')
    s, b = call('get', {'number': HANDSET, 'wait': '1'})
    assert s == 200 and b['found'] is False and 'newestAgeSeconds' in b


def test_get_since_anchors_the_window():
    receive(HANDSET, 'first')
    s, b = call('get', {'number': HANDSET, 'since': str(int(time.time()) - 60)})
    assert b['found'] is True
    s, b = call('get', {'number': HANDSET, 'since': str(int(time.time()) + 60)})
    assert b['found'] is False


def test_unknown_number_is_200_not_found():
    s, b = call('get', {'number': '0499999999'})
    assert s == 200 and b['found'] is False and b['body'] is None


# ------------------------------------------------------------------ mocksend
def test_mocksend_then_get_reports_mocked():
    s, b = call('mocksend', {'number': FAKE})
    assert s == 200 and b['mocked'] is True and len(b['code']) == 6
    assert b['code'] in b['body'] and b['getUrl'].startswith('https://api.example.com/v1/sms/get?')
    s, g = call('get', {'number': FAKE})
    assert g['found'] is True and g['mocked'] is True and g['body'] == b['body']
    assert g['sender'] == 'TestHelper'


def test_get_real_true_ignores_mocks():
    call('mocksend', {'number': FAKE, 'code': 'ABC123'})
    s, b = call('get', {'number': FAKE, 'real': 'true'})
    assert b['found'] is False


def test_mocksend_custom_code_body_and_sender():
    s, b = call('mocksend', {'number': FAKE, 'code': '777', 'body': 'PIN {code} for login',
                             'from': 'MyBank'})
    assert s == 200 and b['body'] == 'PIN 777 for login'
    assert call('get', {'number': FAKE})[1]['sender'] == 'MyBank'


def test_mocksend_refused_for_registered_and_capture_handsets():
    s, b = call('mocksend', {'number': '+61400000002'})          # on the send allow-list
    assert s == 403 and 'sendUrl' in b
    receive('0400000077', 'real text')                            # a capture-only handset
    s, b = call('mocksend', {'number': '0400000077'})
    assert s == 403


def test_mocksend_rate_limited_per_number():
    assert call('mocksend', {'number': FAKE})[0] == 200
    s, b = call('mocksend', {'number': FAKE})
    assert s == 429 and b['retryAfterSeconds'] <= 1
    assert call('mocksend', {'number': '0491570157'})[0] == 200   # other numbers unaffected


# ------------------------------------------------------------------ send
def test_send_to_unregistered_number_suggests_mocksend():
    s, b = call('send', {'number': FAKE, 'code': '123456', 'body': 'Code {code}'})
    assert s == 403 and 'no text was sent' in b['error']
    assert 'mocksend' in b['hint']
    assert b['mockUrl'] == ('https://api.example.com/v1/sms/mocksend?number=0491570156'
                            '&code=123456&body=Code%20%7Bcode%7D')


def test_send_real_text_via_configured_region_and_rate_limit():
    s, b = call('send', {'number': '0400000001'})
    assert s == 200 and b['sent'] is True and b['mocked'] is False
    assert b['to'] == '+61400000001' and len(b['code']) == 6 and b['messageId']
    assert 'real=true' in b['getUrl']
    assert sms.sns().meta.region_name == 'ap-southeast-2'
    s, b = call('send', {'number': '0400000001'})
    assert s == 429 and 5 < b['retryAfterSeconds'] <= 10
    assert call('send', {'number': '0400000002'})[0] == 200        # per number


def test_send_validation():
    assert call('send', {'number': '0400000001', 'code': 'bad code!'})[0] == 400
    assert call('send', {'number': 'abc'})[0] == 400
    assert call('send', {})[0] == 400
    assert call('send', {'number': '0400000001', 'body': 'x' * 500})[0] == 400


# ------------------------------------------------------------------ list
def test_list_sweeps_all_numbers_newest_first_with_filters():
    receive(HANDSET, 'real one')
    call('mocksend', {'number': FAKE})
    s, b = call('list', {'seconds': '60'})
    assert s == 200 and b['count'] == 2
    assert b['numbers'] == sorted(['400000001', '491570156'])
    assert b['messages'][0]['mocked'] is True                     # newest first
    s, b = call('list', {'seconds': '60', 'real': 'true'})
    assert b['count'] == 1 and b['messages'][0]['mocked'] is False
    s, b = call('list', {'seconds': '60', 'number': FAKE})
    assert b['count'] == 1 and b['number'] == '491570156'


def test_housekeeping_rows_never_appear_as_messages():
    receive(HANDSET, 'real')
    call('send', {'number': '0400000001'})                        # writes the !send row
    s, b = call('list', {'number': HANDSET, 'seconds': '60'})
    assert b['count'] == 1
    s, b = call('get', {'number': HANDSET})
    assert b['body'] == 'real'


def test_index_and_unknown_action():
    ev = {'httpMethod': 'GET', 'resource': '/sms', 'path': '/v1/sms', 'pathParameters': None,
          'queryStringParameters': None, 'headers': {}, 'body': None}
    b = json.loads(sms.handler(ev, None)['body'])
    assert set(b['actions']) == {'receive', 'get', 'list', 'send', 'mocksend'}
    s, b = call('save', {})
    assert s == 404 and 'receive' in b['actions']
