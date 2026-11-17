"""mail/send - outbound email through SES.

Three ways in:
  GET  ?from&to&subject&body                    everything in the query string
  POST ?from&to&subject   (body = message)      any non-JSON Content-Type
  POST ?from&to&subject   Content-Type: application/json
       {"body": "...", "attachments": [{"filename": "...", "ref"|"key": "..."}]}

Query-string attachment (at most one source): `attachmentkb` (generated
text), `key` (saved attachment) or `ref` (received attachment, straight from
the temporary store - no promotion needed). `prepend64` / `overwrite64` /
`append64` splice bytes into whichever attachment that is (or into an empty
one when no source is given).
"""
import json
import mimetypes
import random
import secrets
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr

from common import ApiError, KEY_RE, KEY_RULE, b64decode, respond
import mail_store

# Two SES clients on purpose:
#   ses   (v1) - SendEmail for the plain no-attachment path
#   sesv2      - SendEmail/Raw for anything with attachments. The v1
#                SendRawEmail limit is 10 MB after base64 and is NOT
#                adjustable; v2 is 40 MB. Same ses:SendEmail IAM action.

# Raw bytes across all attachments on one message. SESv2 caps the message
# at 40 MB AFTER base64; base64 with MIME line breaks costs ~x1.37, so
# 28000 KB -> ~37.5 MB on the wire, leaving room for headers.
MAX_ATTACH_KB = 28000
# Synthetic ?attachmentkb= text is generated in-process, so it keeps a more
# modest ceiling - this is a CPU/memory cost, not a protocol limit.
MAX_GENERATED_KB = 6000

# SES will accept up to 40 MB, but the RECIPIENT is the real limit and it is
# much lower: Outlook.com and iCloud stop at 20 MB, Yahoo 25 MB, Microsoft
# 365 36 MB by default, Gmail 50 MB. Over the far end's cap the message is
# refused after SES has already accepted it, so the send returns 200 here and
# the mail simply never arrives - it comes back as a bounce to the
# Return-Path instead. Deliberately a WARNING and not a rejection: provoking
# that bounce is a legitimate thing to test. The warning names only the
# providers that would actually refuse THIS message.
DELIVERY_TIERS = (('Outlook.com', 20), ('iCloud', 20), ('Yahoo', 25),
                  ('Microsoft 365', 36), ('Gmail', 50))
DELIVERY_WARN_BYTES = 20 * 1024 * 1024
DELIVERABLE_RAW_KB = 18000     # raw attachment that still clears a 25 MB receiver

WORDS = ("the quick brown fox jumps over a lazy dog time year people way day man "
         "thing woman life child world school state family student group country "
         "problem hand part place case week company system program question work "
         "government number night point home water room mother area money story "
         "fact month lot right study book eye job word business issue side kind "
         "head house service friend father power hour game line end member law car "
         "city community name president team minute idea body information back "
         "parent face level office door health person art war history party result "
         "change morning reason research girl guy moment air teacher force "
         "education foot boy age policy process music market sense nation plan "
         "college interest death course experience effort").split()

INJECT = ('prepend64', 'overwrite64', 'append64')


def gen_text(n_bytes):
    out = []
    total = 0
    i = 0
    while total < n_bytes:
        w = random.choice(WORDS)
        out.append(w)
        total += len(w) + 1
        i += 1
        if i % 20 == 0:
            out.append('\n')
            total += 1
    s = ' '.join(out)
    return s[:n_bytes]   # ASCII words: 1 char == 1 byte, so this hits the size exactly


# ------------------------------------------------------------ allow-list ----
def _csv_env(name):
    # ALLOWED_TO: addresses; ALLOWED_DOMAINS: 'example.com', '@example.com'
    # or '*.example.com'. Comma-separated, case-insensitive.
    return set(a.strip().lower().lstrip('@')
               for a in mail_store.env(name).split(',') if a.strip())


def _domain_of(addr):
    return addr.rsplit('@', 1)[-1].lower() if '@' in addr else ''


def permitted_to(addr):
    """Recipient check. Permitted: the stack's own MAIL_DOMAIN (the catcher,
    so a send can be popped straight back), an exact ALLOWED_TO address, an
    ALLOWED_DOMAINS domain, or a subdomain of a '*.domain' entry."""
    dom = _domain_of(addr)
    if not dom:
        return False
    if dom == mail_store.mail_domain():
        return True
    if addr in _csv_env('ALLOWED_TO'):
        return True
    domains = _csv_env('ALLOWED_DOMAINS')
    if dom in domains:
        return True
    # '*.example.com' allows any subdomain at any depth, but not example.com
    # itself. The suffix check includes the leading dot, so
    # 'evilexample.com' does NOT match '*.example.com'.
    for entry in domains:
        if entry.startswith('*.') and dom.endswith(entry[1:]):
            return True
    return False


