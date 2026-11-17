"""mail/pop, mail/reset, mail/clear - the inbound mail catcher.

Storage (table MAIL_TABLE, hash EmailAddress S, range Serial N):
  Serial 0      header row: MessageCount (serials claimed so far) and
                NextMessage (next serial pop will hand out, 1-based)
  Serial 1..n   one row per received message, written by mail_receive:
                SavedTime, Subject, FromAddr, MessageText, S3Key,
                Attachments [{filename, ref, bytes}]
"""
import time

from boto3.dynamodb.conditions import Key

from common import ApiError, respond
import mail_store

# Server-side wait on pop. Opt-in, because making it the default would
# silently turn every caller's fast empty response into a held invocation.
# Capped at 20s: API Gateway gives up at 29, and the Lambda has to answer
# before it does.
WAIT_MAX_S = 20
POLL_START_S = 0.25
POLL_MAX_S = 1.0
# A poll that lands just after the previous one consumed a message would
# otherwise see an empty inbox. Re-offering the most recent message for a
# couple of seconds closes the gap between requests.
GRACE_DEFAULT_S = 2
GRACE_MAX_S = 60


def address(req):
    em = (req.text('address', required=True) or '').lower()
    if ' ' in em:
        raise ApiError(400, "'address' contains a space - a '+' in an address must be sent "
                            "as %2B (a bare + in a query string arrives as a space).",
                       received=em, hint=em.replace(' ', '%2B'))
    if '@' not in em or em.startswith('@') or em.endswith('@') or len(em) > 320:
        raise ApiError(400, "'address' must be an email address, e.g. signup@example.com.",
                       received=em)
    return em


def _t():
    return mail_store.table()


# A claimed serial whose row is missing while a LATER serial exists is either
# still being written (a concurrent receive with a slow attachment extraction)
# or was abandoned by a failed receive. It is only stepped over once it has
# been missing this long - comfortably past the receive function's timeout.
SKIP_MISSING_AFTER_S = 75


def _peek(em):
    """(header, next_index, count) for the address, or None if unknown."""
    # Consistent reads throughout: an eventually-consistent miss here would
    # look like a missing row and could step over a real message.
    h = _t().get_item(Key={'EmailAddress': em, 'Serial': 0},
                      ConsistentRead=True).get('Item')
    if not h:
        return None
    return h, int(h.get('NextMessage', 1)), int(h.get('MessageCount', 0))


def _row(em, serial):
    return _t().get_item(Key={'EmailAddress': em, 'Serial': serial},
                         ConsistentRead=True).get('Item')


def _missing_long_enough(em, h, nxt):
    """True once serial nxt has been seen missing for SKIP_MISSING_AFTER_S.

    The first sighting stamps MissingSerial/MissingSince on the header row
    (only while the pointer is still at nxt), so every poller - in any
    invocation - measures from the same moment."""
    now = int(time.time())
    if int(h.get('MissingSerial', 0)) == nxt:
        return now - int(h.get('MissingSince', now)) >= SKIP_MISSING_AFTER_S
    try:
        _t().update_item(Key={'EmailAddress': em, 'Serial': 0},
                         UpdateExpression='SET MissingSerial = :s, MissingSince = :t',
                         ConditionExpression='NextMessage = :s',
                         ExpressionAttributeValues={':s': nxt, ':t': now})
    except Exception:
        pass
    return False


def _take(em, nxt):
    """Advance the pointer past nxt, unless another caller got there first."""
    try:
        _t().update_item(Key={'EmailAddress': em, 'Serial': 0},
                         UpdateExpression='SET NextMessage = :n',
                         ConditionExpression='NextMessage = :c',
                         ExpressionAttributeValues={':n': nxt + 1, ':c': nxt})
        return True
    except Exception:
        return False


def _attachments(it):
    out = []
    for a in it.get('Attachments') or []:
        out.append({'filename': a.get('filename', ''),
                    'ref': a.get('ref', ''),
                    'bytes': int(a.get('bytes', 0))})
    return out


def _delivered(em, serial, it, remaining, waited, repeat=False):
    saved = int(it.get('SavedTime', 0))
    return respond(200, {
        'address': em,
        'exhausted': False,
        'repeat': repeat,
        'serial': serial,
        'remaining': remaining,
        'waitedMs': waited,
        'receivedTime': saved,
        'ageSeconds': max(0, int(time.time()) - saved),
        'subject': it.get('Subject', ''),
        'from': it.get('FromAddr', ''),
        'body': it.get('MessageText', ''),
        'bodyTruncated': bool(it.get('Truncated', False)),
        'attachments': _attachments(it),
    })


