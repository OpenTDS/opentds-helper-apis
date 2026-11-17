"""webhook - capture inbound webhooks so a test can assert on them.

  /v1/webhook/receive?key=K[&status=N]   any method; stores the whole request
  /v1/webhook/list?key=K[&since][&wait][&limit]   newest first
  /v1/webhook/clear?key=K

Storage: one item per captured request, partition key = the namespace key,
sort key = seq ("<15-digit epoch ms>-<7 random digits>", so a Query in
reverse is newest first). Items expire after RETENTION_DAYS via TTL.
"""
import json
import os
import secrets
import time

import boto3
from boto3.dynamodb.conditions import Key

from common import ApiError, respond, run

TABLE = os.environ.get('WEBHOOK_TABLE', 'webhook')
RETENTION_DAYS = int(os.environ.get('WEBHOOK_RETENTION_DAYS', '30') or 30)
MAX_BODY_BYTES = 300 * 1024        # DynamoDB items stop at 400 KB
WAIT_MAX_S = 20
LIST_DEFAULT = 50
LIST_MAX = 500
# Parameters that steer the catcher itself rather than being part of the
# captured request.
CONTROL_PARAMS = ('key', 'status')

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource('dynamodb').Table(TABLE)
    return _table


def _seq(now_ms):
    return f'{now_ms:015d}-{secrets.randbelow(10_000_000):07d}'


def receive(req):
    key = req.key()
    status = req.integer('status', default=200, lo=200, hi=599)
    now_ms = int(time.time() * 1000)

    body_bytes = req.body_bytes() if req.body is not None else b''
    item = {
        'key': key,
        'seq': _seq(now_ms),
        'receivedTime': now_ms // 1000,
        'receivedTimeMs': now_ms,
        'method': req.method,
        # Raw names: captured data is returned exactly as sent.
        'query': {k: v for k, v in (req.event.get('queryStringParameters') or {}).items()
                  if str(k).lower() not in CONTROL_PARAMS},
        'headers': dict(req.headers),
        'sourceIp': req.source_ip or '',
        'contentType': req.headers.get('content-type', ''),
        'bytes': len(body_bytes),
        'ttl': now_ms // 1000 + RETENTION_DAYS * 86400,
    }
    # Budget in BYTES (DynamoDB counts UTF-8 bytes), less what the headers
    # and query string already use.
    budget = MAX_BODY_BYTES - len(json.dumps(item['headers'])) - len(json.dumps(item['query']))
    budget = max(4096, budget)
    try:
        text = body_bytes.decode('utf-8')
        if len(body_bytes) > budget:
            item['body'] = body_bytes[:budget].decode('utf-8', 'ignore')
            item['bodyTruncated'] = True
        else:
            item['body'] = text
            # Parsed JSON is stored as a string and re-parsed on read, so
            # numbers keep their exact form and DynamoDB's float rules
            # never apply.
            if text.strip()[:1] in ('{', '['):
                try:
                    json.loads(text)
                    item['isJson'] = True
                except ValueError:
                    pass
    except UnicodeDecodeError:
        import base64
        enc = base64.b64encode(body_bytes[:budget * 3 // 4]).decode('ascii')
        item['bodyBase64'] = enc
        if len(body_bytes) > budget * 3 // 4:
            item['bodyTruncated'] = True

    table().put_item(Item=item)
    if status in (204, 304):
        from common import raw
        return raw(status, '', 'text/plain')
    return respond(status, {'received': True, 'key': key, 'seq': item['seq'],
                            'receivedTime': item['receivedTime'], 'status': status})


def _present(it):
    out = {k: v for k, v in it.items() if k not in ('ttl', 'isJson')}
    if it.get('isJson'):
        try:
            out['json'] = json.loads(it.get('body', ''))
        except ValueError:
            pass
    return out


def _query(key, since_ms, limit):
    cond = Key('key').eq(key)
    if since_ms is not None:
        cond = cond & Key('seq').gte(f'{since_ms:015d}')
    items, lek = [], None
    while True:
        kw = {'KeyConditionExpression': cond, 'ScanIndexForward': False,
              'Limit': limit + 1 - len(items)}
        if lek:
            kw['ExclusiveStartKey'] = lek
        page = table().query(**kw)
        items.extend(page.get('Items', []))
        lek = page.get('LastEvaluatedKey')
        if not lek or len(items) > limit:
            break
    return items[:limit], len(items) > limit


def list_events(req):
    key = req.key()
    limit = req.integer('limit', default=LIST_DEFAULT, lo=1, hi=LIST_MAX)
    wait = req.integer('wait', default=0, lo=0, hi=WAIT_MAX_S)
    since = req.number('since')
    since_ms = None
    if since is not None:
        # Accept seconds or milliseconds, like every other window parameter.
        since_ms = int(since if since > 1e11 else since * 1000)

    started = time.monotonic()
    deadline = started + wait
    delay = 0.25
    while True:
        items, more = _query(key, since_ms, limit)
        left = deadline - time.monotonic()
        if items or left <= 0:
            break
        time.sleep(min(delay, left))
        delay = min(1.0, delay * 1.5)

    return respond(200, {
        'key': key,
        'count': len(items),
        'more': more,
        'waitedMs': int((time.monotonic() - started) * 1000),
        'events': [_present(i) for i in items],
    })


def clear(req):
    key = req.key()
    deleted, lek = 0, None
    with table().batch_writer() as bw:
        while True:
            kw = {'KeyConditionExpression': Key('key').eq(key),
                  'ProjectionExpression': '#k, seq',
                  'ExpressionAttributeNames': {'#k': 'key'}}
            if lek:
                kw['ExclusiveStartKey'] = lek
            page = table().query(**kw)
            for it in page.get('Items', []):
                bw.delete_item(Key={'key': it['key'], 'seq': it['seq']})
                deleted += 1
            lek = page.get('LastEvaluatedKey')
            if not lek:
                break
    return respond(200, {'key': key, 'deleted': deleted})


ROUTES = {'receive': receive, 'list': list_events, 'clear': clear}


def handler(event, context):
    return run(event, 'webhook', ROUTES,
               index={'description': 'Capture inbound webhooks and read them back.'})
