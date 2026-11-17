"""totp - RFC 6238 one-time codes for MFA-protected test accounts.

  /v1/totp/set?key=K&user=U&secret=BASE32[&digits][&period][&algorithm]
  /v1/totp/get?key=K&user=U          codes from the stored secret
  /v1/totp/get?secret=BASE32         stateless: codes straight from a secret
"""
import base64
import hashlib
import hmac
import os
import struct
import time

import boto3

from common import ApiError, respond, run

TABLE = os.environ.get('TOTP_TABLE', 'totp')
ALGORITHMS = {'sha1': hashlib.sha1, 'sha256': hashlib.sha256, 'sha512': hashlib.sha512}

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource('dynamodb').Table(TABLE)
    return _table


def _secret_bytes(secret_b32):
    s = secret_b32.strip().replace(' ', '').replace('-', '').upper()
    s += '=' * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s, casefold=True)


def totp(secret_b32, when, period=30, digits=6, algorithm='sha1'):
    key = _secret_bytes(secret_b32)
    counter = int(when // period)
    mac = hmac.new(key, struct.pack('>Q', counter), ALGORITHMS[algorithm]).digest()
    off = mac[-1] & 0x0f
    binary = ((mac[off] & 0x7f) << 24 | (mac[off + 1] & 0xff) << 16 |
              (mac[off + 2] & 0xff) << 8 | (mac[off + 3] & 0xff))
    return str(binary % (10 ** digits)).zfill(digits)


def _options(req):
    digits = req.integer('digits', default=6)
    period = req.integer('period', default=30)
    algorithm = (req.text('algorithm', default='sha1') or 'sha1').lower().replace('-', '')
    if digits not in (6, 7, 8):
        raise ApiError(400, "'digits' must be 6, 7 or 8.", received=digits)
    if not 10 <= period <= 300:
        raise ApiError(400, "'period' must be between 10 and 300 seconds.", received=period)
    if algorithm not in ALGORITHMS:
        raise ApiError(400, "'algorithm' must be sha1, sha256 or sha512.",
                       received=algorithm)
    return digits, period, algorithm


def _validate_secret(secret):
    try:
        if not _secret_bytes(secret):
            raise ValueError('empty')
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, "'secret' is not a valid base32 TOTP secret.", detail=str(e))


def _codes(secret, digits, period, algorithm):
    now = time.time()
    return {
        'code': totp(secret, now, period, digits, algorithm),
        'nextCode': totp(secret, now + period, period, digits, algorithm),
        'previousCode': totp(secret, now - period, period, digits, algorithm),
        'secondsRemaining': round(period - (now % period), 2),
        'period': period,
        'digits': digits,
        'algorithm': algorithm,
    }


def set_secret(req):
    key = req.key()
    user = req.text('user', required=True)
    secret = req.text('secret', required=True)
    digits, period, algorithm = _options(req)
    _validate_secret(secret)
    now = int(time.time())
    table().put_item(Item={'key': key, 'user': user, 'secret': secret,
                           'digits': digits, 'period': period,
                           'algorithm': algorithm, 'savedTime': now})
    return respond(200, {'key': key, 'user': user, 'saved': True, 'savedTime': now,
                         'digits': digits, 'period': period, 'algorithm': algorithm})


def get_code(req):
    secret = req.text('secret')
    if secret and not req.text('key'):
        digits, period, algorithm = _options(req)
        _validate_secret(secret)
        return respond(200, _codes(secret, digits, period, algorithm))

    key = req.key()
    user = req.text('user', required=True)
    it = table().get_item(Key={'key': key, 'user': user}).get('Item')
    if not it:
        raise ApiError(404, 'No secret stored for this key and user.', key=key, user=user)
    out = {'key': key, 'user': user}
    out.update(_codes(it['secret'], int(it.get('digits', 6)), int(it.get('period', 30)),
                      it.get('algorithm', 'sha1')))
    return respond(200, out)


ROUTES = {'set': set_secret, 'get': get_code}


def handler(event, context):
    return run(event, 'totp', ROUTES,
               index={'description': 'Store TOTP secrets and generate RFC 6238 codes.'})
