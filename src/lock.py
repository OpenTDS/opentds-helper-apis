"""lock - short-lived mutual exclusion between concurrent test scripts (v1).

GET /v1/lock/acquire?key=K[&wait=0..20]  -> locked true/false, ref
GET /v1/lock/release?ref=REF             -> released true/false, reason

Serialises access to a shared resource that only one script may use at a
time (a test account, a device, a single-use inbox, a rate-limited sandbox).

Table (env LOCK_TABLE): one item per key, partition key LockKey (S).
Attributes: LockId (the current ref), ExpiresAt, ClaimedAt, GcAt (TTL).
"""
import os
import random
import secrets
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError

from common import KEY_RE, ApiError, base_url, respond, run

SERVICE = 'lock'

# A lock can NEVER be held longer than this. It is not a default and the
# caller cannot raise it. The whole safety story rests on it: a script that
# crashes, hangs, or simply never calls release costs everyone else at most
# one minute. If whatever you hold the lock for has not happened inside that,
# the system under test has a real problem and the script SHOULD fail.
MAX_HOLD_S = 60
# API Gateway abandons the request at 29s, so a held wait cannot exceed that.
WAIT_MAX_S = 20
# Waiters poll - there is no push in Lambda - so a release is picked up within
# roughly a quarter second. The jitter stops several waiters waking in
# lockstep and colliding on the same conditional write every time.
POLL_MIN_S = 0.20
POLL_MAX_S = 0.35
# Rows are reused per key, so this only matters if someone locks on a one-off
# key. Housekeeping only: the lease itself is enforced by ExpiresAt below.
GC_AFTER_S = 86400

# ':' joins the key to the random half of a ref. v1 keys (common KEY_RE) may
# themselves contain ':', but the random half is lower-case hex and never
# does, so the ref is split on its LAST ':' and the key comes back
# unambiguously. That is what lets lock/release take a ref alone and still go
# straight to the right partition - a secondary index would be eventually
# consistent, and a release arriving milliseconds after its lock could miss
# the row entirely.
SEP = ':'

_ddb = None


def _client():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client('dynamodb')
    return _ddb


def _table():
    return os.environ['LOCK_TABLE']


def _try_claim(key):
    """One atomic attempt. Returns (ref, expiresTime) or None if held."""
    now = int(time.time())
    ref = key + SEP + secrets.token_hex(4)
    expires = now + MAX_HOLD_S
    try:
        _client().update_item(
            TableName=_table(),
            Key={'LockKey': {'S': key}},
            UpdateExpression=('SET LockId = :id, ExpiresAt = :exp, '
                              'ClaimedAt = :now, GcAt = :gc'),
            # Free means never claimed, or the previous hold has run out. This
            # comparison is what enforces the lease - NOT the table's TTL,
            # which DynamoDB applies lazily and can lag by up to 48 hours.
            ConditionExpression='attribute_not_exists(LockKey) OR ExpiresAt <= :now',
            ExpressionAttributeValues={':id': {'S': ref},
                                       ':exp': {'N': str(expires)},
                                       ':now': {'N': str(now)},
                                       ':gc': {'N': str(now + GC_AFTER_S)}})
        return ref, expires
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return None
        raise


def _read(key):
    return (_client().get_item(TableName=_table(), Key={'LockKey': {'S': key}},
                               ConsistentRead=True).get('Item') or {})


def _release_url(req, ref):
    return base_url(req.event) + '/lock/release?ref=' + urllib.parse.quote(ref, safe='')