def permitted_from(addr):
    """The sender must be at the SES-verified domain identity (MAIL_DOMAIN) or
    a subdomain of it; SES verification of a domain covers its subdomains, and
    the function's IAM policy is scoped to exactly those identities."""
    dom = _domain_of(addr)
    md = mail_store.mail_domain()
    return bool(dom and md and (dom == md or dom.endswith('.' + md)))


# ------------------------------------------------------------- sources ----
def _saved(key):
    if not KEY_RE.match(key):
        raise ApiError(400, f"'key' must be {KEY_RULE}.", received=key)
    data, fname, _ct = mail_store.fetch_saved(key)
    return data, fname


def _received(ref):
    data, fname, _ct = mail_store.fetch_received(ref)
    return data, fname


def _query_body(req):
    # Taken as given (not trimmed): leading/trailing whitespace can matter
    # to the system under test.
    v = req.get('body')
    if v is None or str(v).strip() == '':
        raise ApiError(400, "Missing required query parameter 'body' "
                            "(or POST the message as the request body).")
    return str(v)


def _message_and_json(req):
    """(message, json_attachments or None, warning or None)."""
    if req.method != 'POST':
        return _query_body(req), None, None
    body = req.body_bytes().decode('utf-8', 'replace')
    ctype = str(req.headers.get('content-type') or '').lower()
    if 'application/json' in ctype:
        try:
            j = json.loads(body or '{}')
        except Exception:
            raise ApiError(400, 'Request body is not valid JSON (Content-Type: application/json was sent).')
        if not isinstance(j, dict):
            raise ApiError(400, "JSON body must be an object with optional 'body' and 'attachments'.")
        jb = j.get('body', '')
        message = jb if isinstance(jb, str) else json.dumps(jb)
        atts = j.get('attachments') or []
        if not isinstance(atts, list):
            raise ApiError(400, "'attachments' must be a list of {filename, ref} or {filename, key} objects.")
        if not message:
            message = req.get('body') or ''
        return message, atts, None
    warning = None
    # Guardrail: a body that LOOKS like structured JSON sent without the JSON
    # content type goes out as literal text, which is almost never what the
    # caller meant. Warn them.
    if body.lstrip().startswith('{'):
        try:
            maybe = json.loads(body)
            if isinstance(maybe, dict) and ('attachments' in maybe or 'body' in maybe):
                warning = ("request body looks like structured JSON but Content-Type was '"
                           + (ctype or 'not set')
                           + "'; it was sent as LITERAL message text. Send Content-Type: "
                           + "application/json to use body/attachments structured mode.")
        except Exception:
            pass
    if body == '':
        # An empty POST body falls back to the query-string 'body'.
        body = _query_body(req)
    return body, None, warning


def _json_attachments(items):
    atts, total = [], 0
    for a in items:
        if not isinstance(a, dict):
            raise ApiError(400, "Each attachment must be an object with 'ref' or 'key'.")
        ref = str(a.get('ref') or '').strip()
        key = str(a.get('key') or '').strip()
        if bool(ref) == bool(key):
            raise ApiError(400, "Each attachment needs exactly one of 'ref' (received attachment) "
                                "or 'key' (saved attachment).", received=a)
        data, default_name = _received(ref) if ref else _saved(key)
        fname = str(a.get('filename') or '').strip() or default_name
        total += len(data)
        atts.append((fname, data, True))
    if total > MAX_ATTACH_KB * 1024:
        raise ApiError(400, 'Total attachments exceed the cap.', maxKb=MAX_ATTACH_KB, bytes=total)
    return atts


def _query_attachment(req):
    key = req.text('key')
    ref = req.text('ref')
    kb = req.integer('attachmentkb', None, lo=1, hi=MAX_GENERATED_KB, clamp=False)
    given = [n for n, v in (('attachmentkb', kb), ('key', key), ('ref', ref)) if v is not None]
    if len(given) > 1:
        raise ApiError(400, 'attachmentkb, key and ref are mutually exclusive; pass one.',
                       received=given)
    inj = {}
    for name in INJECT:
        v = req.get(name)
        if v not in (None, ''):
            inj[name] = b64decode(v)
    if not given and not inj:
        return []

    source_name = None
    binary = False
    if key is not None:
        data, source_name = _saved(key)
        binary = True
    elif ref is not None:
        data, source_name = _received(ref)
        binary = True
    else:
        data = gen_text(kb * 1024).encode('utf-8') if kb else b''
    if 'prepend64' in inj:
        data = inj['prepend64'] + data
    if 'overwrite64' in inj:
        ins = inj['overwrite64']
        mid = len(data) // 2
        data = data[:mid] + ins + data[mid + len(ins):]   # overwrite near the middle
    if 'append64' in inj:
        data = data + inj['append64']
    if len(data) > MAX_ATTACH_KB * 1024:
        raise ApiError(400, 'Attachment exceeds the cap after injection.',
                       maxKb=MAX_ATTACH_KB, bytes=len(data))
    # Filename shown in the email: 'filename' wins, then the saved key or the
    # received attachment's original name, then a random name.
    name = req.text('filename')
    if name:
        fname = name if '.' in name else name + '.txt'
    elif source_name:
        fname = source_name
    else:
        fname = 'attachment-' + secrets.token_hex(4) + '.txt'
    return [(fname, data, binary)]


