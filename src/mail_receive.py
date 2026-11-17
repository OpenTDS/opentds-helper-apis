"""S3-event handler: turn SES-delivered raw mail into mail/pop rows.

SES's receipt rule writes every inbound message to INBOUND_BUCKET under
`inbound/`; that prefix's s3:ObjectCreated notification invokes this
function (handler `mail_receive.handler`). For each object it:

1. parses the MIME message and picks the recipient address (the first
   To/Cc/Delivered-To/X-Original-To address at MAIL_DOMAIN, else the first
   address found);
2. atomically claims the next serial for that address on the header row;
3. extracts attachments into the same bucket under `attachments/<ref>`
   (7-day lifecycle), where `ref` is an opaque 10-character token;
4. writes the message row that mail/pop reads.

The claim happens BEFORE the row is written; mail/pop treats a claimed
serial with no row as "arriving" (pending) rather than consuming it.
"""
import email
import re
import secrets
import time
from email.header import decode_header, make_header
from email.utils import getaddresses
from urllib.parse import unquote_plus

import mail_store


def _text(m):
    if m.is_multipart():
        for want in ('text/plain', 'text/html'):
            for p in m.walk():
                disp = str(p.get('Content-Disposition', ''))
                if p.get_content_type() == want and 'attachment' not in disp:
                    try:
                        cs = p.get_content_charset() or 'utf-8'
                        return p.get_payload(decode=True).decode(cs, 'replace')
                    except Exception:
                        pass
        return ''
    try:
        cs = m.get_content_charset() or 'utf-8'
        return m.get_payload(decode=True).decode(cs, 'replace')
    except Exception:
        return m.get_payload()


def _atts(m, bucket):
    # Extract attachment parts (anything with a filename, or an explicit
    # attachment disposition) and store the raw decoded bytes. The
    # API-visible ref is an opaque 10-char token; the S3 object lives at
    # attachments/<token> so the 7-day lifecycle rule applies. The original
    # (sanitised) filename rides along as S3 metadata so send and get can
    # default to it.
    s3 = mail_store.s3()
    out = []
    idx = 0
    for p in m.walk():
        if p.is_multipart():
            continue
        fname = p.get_filename()
        disp = str(p.get('Content-Disposition', '')).lower()
        if not fname and 'attachment' not in disp:
            continue
        data = p.get_payload(decode=True)
        if data is None:
            continue
        idx += 1
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', fname or '').lstrip('._-') or ('part%d.bin' % idx)
        ref = ''.join(secrets.choice(mail_store.REF_ALPHABET) for _ in range(10))
        ctype = p.get_content_type() or 'application/octet-stream'
        s3.put_object(Bucket=bucket, Key=mail_store.RECEIVED_PREFIX + ref, Body=data,
                      ContentType=ctype, Metadata={'filename': safe})
        out.append({'filename': fname or safe, 'ref': ref, 'bytes': len(data)})
    return out


def _recipient(m, domain):
    hdrs = []
    for h in ('To', 'Cc', 'Delivered-To', 'X-Original-To'):
        hdrs += [str(v) for v in m.get_all(h, [])]
    addrs = [a.lower() for _, a in getaddresses(hdrs) if a]
    if domain:
        for a in addrs:
            if a.endswith('@' + domain):
                return a
    return addrs[0] if addrs else 'unknown'


def _header(m, name):
    # Decode RFC 2047 encoded-words (=?utf-8?b?...?=) so a non-ASCII
    # subject reads as text; fall back to the raw header if it is malformed.
    v = m.get(name)
    if v is None:
        return ''
    try:
        return str(make_header(decode_header(str(v))))
    except Exception:
        return str(v)


# DynamoDB items stop at 400 KB. A bigger body would make the row write fail
# AFTER the serial was claimed, so it is clipped (and flagged) instead.
MAX_TEXT_BYTES = 300 * 1024


def _clip(text, max_bytes):
    b = text.encode('utf-8')
    if len(b) <= max_bytes:
        return text, False
    return b[:max_bytes].decode('utf-8', 'ignore'), True


def handler(event, context):
    s3 = mail_store.s3()
    t = mail_store.table()
    domain = mail_store.mail_domain()
    now = int(time.time())
    stored = 0
    for rec in event.get('Records', []):
        b = rec['s3']['bucket']['name']
        # S3 event keys are URL-encoded (space as '+').
        k = unquote_plus(rec['s3']['object']['key'])
        raw = s3.get_object(Bucket=b, Key=k)['Body'].read()
        m = email.message_from_bytes(raw)
        rcpt = _recipient(m, domain)
        # Extract attachments BEFORE claiming a serial, so the gap between
        # the claim and the row write (which pop has to wait out) is tiny.
        atts = _atts(m, b)
        # Atomically claim the next serial; init NextMessage=1 on first hit.
        res = t.update_item(
            Key={'EmailAddress': rcpt, 'Serial': 0},
            UpdateExpression='SET NextMessage = if_not_exists(NextMessage, :one) '
                             'ADD MessageCount :one',
            ExpressionAttributeValues={':one': 1},
            ReturnValues='UPDATED_NEW')
        serial = int(res['Attributes']['MessageCount'])
        text, cut = _clip(_text(m) or '', MAX_TEXT_BYTES)
        item = {
            'EmailAddress': rcpt, 'Serial': serial, 'SavedTime': now,
            'Subject': _clip(_header(m, 'Subject'), 2048)[0],
            'FromAddr': _clip(_header(m, 'From'), 2048)[0],
            'MessageText': text, 'S3Key': k}
        if cut:
            item['Truncated'] = True
        if atts:
            item['Attachments'] = atts
        t.put_item(Item=item)
        stored += 1
        print('stored', rcpt, serial, len(atts), 'attachment(s)')
    return {'stored': stored}
