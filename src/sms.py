"""sms - text messages for end-to-end tests (v1).

  POST /v1/sms/receive?number=N   a capture handset posts each text it receives.
                                  Requires the X-Device-Key header.
  GET  /v1/sms/get?number=N[&wait][&since][&real]   newest message for a number
  GET  /v1/sms/list?seconds=S[&number][&real]       everything in the last S seconds
  GET  /v1/sms/send?number=N[&code][&body]          a REAL text, to registered handsets only
  GET  /v1/sms/mocksend?number=N[&code][&body][&from]  a simulated text, for any other number

Trust model
-----------
Reads are open, like the rest of v1. What makes them trustworthy is the write
side: a real message only enters the store through sms/receive with a valid
device key (a secret built into the capture app and configured on the stack),
and a mocked message can never be filed under a registered handset's number.
So for a registered number, everything sms/get returns came from that phone.
Every message carries mocked:true|false so a script can always tell.

Storage (env SMS_TABLE)
-----------------------
Partition key Service = the number's last nine digits (short codes whole), so
+61412345678, 61412345678 and 0412345678 are one inbox. Sort key Received =
zero-padded epoch ms + '#' + random suffix, so lexical order is time order.
A day-bucket GSI (Day, Received) lets sms/list sweep every number with a Query.

Each number also has housekeeping rows whose sort keys start with '!', which
sorts before any digit and is excluded from every message query:
  !real  - stamped by every authenticated receive: marks a capture handset
  !send  - rate limit for real sends (one per SMS_SEND_INTERVAL seconds)
  !mock  - rate limit for mock sends (one per second)
"""
import hmac
import json
import os
import random
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from common import ApiError, base_url, respond, run

SERVICE = 'sms'
DAY_INDEX = 'ByDay'

WAIT_MAX_S = 20
POLL_START_S = 0.25
POLL_MAX_S = 1.0
REAL_RETENTION_DAYS = 7
MOCK_RETENTION_DAYS = 1
MAX_WINDOW_S = 86400
DEFAULT_WINDOW_S = 300
MAX_RESULTS = 5000
MAX_LIST_BYTES = 5_000_000      # Lambda proxy responses cap at 6 MB; stop well short
MAX_MESSAGE = 4000
MAX_SEND_CHARS = 480            # ~3 concatenated SMS segments; each one is billed
CODE_LEN = 6
CODE_RE = re.compile(r'^[A-Za-z0-9]{1,12}$')
DEFAULT_TEMPLATE = 'Your verification code is {code}'
MOCK_SENDER = 'TestHelper'
MOCK_INTERVAL_S = 1
MSG_FLOOR = '0'                 # every message sort key is >= this; '!' rows are below it

_table = None
_sns = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource('dynamodb').Table(os.environ['SMS_TABLE'])
    return _table


def sns():
    # Real texts go out from the region where the account's SMS production
    # access and spend limit live, which need not be the stack's region.
    global _sns
    if _sns is None:
        _sns = boto3.client('sns', region_name=os.environ.get('SMS_REGION') or None)
    return _sns


# ------------------------------------------------------------------ numbers
def canon(raw):
    """Last nine digits of a phone number (short codes kept whole), or None."""
    digits = re.sub(r'\D', '', raw or '')
    if not digits:
        return None
    return digits[-9:] if len(digits) > 9 else digits


def registered():
    """{canonical number: number as configured} for the real-send allow-list."""
    out = {}
    for item in (os.environ.get('SMS_ALLOWED_NUMBERS') or '').split(','):
        item = item.strip()
        if item and canon(item):
            out[canon(item)] = item
    return out


def device_keys():
    return [k.strip() for k in (os.environ.get('SMS_DEVICE_KEYS') or '').split(',') if k.strip()]


def send_interval():
    try:
        return max(0, int(os.environ.get('SMS_SEND_INTERVAL') or 10))
    except ValueError:
        return 10


def number_param(req, required=True):
    raw = req.text('number', required=required)
    if raw is None:
        return None, None
    n = canon(raw)
    if not n:
        raise ApiError(400, "'number' must contain digits.", received=raw,
                       hint='e.g. number=0491570156 or number=%2B61491570156 (send + as %2B)')
    return n, raw


# ------------------------------------------------------------------ helpers
def sort_key(ms):
    return '%015d#%04d' % (ms, random.randrange(10000))


def day_of(epoch_s):
    return 'D#' + time.strftime('%Y-%m-%d', time.gmtime(epoch_s))