def send(req):
    frm = req.text('from', required=True)
    to = req.text('to', required=True).lower()
    subject = req.text('subject', required=True)
    from_addr = parseaddr(frm)[1] or frm
    md = mail_store.mail_domain()
    if not permitted_from(from_addr):
        raise ApiError(403, "'from' must be an address at this service's mail domain.",
                       received=frm, allowedFrom='*@' + md if md else None)
    if not permitted_to(to):
        raise ApiError(403, "'to' is not a permitted recipient.", received=to,
                       mailDomain=md,
                       allowedAddresses=sorted(_csv_env('ALLOWED_TO')),
                       allowedDomains=sorted(_csv_env('ALLOWED_DOMAINS')))

    message, json_atts, warning = _message_and_json(req)
    if json_atts:
        if any(req.get(n) not in (None, '') for n in ('key', 'ref', 'attachmentkb') + INJECT):
            raise ApiError(400, 'JSON attachments cannot be combined with the key, ref, '
                                'attachmentkb or *64 query parameters.')
        atts = _json_attachments(json_atts)
    else:
        atts = _query_attachment(req)

    try:
        if not atts:
            r = mail_store.ses().send_email(
                Source=frm,
                Destination={'ToAddresses': [to]},
                Message={'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                         'Body': {'Text': {'Data': message, 'Charset': 'UTF-8'}}})
            out = {'result': 'sent', 'messageId': r.get('MessageId'),
                   'sentTime': int(time.time()),
                   'from': frm, 'to': to, 'subject': subject, 'attachments': []}
            if warning:
                out['warning'] = warning
            return respond(200, out)

        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = frm
        msg['To'] = to
        msg.attach(MIMEText(message, 'plain', 'utf-8'))
        for fname, data, binary_default in atts:
            # Content type from the filename extension; generated text files
            # fall back to text/plain, stored files to octet-stream.
            ctype, _ = mimetypes.guess_type(fname)
            if not ctype:
                ctype = 'application/octet-stream' if binary_default else 'text/plain'
            maintype, subtype = ctype.split('/', 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)   # base64 CTE preserves the exact bytes
            part.add_header('Content-Disposition', 'attachment', filename=fname)
            msg.attach(part)
        # SESv2 SendEmail/Raw: 40 MB after base64, vs 10 MB and non-adjustable
        # on v1 SendRawEmail. as_bytes() avoids a second full-size copy.
        raw = msg.as_bytes()
        r = mail_store.sesv2().send_email(FromEmailAddress=frm,
                                          Destination={'ToAddresses': [to]},
                                          Content={'Raw': {'Data': raw}})
    except ApiError:
        raise
    except Exception as e:     # noqa: BLE001 - SES refusal reported as a gateway error
        raise ApiError(502, 'SES refused the message: %s' % e)

    out = {'result': 'sent', 'messageId': r.get('MessageId'),
           'sentTime': int(time.time()),
           'from': frm, 'to': to, 'subject': subject,
           'messageBytes': len(raw),
           'attachments': [{'filename': f, 'bytes': len(d)} for f, d, _b in atts]}
    if len(raw) > DELIVERY_WARN_BYTES:
        wire_mb = len(raw) / 1048576.0
        refused = ['%s (%d MB)' % (n, c) for n, c in DELIVERY_TIERS if wire_mb > c]
        out['messageMb'] = round(wire_mb, 1)
        out['refusedBy'] = refused
        out['deliverabilityWarning'] = (
            'SES accepted this message, but at %.1f MB on the wire it is over the '
            'published receiving limit of: %s. Those recipients refuse it after SES '
            'has already taken it, so it returns as a bounce to %s rather than being '
            'delivered - this 200 only means SES accepted it, not that it arrived. '
            'Base64 and MIME line breaks add about 35%%, so roughly %d KB of raw '
            'attachment is the most that clears a 25 MB receiver.'
            % (wire_mb, ', '.join(refused), frm, DELIVERABLE_RAW_KB))
    if warning:
        out['warning'] = warning
    return respond(200, out)