def _empty(em, waited, comment, pending=False):
    out = {'address': em, 'exhausted': True, 'body': None, 'remaining': 0,
           'waitedMs': waited, 'comment': comment}
    if pending:
        out['pending'] = True
    return respond(200, out)


def get_next(em, wait_s=0, grace_s=0):
    started = time.monotonic()
    deadline = started + max(0, min(WAIT_MAX_S, wait_s))
    delay = POLL_START_S
    pending = False

    # The bound is only there to stop a pathological loop; every continue
    # below has already made progress on the pointer.
    for _ in range(10000):
        peek = _peek(em)
        pending = False
        if peek:
            h, nxt, cnt = peek
            if nxt <= cnt:
                it = _row(em, nxt)
                if it:
                    if _take(em, nxt):
                        return _delivered(
                            em, nxt, it, cnt - nxt,
                            int((time.monotonic() - started) * 1000))
                    continue          # someone else took it - try the next
                if cnt > nxt and _missing_long_enough(em, h, nxt):
                    # A later serial exists and this row has been missing for
                    # longer than any receive can take: it was claimed and
                    # never written. Step over it rather than blocking the
                    # queue behind it forever.
                    _take(em, nxt)
                    continue
                # No row yet: the receive function is still writing it
                # (attachment extraction sits between claim and write).
                # Waiting is right - consuming it here would lose the email.
                pending = True
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(delay, left))
        delay = min(POLL_MAX_S, delay * 1.5)

    waited = int((time.monotonic() - started) * 1000)
    peek = _peek(em)
    now = int(time.time())

    # Nothing unread. Offer the most recently delivered message again if it
    # is younger than the grace window, so a poll that lands in the gap
    # between two requests still sees it. Always flagged repeat:true so a
    # caller can tell a re-offer from a fresh delivery.
    if peek and grace_s > 0 and peek[2] >= 1:
        it = _row(em, peek[2])
        if it and now - int(it.get('SavedTime', 0)) <= grace_s:
            return _delivered(em, peek[2], it, 0, waited, repeat=True)

    if pending:
        return _empty(em, waited, 'a message is arriving but is not readable yet; '
                                  'call again - it has NOT been consumed', pending=True)
    if not peek:
        return _empty(em, waited, 'no messages for this address')
    return _empty(em, waited, 'no more messages')


def pop(req):
    em = address(req)
    w = req.integer('wait', 0, lo=0, hi=WAIT_MAX_S)
    # Grace defaults on only when waiting, because that is the polling case
    # it exists for. Leaving it off for wait=0 keeps a plain drain loop
    # terminating exactly as it always has.
    g = req.integer('grace', None, lo=0, hi=GRACE_MAX_S)
    if g is None:
        g = GRACE_DEFAULT_S if w else 0
    return get_next(em, w, g)


def reset(req):
    em = address(req)
    try:
        _t().update_item(Key={'EmailAddress': em, 'Serial': 0},
                         UpdateExpression='SET NextMessage = :one',
                         ConditionExpression='attribute_exists(Serial)',
                         ExpressionAttributeValues={':one': 1})
        return respond(200, {'address': em, 'reset': True,
                             'comment': 'pointer reset to first message'})
    except Exception:
        return respond(200, {'address': em, 'reset': False,
                             'comment': 'nothing to reset'})


def clear(req):
    em = address(req)
    n = 0
    t = _t()
    kw = {'KeyConditionExpression': Key('EmailAddress').eq(em),
          'ProjectionExpression': 'Serial'}
    resp = t.query(**kw)
    with t.batch_writer() as bw:
        while True:
            for i in resp['Items']:
                bw.delete_item(Key={'EmailAddress': em, 'Serial': i['Serial']})
                if int(i['Serial']) != 0:
                    n += 1
            lek = resp.get('LastEvaluatedKey')
            if not lek:
                break
            resp = t.query(ExclusiveStartKey=lek, **kw)
    return respond(200, {'address': em, 'deleted': n,
                         'comment': 'all messages cleared'})