def do_acquire(req):
    if req.text('key') is None:
        raise ApiError(400, "Missing required query parameter 'key'.",
                       hint='the key is whatever the scripts agree to contend on - '
                            'usually an identifier of the shared resource, e.g. '
                            'key=test-account-07')
    key = req.key()
    wait_s = req.integer('wait', 0, 0, WAIT_MAX_S)

    started = time.time()
    deadline = started + wait_s
    while True:
        got = _try_claim(key)
        if got:
            ref, expires = got
            url = _release_url(req, ref)
            return respond(200, {'locked': True,
                                 'key': key,
                                 'ref': ref,
                                 'maxSeconds': MAX_HOLD_S,
                                 'expiresTime': expires,
                                 'waitedMs': int((time.time() - started) * 1000),
                                 'releaseUrl': url,
                                 'comment': 'release it with ' + url})
        now = time.time()
        if now >= deadline:
            held = _read(key)
            free_in = max(0, int(held.get('ExpiresAt', {}).get('N', 0)) - int(now))
            # 200, not 409, on purpose: newman and DevWeb treat a non-2xx as a
            # failed request by default, and a busy lock is not a test failure.
            # Branch on locked, not on the status code.
            return respond(200, {'locked': False,
                                 'key': key,
                                 'ref': None,
                                 'freeInSeconds': free_in,
                                 'waitedMs': int((now - started) * 1000),
                                 'comment': ('another script holds this lock; it frees in at '
                                             'most %d seconds. Call again, or pass '
                                             'wait=SECONDS (max %d) to hold the request '
                                             'open until it frees.' % (free_in, WAIT_MAX_S))})
        time.sleep(min(random.uniform(POLL_MIN_S, POLL_MAX_S), max(0.0, deadline - now)))


def do_release(req):
    ref = req.text('ref')
    if ref is None:
        raise ApiError(400, "Missing required query parameter 'ref'.",
                       hint='pass the ref that lock/acquire returned')
    if SEP not in ref:
        raise ApiError(400, "'ref' is not in the expected form key" + SEP + "code.",
                       received=ref, hint='pass the ref exactly as lock/acquire returned it, '
                                          'URL-encoded')
    key = ref.rsplit(SEP, 1)[0]
    if not KEY_RE.match(key):
        raise ApiError(400, "'ref' does not carry a usable key.", received=ref,
                       hint="a '+' in a ref must be sent as %2B")

    now = int(time.time())
    try:
        res = _client().update_item(
            TableName=_table(),
            Key={'LockKey': {'S': key}},
            # ExpiresAt 0 means free. LockId is removed as well, so releasing
            # the same ref twice reports honestly rather than succeeding.
            UpdateExpression='SET ExpiresAt = :zero REMOVE LockId',
            # Only the current holder may release. This is why the ref is
            # random: it is the proof of ownership, so a script whose lease
            # expired cannot release the lock someone else now holds.
            ConditionExpression='LockId = :id',
            ExpressionAttributeValues={':id': {'S': ref}, ':zero': {'N': '0'}},
            ReturnValues='ALL_OLD')
        claimed = int((res.get('Attributes') or {}).get('ClaimedAt', {}).get('N', now))
        return respond(200, {'released': True, 'key': key, 'ref': ref,
                             'heldSeconds': max(0, now - claimed)})
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise

    # Failure path only: one extra read so the answer says WHY, which is the
    # difference between a five-second diagnosis and a long afternoon.
    it = _read(key)
    current = it.get('LockId', {}).get('S')
    expires = int(it.get('ExpiresAt', {}).get('N', 0))
    if not it:
        reason = 'no lock has ever been taken on this key'
    elif current is None:
        reason = 'already released'
    elif expires > now:
        # Cannot tell an expired-and-retaken ref from one that was never
        # valid, so say both rather than assert the wrong one.
        reason = ('not the current holder - this key is held by another script for '
                  'another %d seconds; either your lock expired and was taken, or '
                  'this ref was never valid' % (expires - now))
    else:
        reason = 'not the current holder - the lock on this key has already expired'
    # 200, like a busy acquire: a lost lease is information, not a failed request.
    return respond(200, {'released': False, 'key': key, 'ref': ref, 'reason': reason})


ROUTES = {'acquire': do_acquire, 'release': do_release}
INDEX = {'description': 'Short-lived (60 s, no renew) mutual exclusion on a caller-chosen '
                        'key. Busy is 200 with locked:false.'}


def handler(event, context):
    return run(event, SERVICE, ROUTES, INDEX)