def parse_phone_time(v):
    """Handset timestamp -> epoch seconds where possible (ms, s or ISO 8601)."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float, Decimal)):
        n = float(v)
    else:
        s = str(v).strip()
        try:
            n = float(s)
        except ValueError:
            try:
                d = datetime.fromisoformat(s.replace('Z', '+00:00'))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return int(d.timestamp())
            except ValueError:
                return None
    if n > 1e11:
        n /= 1000.0
    return int(n) if 0 < n < 4e10 else None


def shape(it, now):
    saved = int(it.get('SavedTime', 0))
    phone = it.get('TimeUtc')
    out = {
        'number': it.get('Service'),
        'mocked': bool(it.get('Mocked', False)),
        'sender': it.get('Sender'),
        'body': it.get('Message'),
        'receivedTime': saved,
        'ageSeconds': max(0, now - saved),
        'handsetTime': int(phone) if phone is not None else None,
        'handsetTimeRaw': it.get('Time'),
        'seq': it.get('Received'),
    }
    if phone is not None:
        out['handsetAgeSeconds'] = max(0, now - int(phone))
    return out


def store(number, raw_number, sender, message, mocked, phone_time=None):
    now = int(time.time())
    ms = int(time.time() * 1000)
    days = MOCK_RETENTION_DAYS if mocked else REAL_RETENTION_DAYS
    item = {'Service': number, 'Received': sort_key(ms), 'ServiceRaw': str(raw_number),
            'Message': message, 'SavedTime': now, 'Day': day_of(now),
            'Mocked': mocked, 'ttl': now + days * 86400}
    if sender is not None:
        item['Sender'] = str(sender)
    if phone_time is not None:
        item['Time'] = phone_time if isinstance(phone_time, str) else str(phone_time)
        tutc = parse_phone_time(phone_time)
        if tutc is not None:
            item['TimeUtc'] = tutc
    table().put_item(Item=item)
    return item


def throttle(number, row, interval):
    """One action per `interval` seconds per number, via a conditional write."""
    if interval <= 0:
        return
    now = time.time()
    try:
        table().update_item(
            Key={'Service': number, 'Received': row},
            UpdateExpression='SET NextAllowed = :next, #t = :ttl',
            ConditionExpression='attribute_not_exists(NextAllowed) OR NextAllowed <= :now',
            ExpressionAttributeNames={'#t': 'ttl'},
            ExpressionAttributeValues={':now': Decimal(str(round(now, 3))),
                                       ':next': Decimal(str(round(now + interval, 3))),
                                       ':ttl': int(now) + 86400})
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        it = table().get_item(Key={'Service': number, 'Received': row},
                              ConsistentRead=True).get('Item') or {}
        wait = max(0.0, float(it.get('NextAllowed', now)) - now)
        raise ApiError(429, f'Too many texts to this number: one every {interval} s.',
                       number=number, retryAfterSeconds=round(wait, 1))


def is_capture_handset(number):
    return bool(table().get_item(Key={'Service': number, 'Received': '!real'}).get('Item'))


def _query_newest(number, real_only, newer_than=None):
    """Newest message for a number (optionally real ones only, newer than a sort key)."""
    lo = newer_than or MSG_FLOOR
    kw = {'KeyConditionExpression': Key('Service').eq(number) & Key('Received').gt(lo),
          'ScanIndexForward': False, 'Limit': 1 if not real_only else 25}
    if real_only:
        kw['FilterExpression'] = Attr('Mocked').ne(True)
    while True:
        page = table().query(**kw)
        items = page.get('Items') or []
        if items:
            return items[0]
        lek = page.get('LastEvaluatedKey')
        if not lek:
            return None
        kw['ExclusiveStartKey'] = lek


# ------------------------------------------------------------------ receive
def do_receive(req):
    keys = device_keys()
    if not keys:
        raise ApiError(403, 'sms/receive is disabled: no device keys are configured on this '
                            'deployment.', hint='set the SmsDeviceKeys stack parameter')
    given = req.headers.get('x-device-key') or ''
    if not any(hmac.compare_digest(given.encode(), k.encode()) for k in keys):
        raise ApiError(401, 'Missing or wrong X-Device-Key header - message not stored.',
                       hint='the capture app must send the device key configured on the stack')
    number, raw_number = number_param(req)

    payload = {}
    body = req.body_bytes().decode('utf-8', 'replace') if req.body else ''
    if body.strip():
        try:
            payload = json.loads(body)
        except ValueError:
            raise ApiError(400, 'Request body is not valid JSON.', received=body[:200],
                           hint='send {"sender": "...", "time": ..., "body": "..."} with '
                                'Content-Type: application/json')
        if not isinstance(payload, dict):
            raise ApiError(400, 'Request body must be a JSON object.')
    low = {str(k).lower(): v for k, v in payload.items()}

    def pick(*names):
        for n in names:
            if low.get(n) not in (None, ''):
                return low[n]
        return None

    # The common Android forwarders use different field names; accept the
    # usual ones so a handset profile does not need rewriting.
    sender = pick('sender', 'from', 'originatingaddress', 'address')
    message = pick('body', 'message', 'text', 'msg', 'content')
    phone_time = pick('time', 'date', 'timestamp', 'receivedat')
    if message is None:
        raise ApiError(400, 'No message text in the request body.',
                       hint='JSON body {"sender": "...", "time": ..., "body": "..."}',
                       keysReceived=sorted(payload))
    message = str(message)
    if len(message) > MAX_MESSAGE:
        raise ApiError(400, 'Message is too long.', maximum=MAX_MESSAGE, received=len(message))

    item = store(number, raw_number, sender, message, False, phone_time)
    now = int(item['SavedTime'])
    # Mark this number as a capture handset: mocks are refused for it from now on.
    table().put_item(Item={'Service': number, 'Received': '!real', 'LastReal': now,
                           'ttl': now + 30 * 86400})
    return respond(200, {'saved': True, 'number': number, 'mocked': False,
                         'sender': item.get('Sender'), 'receivedTime': now,
                         'handsetTime': item.get('TimeUtc'), 'bytes': len(message.encode()),
                         'retentionDays': REAL_RETENTION_DAYS})


# ------------------------------------------------------------------ get
def do_get(req):
    number, _ = number_param(req)
    wait_s = req.integer('wait', default=0, lo=0, hi=WAIT_MAX_S)
    real_only = req.boolean('real', False)
    since = req.number('since')
    started = time.time()

    # Without since, a wait looks only for messages that arrive after the call
    # starts, so a stale code from an earlier run can never satisfy it. since
    # anchors that window earlier - capture it just before triggering the text.
    since_ms = None
    if since is not None:
        since_ms = int(since if since > 1e11 else since * 1000)
    elif wait_s:
        since_ms = int(started * 1000)
    floor = ('%015d' % since_ms) if since_ms is not None else None

    deadline = started + wait_s
    delay = POLL_START_S
    while True:
        hit = _query_newest(number, real_only, floor)
        left = deadline - time.time()
        if hit or left <= 0:
            break
        time.sleep(min(delay, left))
        delay = min(POLL_MAX_S, delay * 1.5)

    now = int(time.time())
    waited = int((time.time() - started) * 1000)
    if hit:
        out = {'found': True}
        out.update(shape(hit, now))
        out['waitedMs'] = waited
        return respond(200, out)

    out = {'number': number, 'found': False, 'body': None, 'waitedMs': waited}
    newest = _query_newest(number, real_only)
    if newest is None:
        out['comment'] = ('no real (non-mocked) messages for this number' if real_only
                          else 'no messages have been received for this number')
    else:
        # Only reachable when a floor was set (wait or since): say how stale
        # the newest message is, so "quiet" and "never used" are distinguishable.
        out['newestAgeSeconds'] = max(0, now - int(newest.get('SavedTime', 0)))
        out['comment'] = ('nothing newer arrived%s; the newest message for this number is %d s old'
                          % (' within the wait' if wait_s else '', out['newestAgeSeconds']))
    return respond(200, out)


# ------------------------------------------------------------------ list
def do_list(req):
    window = req.integer('seconds', default=DEFAULT_WINDOW_S, lo=1, hi=MAX_WINDOW_S)
    number, _ = number_param(req, required=False)
    real_only = req.boolean('real', False)
    now = int(time.time())
    lo = '%015d' % int((time.time() - window) * 1000)

    items, truncated = [], False
    if number:
        kw = {'KeyConditionExpression': Key('Service').eq(number) & Key('Received').gte(lo),
              'ScanIndexForward': False}
        sources = [kw]
    else:
        sources = [{'IndexName': DAY_INDEX, 'ScanIndexForward': False,
                    'KeyConditionExpression': Key('Day').eq(d) & Key('Received').gte(lo)}
                   for d in sorted({day_of(now), day_of(now - window)})]
    for kw in sources:
        while True:
            page = table().query(**kw)
            items.extend(page.get('Items') or [])
            if len(items) >= MAX_RESULTS * 2:
                truncated = True
                break
            lek = page.get('LastEvaluatedKey')
            if not lek:
                break
            kw['ExclusiveStartKey'] = lek
        if truncated:
            break

    rows = [shape(it, now) for it in items
            if int(it.get('SavedTime', 0)) >= now - window
            and not (real_only and it.get('Mocked'))]
    rows.sort(key=lambda x: x['seq'] or '', reverse=True)
    if len(rows) > MAX_RESULTS:
        rows, truncated = rows[:MAX_RESULTS], True
    size, kept = 0, 0
    for r in rows:
        size += len(json.dumps(r, default=str)) + 2
        if size > MAX_LIST_BYTES:
            break
        kept += 1
    if kept < len(rows):
        rows, truncated = rows[:kept], True
    out = {'seconds': window, 'sinceTime': now - window, 'untilTime': now,
           'count': len(rows), 'numbers': sorted({r['number'] for r in rows if r['number']}),
           'messages': rows}
    if number:
        out['number'] = number
    if truncated:
        out['truncated'] = True
        out['comment'] = ('more messages matched than one response holds (max %d, or about 5 MB); '
                          'these are the newest - narrow seconds= or add number= for the rest'
                          % MAX_RESULTS)
    return respond(200, out)


# ------------------------------------------------------------------ send
def _code_and_body(req):
    code = req.text('code')
    if code:
        if not CODE_RE.match(code):
            raise ApiError(400, "'code' must be 1-12 letters or digits.", received=code)
    else:
        code = ''.join(secrets.choice('0123456789') for _ in range(CODE_LEN))
    template = req.text('body') or DEFAULT_TEMPLATE
    body = template.replace('{code}', code)
    if len(body) > MAX_SEND_CHARS:
        raise ApiError(400, 'Message is too long.', maximum=MAX_SEND_CHARS, received=len(body))
    return code, body


def _url(event, action, params):
    return f'{base_url(event)}/sms/{action}?' + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in (None, '')}, quote_via=urllib.parse.quote)


def do_send(req):
    number, raw_number = number_param(req)
    code, body = _code_and_body(req)
    dest = registered().get(number)
    if not dest:
        raise ApiError(
            403, f'{raw_number} is not a registered handset - no text was sent.',
            number=number,
            hint='Real texts only go to handsets registered on this deployment. Use '
                 'sms/mocksend to simulate the message: sms/get then returns it with mocked:true.',
            mockUrl=_url(req.event, 'mocksend', {'number': raw_number, 'code': code,
                                                 'body': req.text('body')}))
    throttle(number, '!send', send_interval())
    try:
        res = sns().publish(
            PhoneNumber=dest, Message=body,
            # Transactional takes the higher-reliability path a one-time code needs.
            MessageAttributes={'AWS.SNS.SMS.SMSType':
                               {'DataType': 'String', 'StringValue': 'Transactional'}})
    except ClientError as e:
        raise ApiError(502, 'SNS would not accept the message.', detail=str(e), number=number,
                       hint='if SNS says the number is not verified, SMS in the configured '
                            'region is still in the SNS sandbox')
    sent = int(time.time())
    return respond(200, {
        'sent': True, 'mocked': False, 'number': number, 'to': dest, 'code': code,
        'codeInBody': code in body, 'body': body, 'messageId': res.get('MessageId'),
        'sentTime': sent,
        'getUrl': _url(req.event, 'get', {'number': number, 'wait': 20, 'since': sent,
                                          'real': 'true'}),
    })


def do_mocksend(req):
    number, raw_number = number_param(req)
    code, body = _code_and_body(req)
    if number in registered() or is_capture_handset(number):
        raise ApiError(
            403, f'{raw_number} is a registered handset - mock messages are not allowed on it.',
            number=number,
            hint='A registered handset only ever holds texts it really received. Use sms/send '
                 'for a real text, or mock to a number that is not a real handset.',
            sendUrl=_url(req.event, 'send', {'number': raw_number, 'code': code,
                                             'body': req.text('body')}))
    throttle(number, '!mock', MOCK_INTERVAL_S)
    sender = req.text('from') or MOCK_SENDER
    item = store(number, raw_number, sender, body, True)
    sent = int(item['SavedTime'])
    return respond(200, {
        'sent': True, 'mocked': True, 'number': number, 'code': code,
        'codeInBody': code in body, 'body': body, 'sender': sender, 'sentTime': sent,
        'retentionDays': MOCK_RETENTION_DAYS,
        'getUrl': _url(req.event, 'get', {'number': number, 'since': sent - 1}),
    })


ROUTES = {'receive': do_receive, 'get': do_get, 'list': do_list,
          'send': do_send, 'mocksend': do_mocksend}


def handler(event, context):
    return run(event, SERVICE, ROUTES, index={
        'description': 'Text messages: catch real texts posted by capture handsets, send real '
                       'texts to registered handsets, or simulate texts to any other number.'})
