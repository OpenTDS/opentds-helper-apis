"""Shared configuration, AWS clients and attachment-store helpers for mail.

Two S3 stores back the mail service:

* the SAVED store (ATTACHMENT_BUCKET): files a test keeps, addressed by a
  caller-chosen `key` which is also the S3 object key. No expiry.
* the RECEIVED store: attachments extracted from inbound mail by
  mail_receive, in INBOUND_BUCKET under `attachments/<ref>`, where `ref` is an
  opaque 10-character token handed out by mail/pop. The bucket's 7-day
  lifecycle rule on `attachments/` expires them.

Clients are created lazily so that tests (moto) and cold starts both work.
"""
import os
import re
import time

import boto3
from botocore.config import Config

from common import ApiError

# Opaque received-attachment ref issued by mail_receive (10 chars today;
# 8-12 accepted so the format can change without breaking validation).
REF_RE = re.compile(r'^[A-Za-z0-9]{8,12}$')
REF_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'
RECEIVED_PREFIX = 'attachments/'
REF_EXPIRY_DAYS = 7

PRESIGN_TTL = 900     # seconds; well inside the Lambda role's credential lifetime

_clients = {}


def env(name, default=''):
    return os.environ.get(name, default)


def mail_domain():
    return env('MAIL_DOMAIN').strip().lower().lstrip('@')


def inbound_bucket():
    return env('INBOUND_BUCKET')


def attachment_bucket():
    return env('ATTACHMENT_BUCKET')


def table():
    if 'table' not in _clients:
        _clients['table'] = boto3.resource('dynamodb').Table(env('MAIL_TABLE'))
    return _clients['table']


def s3():
    # SigV4 is pinned explicitly: presigned POST policies are rejected by
    # S3 if the client falls back to an older signature version.
    #
    # The endpoint is pinned to the REGIONAL host as well, and that matters
    # for presigning: the default virtual-hosted host for a client built
    # without one is <bucket>.s3.amazonaws.com, and S3 answers a bucket
    # outside us-east-1 there with a 307 redirect to the regional host. A
    # browser will not follow a cross-origin redirect on an upload POST -
    # it aborts, and the page sees an opaque network/CORS failure. Signing
    # is region-scoped too, so the redirected request would not verify
    # anyway. AWS_REGION is set by the Lambda runtime.
    if 's3' not in _clients:
        region = env('AWS_REGION') or env('AWS_DEFAULT_REGION')
        _clients['s3'] = boto3.client(
            's3',
            region_name=region or None,
            endpoint_url=('https://s3.%s.amazonaws.com' % region) if region else None,
            config=Config(signature_version='s3v4',
                          s3={'addressing_style': 'virtual'}))
    return _clients['s3']


def ses():
    if 'ses' not in _clients:
        _clients['ses'] = boto3.client('ses')
    return _clients['ses']


def sesv2():
    if 'sesv2' not in _clients:
        _clients['sesv2'] = boto3.client('sesv2')
    return _clients['sesv2']


def reset_clients():
    """For tests: drop cached clients so the next call builds fresh ones."""
    _clients.clear()


# ---------------------------------------------------------------- refs ----
def check_ref(ref):
    if not REF_RE.match(ref or ''):
        raise ApiError(400, "'ref' must be the opaque attachment ref returned by mail/pop or "
                            "mail/attachments/make "
                            "(letters and digits, 8-12 characters).", received=ref)
    return ref


def received_key(ref):
    return RECEIVED_PREFIX + check_ref(ref)


def ref_not_found(ref):
    return ApiError(404, 'Attachment not found (refs expire %d days after they were '
                         'issued).' % REF_EXPIRY_DAYS, ref=ref)


def key_not_found(key):
    return ApiError(404, 'Saved attachment not found.', key=key,
                    hint='save it first with mail/attachments/save or mail/attachments/upload/url')


# ----------------------------------------------------------- S3 access ----
def head(bucket, s3key):
    # head_object raises ClientError (404), not NoSuchKey - callers want a
    # single 'missing' signal, so normalise to None.
    try:
        return s3().head_object(Bucket=bucket, Key=s3key)
    except Exception:
        return None


def _get(bucket, s3key):
    try:
        return s3().get_object(Bucket=bucket, Key=s3key)
    except s3().exceptions.NoSuchKey:
        return None


def fetch_saved(key):
    """(bytes, filename, content_type) from the saved store, or 404."""
    obj = _get(attachment_bucket(), key)
    if obj is None:
        raise key_not_found(key)
    meta = obj.get('Metadata') or {}
    return obj['Body'].read(), meta.get('filename') or key, obj.get('ContentType')


def fetch_received(ref):
    """(bytes, original filename, content_type) from the received store, or 404."""
    obj = _get(inbound_bucket(), received_key(ref))
    if obj is None:
        raise ref_not_found(ref)
    meta = obj.get('Metadata') or {}
    return obj['Body'].read(), meta.get('filename') or ref, obj.get('ContentType')


def presigned_get(bucket, s3key, filename='', disposition='attachment', content_type=None):
    # A Lambda proxy RESPONSE is capped at 6 MB, so large objects come back
    # as a short-lived direct-from-S3 URL instead of inline base64.
    # disposition 'inline' lets a browser show the file (a PDF opens in a tab)
    # instead of saving it.
    params = {'Bucket': bucket, 'Key': s3key}
    if filename:
        params['ResponseContentDisposition'] = \
            '%s; filename="%s"' % (disposition, filename.replace('"', ''))
    if content_type:
        params['ResponseContentType'] = content_type
    return s3().generate_presigned_url('get_object', Params=params,
                                       ExpiresIn=PRESIGN_TTL)


def now():
    return int(time.time())
