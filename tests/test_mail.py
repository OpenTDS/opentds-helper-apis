"""Tests for the v1 mail service (mail.handler + mail_receive.handler), moto-backed."""
import base64
import io
import json
import urllib.parse
import os
import sys
import zipfile
import zlib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
import pytest
from moto import mock_aws

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
sys.path.insert(0, SRC)

REGION = 'ap-southeast-2'
DOMAIN = 'mail.example.com'
TABLE = 'mail-test'
INBOUND = 'mail-inbound-test'
SAVED = 'mail-attachments-test'

os.environ.update({
    'AWS_ACCESS_KEY_ID': 'testing', 'AWS_SECRET_ACCESS_KEY': 'testing',
    'AWS_SESSION_TOKEN': 'testing', 'AWS_SECURITY_TOKEN': 'testing',
    'AWS_DEFAULT_REGION': REGION, 'AWS_REGION': REGION,
    'MAIL_TABLE': TABLE, 'MAIL_DOMAIN': DOMAIN,
    'INBOUND_BUCKET': INBOUND, 'ATTACHMENT_BUCKET': SAVED,
    'ALLOWED_TO': 'qa@partner.example.org',
    'ALLOWED_DOMAINS': 'allowed.example.net,*.sub.example.org',
    'BASE_URL': 'https://api.example.com/v1',
})

import mail            # noqa: E402
import mail_receive    # noqa: E402
import mail_pdf  # noqa: E402
import mail_store      # noqa: E402


@pytest.fixture(autouse=True)
def aws():
    with mock_aws():
        mail_store.reset_clients()
        ddb = boto3.client('dynamodb', region_name=REGION)
        ddb.create_table(
            TableName=TABLE, BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': 'EmailAddress', 'AttributeType': 'S'},
                                  {'AttributeName': 'Serial', 'AttributeType': 'N'}],
            KeySchema=[{'AttributeName': 'EmailAddress', 'KeyType': 'HASH'},
                       {'AttributeName': 'Serial', 'KeyType': 'RANGE'}])
        s3 = boto3.client('s3', region_name=REGION)
        for b in (INBOUND, SAVED):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={'LocationConstraint': REGION})
        boto3.client('ses', region_name=REGION).verify_domain_identity(Domain=DOMAIN)
        yield
        mail_store.reset_clients()


# ------------------------------------------------------------- helpers ----
def call(action, qs=None, method='GET', body=None, headers=None, b64=False):
    ev = {'httpMethod': method, 'resource': '/mail/{proxy+}', 'path': '/mail/' + action,
          'pathParameters': {'proxy': action} if action else None,
          'queryStringParameters': qs, 'headers': headers or {'Host': 'api.example.com'},
          'body': body, 'isBase64Encoded': b64,
          'requestContext': {'identity': {'sourceIp': '203.0.113.9'}}}
    r = mail.handler(ev, None)
    ctype = r['headers'].get('Content-Type', '')
    if ctype.startswith('application/json'):
        return r['statusCode'], json.loads(r['body'])
    return r['statusCode'], r


def deliver(to, subject='Welcome', body='Your code is 123456', attachments=(), key=None):
    """Put a raw MIME message in the inbound bucket and fire the S3 event."""
    msg = MIMEMultipart()
    msg['From'] = 'Sender <no-reply@sender.example.com>'
    msg['To'] = to
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    for fname, data in attachments:
        part = MIMEApplication(data)
        part.add_header('Content-Disposition', 'attachment', filename=fname)
        msg.attach(part)
    key = key or 'inbound/msg-%s' % os.urandom(4).hex()
    boto3.client('s3', region_name=REGION).put_object(Bucket=INBOUND, Key=key, Body=msg.as_bytes())
    out = mail_receive.handler({'Records': [{'s3': {'bucket': {'name': INBOUND},
                                                    'object': {'key': key}}}]}, None)
    assert out['stored'] == 1


def make_pdf(text):
    content = zlib.compress(('BT /F1 12 Tf 72 720 Td (%s) Tj ET' % text).encode('latin-1'))
    objs = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length %d /Filter /FlateDecode >>\nstream\n' % len(content) + content + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = io.BytesIO()
    out.write(b'%PDF-1.4\n')
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b'%d 0 obj\n' % i + o + b'\nendobj\n')
    xref = out.tell()
    out.write(b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1))
    for off in offsets:
        out.write(b'%010d 00000 n \n' % off)
    out.write(b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref))
    return out.getvalue()


def make_docx(paragraphs):
    body = ''.join('<w:p><w:r><w:t>%s</w:t></w:r></w:p>' % p for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.'
           'openxmlformats.org/wordprocessingml/2006/main"><w:body>%s</w:body></w:document>' % body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('word/document.xml', doc)
    return buf.getvalue()


def save_b64(key, data):
    return call('attachments/save', {'key': key}, method='POST',
                body=base64.b64encode(data).decode(), headers={'Content-Type': 'text/plain'})


ADDR = 'signup@' + DOMAIN
REPORT = (b'Order summary\nCustomer: Alice Example\nTracking reference is T123456789 and '
          b'it ships today.\nInvoice: INV-001\nInvoice: INV-002\nTotal: $1,234.56\n')


# ------------------------------------------------------------- routing ----
def test_index_and_unknown_action():
    st, b = call('')
    assert st == 200 and b['service'] == 'mail' and 'attachments/find' in b['actions']
    st, b = call('nope', {'address': ADDR})
    assert st == 404 and b['status'] == 404 and 'pop' in b['actions']
    st, r = call('pop', method='OPTIONS')
    assert st == 204


# ---------------------------------------------------------------- inbox ----
def test_pop_empty_wait0():
    st, b = call('pop', {'address': ADDR, 'wait': '0'})
    assert st == 200
    assert b['exhausted'] is True and b['body'] is None and b['address'] == ADDR
    assert b['comment'] == 'no messages for this address'


def test_pop_requires_address():
    st, b = call('pop', {})
    assert st == 400 and 'address' in b['error']
    st, b = call('pop', {'address': 'not-an-address'})
    assert st == 400


def test_receive_pop_reset_clear():
    deliver('Signup <%s>' % ADDR.upper(), subject='First',
            attachments=[('report.txt', REPORT)])
    deliver(ADDR, subject='Second', body='second body')

    st, b = call('pop', {'Address': ADDR})          # parameter names are case-insensitive
    assert st == 200 and b['exhausted'] is False and b['repeat'] is False
    assert b['serial'] == 1 and b['remaining'] == 1 and b['subject'] == 'First'
    assert b['from'] == 'Sender <no-reply@sender.example.com>'
    assert b['body'] == 'Your code is 123456'
    assert isinstance(b['receivedTime'], int) and b['ageSeconds'] >= 0
    [att] = b['attachments']
    assert att['filename'] == 'report.txt' and att['bytes'] == len(REPORT)
    assert len(att['ref']) == 10 and set(att) == {'filename', 'ref', 'bytes'}

    st, b = call('pop', {'address': ADDR})
    assert b['serial'] == 2 and b['remaining'] == 0 and b['attachments'] == []
    st, b = call('pop', {'address': ADDR})
    assert b['exhausted'] is True and b['comment'] == 'no more messages'

    # grace re-offers the latest message, flagged repeat
    st, b = call('pop', {'address': ADDR, 'grace': '30'})
    assert b['exhausted'] is False and b['repeat'] is True and b['serial'] == 2

    st, b = call('reset', {'address': ADDR})
    assert st == 200 and b['reset'] is True
    st, b = call('pop', {'address': ADDR})
    assert b['serial'] == 1

    st, b = call('clear', {'address': ADDR})
    assert st == 200 and b['deleted'] == 2
    st, b = call('pop', {'address': ADDR})
    assert b['exhausted'] is True and b['comment'] == 'no messages for this address'
    st, b = call('reset', {'address': ADDR})
    assert b['reset'] is False


def test_pop_pending_claim_is_not_consumed():
    t = boto3.resource('dynamodb', region_name=REGION).Table(TABLE)
    t.put_item(Item={'EmailAddress': ADDR, 'Serial': 0, 'NextMessage': 1, 'MessageCount': 1})
    st, b = call('pop', {'address': ADDR})
    assert b['exhausted'] is True and b['pending'] is True
    t.put_item(Item={'EmailAddress': ADDR, 'Serial': 1, 'SavedTime': 1, 'Subject': 's',
                     'FromAddr': 'f', 'MessageText': 'late'})
    st, b = call('pop', {'address': ADDR})
    assert b['body'] == 'late' and b['serial'] == 1


def test_pop_waits_for_a_recently_missing_row_then_skips_it():
    import time as _time
    t = boto3.resource('dynamodb', region_name=REGION).Table(TABLE)
    t.put_item(Item={'EmailAddress': ADDR, 'Serial': 0, 'NextMessage': 1, 'MessageCount': 2})
    t.put_item(Item={'EmailAddress': ADDR, 'Serial': 2, 'SavedTime': 1, 'Subject': 's',
                     'FromAddr': 'f', 'MessageText': 'two'})
    # Serial 1 was claimed but has no row yet: it may still be being written
    # (a concurrent receive), so it is NOT skipped straight away.
    st, b = call('pop', {'address': ADDR})
    assert b['exhausted'] is True and b.get('pending') is True
    h = t.get_item(Key={'EmailAddress': ADDR, 'Serial': 0})['Item']
    assert int(h['MissingSerial']) == 1
    # Once it has been missing longer than any receive can take, it is skipped.
    t.update_item(Key={'EmailAddress': ADDR, 'Serial': 0},
                  UpdateExpression='SET MissingSince = :t',
                  ExpressionAttributeValues={':t': int(_time.time()) - 600})
    st, b = call('pop', {'address': ADDR})
    assert b['serial'] == 2 and b['body'] == 'two'


def test_pop_rejects_address_with_space():
    st, b = call('pop', {'address': 'signup run42@' + DOMAIN})
    assert st == 400 and '%2B' in b['error']


# ----------------------------------------------------------------- send ----
def test_send_get_plain():
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'someone@allowed.example.net',
                          'subject': 'Hello', 'body': 'Hi there'})
    assert st == 200, b
    assert b['result'] == 'sent' and b['messageId'] and isinstance(b['sentTime'], int)
    assert b['to'] == 'someone@allowed.example.net' and b['attachments'] == []


def test_send_get_generated_attachment_with_injection():
    st, b = call('send', {'from': 'Tester <test@%s>' % DOMAIN, 'to': 'catch@' + DOMAIN,
                          'subject': 'Gen', 'body': 'x', 'attachmentkb': '2',
                          'filename': 'eicar', 'prepend64': base64.b64encode(b'START').decode(),
                          'append64': base64.b64encode(b'END').decode()})
    assert st == 200, b
    assert b['attachments'] == [{'filename': 'eicar.txt', 'bytes': 2048 + 8}]
    assert b['messageBytes'] > 2048


def test_send_post_body_and_saved_key_and_received_ref():
    save_b64('invoice.pdf', make_pdf('Invoice INV-9'))
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'qa@partner.example.org',
                          'subject': 'Saved', 'key': 'invoice.pdf'},
                 method='POST', body='Body from POST', headers={'Content-Type': 'text/plain'})
    assert st == 200, b
    assert b['attachments'][0]['filename'] == 'invoice.pdf'

    deliver(ADDR, attachments=[('report.txt', REPORT)])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'x@deep.sub.example.org',
                          'subject': 'Fwd', 'body': 'fwd', 'ref': ref})
    assert st == 200, b
    assert b['attachments'] == [{'filename': 'report.txt', 'bytes': len(REPORT)}]

    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'catch@' + DOMAIN, 'subject': 's',
                          'body': 'b', 'ref': ref, 'key': 'invoice.pdf'})
    assert st == 400 and 'mutually exclusive' in b['error']
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'catch@' + DOMAIN, 'subject': 's',
                          'body': 'b', 'key': 'missing.pdf'})
    assert st == 404
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'catch@' + DOMAIN, 'subject': 's',
                          'body': 'b', 'ref': 'zzzzzzzzzz'})
    assert st == 404


def test_send_post_json_structured():
    save_b64('a.txt', b'hello')
    deliver(ADDR, attachments=[('report.txt', REPORT)])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    payload = {'body': 'two files', 'attachments': [{'key': 'a.txt', 'filename': 'renamed.txt'},
                                                    {'ref': ref}]}
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'catch@' + DOMAIN, 'subject': 'J'},
                 method='POST', body=json.dumps(payload),
                 headers={'Content-Type': 'application/json'})
    assert st == 200, b
    assert [a['filename'] for a in b['attachments']] == ['renamed.txt', 'report.txt']

    # JSON sent as text/plain goes out literally, with a warning
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'catch@' + DOMAIN, 'subject': 'J'},
                 method='POST', body=json.dumps(payload), headers={'Content-Type': 'text/plain'})
    assert st == 200 and 'warning' in b and b['attachments'] == []


def test_send_refusals():
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'victim@elsewhere.example',
                          'subject': 's', 'body': 'b'})
    assert st == 403 and b['status'] == 403 and b['mailDomain'] == DOMAIN
    assert 'allowed.example.net' in b['allowedDomains']
    # '*.sub.example.org' does not admit sub.example.org itself, nor a lookalike
    for to in ('a@sub.example.org', 'a@evilsub.example.org'):
        assert call('send', {'from': 'test@' + DOMAIN, 'to': to, 'subject': 's',
                             'body': 'b'})[0] == 403
    st, b = call('send', {'from': 'spoof@elsewhere.example', 'to': 'catch@' + DOMAIN,
                          'subject': 's', 'body': 'b'})
    assert st == 403 and b['allowedFrom'] == '*@' + DOMAIN
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'catch@' + DOMAIN, 'subject': 's'})
    assert st == 400 and 'body' in b['error']
    st, b = call('send', {'from': 'test@' + DOMAIN, 'to': 'catch@' + DOMAIN, 'subject': 's',
                          'body': 'b', 'attachmentkb': '999999'})
    assert st == 400


# ---------------------------------------------------------- attachments ----
def test_save_get_delete_by_key():
    st, b = save_b64('report.txt', REPORT)
    assert st == 200 and {k: b[k] for k in ('result', 'key', 'bytes', 'filename', 'source')} == {
        'result': 'saved', 'key': 'report.txt', 'bytes': len(REPORT),
        'filename': 'report.txt', 'source': 'upload'}
    assert b['bodyFormat'] == 'base64' and len(b['sha256']) == 64
    st, b = call('attachments/get', {'key': 'report.txt'})
    assert st == 200 and b['source'] == 'saved' and base64.b64decode(b['data']) == REPORT
    st, b = call('attachments/get', {'key': 'report.txt', 'url': 'true'})
    assert st == 200 and b['url'].startswith('https://') and 'data' not in b
    assert b['expiresInSeconds'] == 900
    st, b = call('attachments/delete', {'key': 'report.txt'})
    assert st == 200 and b['deleted'] is True
    st, b = call('attachments/delete', {'key': 'report.txt'})
    assert st == 200 and b['deleted'] is False
    st, b = call('attachments/get', {'key': 'report.txt'})
    assert st == 404


def test_save_errors():
    st, b = call('attachments/save', {'key': 'x.txt'})
    assert st == 400                               # GET without ref
    st, b = call('attachments/save', {'key': 'x.txt'}, method='POST', body='')
    assert st == 400
    st, b = call('attachments/save', {'key': '.bad'}, method='POST', body='aGVsbG8=')
    assert st == 400
    st, b = call('attachments/save', {'key': 'x.txt', 'ref': 'abcdefghij'})
    assert st == 404


def test_received_get_and_promote():
    deliver(ADDR, attachments=[('report.txt', REPORT)])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    st, b = call('attachments/get', {'ref': ref})
    assert st == 200 and b['source'] == 'received' and b['ref'] == ref
    assert b['filename'] == 'report.txt' and base64.b64decode(b['data']) == REPORT
    st, b = call('attachments/save', {'key': 'kept.txt', 'ref': ref})
    assert st == 200 and b['source'] == 'received' and b['bytes'] == len(REPORT)
    assert b['contentType'] == call('attachments/get', {'ref': ref})[1]['contentType']   # as it arrived
    st, b = call('attachments/save', {'key': 'renamed', 'ref': ref, 'filename': 'r.pdf'})
    assert st == 200 and b['contentType'] == 'application/pdf' and b['filename'] == 'r.pdf'
    st, b = call('attachments/get', {'key': 'kept.txt'})
    assert base64.b64decode(b['data']) == REPORT
    st, b = call('attachments/get', {'key': 'kept.txt', 'ref': ref})
    assert st == 400
    st, b = call('attachments/get', {})
    assert st == 400
    st, b = call('attachments/get', {'ref': 'bad/ref'})
    assert st == 400
    st, b = call('attachments/get', {'ref': 'zzzzzzzzzz'})
    assert st == 404


def test_get_download_and_view_redirect_to_the_file():
    deliver(ADDR, attachments=[('invoice.pdf', b'%PDF-1.4 tiny')])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    st, r = call('attachments/get', {'ref': ref, 'download': 'true'})
    loc = urllib.parse.unquote(r['headers']['Location'])
    assert st == 302 and 'attachment; filename="invoice.pdf"' in loc
    st, r = call('attachments/get', {'ref': ref, 'view': 'true'})
    loc = urllib.parse.unquote(r['headers']['Location'])
    assert st == 302 and 'inline; filename="invoice.pdf"' in loc and 'application/pdf' in loc
    data = requests_get(r['headers']['Location'])
    assert data == b'%PDF-1.4 tiny'
    assert call('attachments/get', {'ref': ref, 'view': 'true', 'download': 'true'})[0] == 400
    assert call('attachments/get', {'ref': 'zzzzzzzzzz', 'view': 'true'})[0] == 404


def requests_get(url):
    # Fetch a presigned URL through moto's in-process S3.
    p = urllib.parse.urlsplit(url)
    bucket = p.netloc.split('.')[0]
    key = urllib.parse.unquote(p.path.lstrip('/'))
    return boto3.client('s3', region_name=REGION).get_object(Bucket=bucket, Key=key)['Body'].read()


def test_find_text_regex_boundaries():
    save_b64('report.txt', REPORT)
    st, b = call('attachments/find', {'key': 'report.txt', 'text': 'INV-00'})
    assert st == 200, b
    assert b['count'] == 2 and b['match']['mode'] == 'literal' and b['source'] == 'saved'
    assert b['results'][0]['matched'] == 'INV-00' and 'contextEscaped' in b['results'][0]
    assert b['document']['extractor'] == 'text'

    st, b = call('attachments/find', {'key': 'report.txt',
                                      'text64': base64.b64encode(b'$1,234.56').decode()})
    assert b['count'] == 1

    # whitespace in the term matches any whitespace; case still matters
    st, b = call('attachments/find', {'key': 'report.txt', 'text': 'alice  EXAMPLE'})
    assert b['count'] == 0 and b['caseInsensitiveCount'] == 1 and 'ignorecase' in b['hint']
    st, b = call('attachments/find', {'key': 'report.txt', 'text': 'alice  EXAMPLE',
                                      'ignorecase': 'true'})
    assert b['count'] == 1
    st, b = call('attachments/find', {'key': 'report.txt', 'text': 'Alice  Example', 'exact': 'true'})
    assert b['count'] == 0

    st, b = call('attachments/find', {'key': 'report.txt', 'regex': r'INV-(\d+)', 'limit': '1'})
    assert b['count'] == 2 and b['values'] == ['001'] and b['truncated'] is True

    st, b = call('attachments/find', {'key': 'report.txt', 'left': 'reference is ', 'right': ' and'})
    assert b['values'] == ['T123456789'] and b['match']['mode'] == 'boundaries'

    st, b = call('attachments/find', {'key': 'report.txt', 'left': 'Customer: '})
    assert b['values'] == ['Alice Example']

    st, b = call('attachments/find', {'key': 'report.txt', 'left64':
                                      base64.b64encode(b'Total: ').decode()})
    assert b['values'] == ['$1,234.56']


def test_find_errors():
    save_b64('report.txt', REPORT)
    assert call('attachments/find', {'key': 'report.txt'})[0] == 400
    st, b = call('attachments/find', {'key': 'report.txt', 'text': 'a', 'regex': 'b'})
    assert st == 400 and b['given'] == ['literal', 'regex']
    assert call('attachments/find', {'key': 'report.txt', 'right': 'x'})[0] == 400
    assert call('attachments/find', {'key': 'report.txt', 'regex': '('})[0] == 400
    assert call('attachments/find', {'text': 'x'})[0] == 400
    assert call('attachments/find', {'key': 'nope.txt', 'text': 'x'})[0] == 404


def test_find_pdf_docx_and_received_ref():
    save_b64('inv.pdf', make_pdf('Invoice number INV-12345 due 30 days'))
    st, b = call('attachments/find', {'key': 'inv.pdf', 'regex': r'INV-\d+'})
    assert st == 200, b
    assert b['document']['extractor'] == 'pdf-layout' and b['values'] == ['INV-12345']
    assert b['results'][0]['page'] == 1

    deliver(ADDR, attachments=[('letter.docx', make_docx(['Dear customer', 'Reference: ABC-777']))])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    st, b = call('attachments/find', {'ref': ref, 'left': 'Reference: '})
    assert st == 200, b
    assert b['source'] == 'received' and b['document']['extractor'] == 'ooxml'
    assert b['values'] == ['ABC-777']


def test_upload_url_and_page():
    st, b = call('attachments/upload/url', {'key': 'big.bin', 'size': '1048576'})
    assert st == 200
    assert b['key'] == 'big.bin' and b['method'] == 'POST' and b['maxKb'] == 102400
    assert b['url'].startswith('https://') and b['fields']['key'] == 'big.bin'
    assert 'policy' in b['fields'] and b['expiresInSeconds'] == 900
    st, b = call('attachments/upload/url', {'key': 'big.bin', 'size': str(200 * 1024 * 1024)})
    assert st == 400
    assert call('attachments/upload/url', {})[0] == 400

    st, r = call('attachments/upload')
    assert st == 200 and r['headers']['Content-Type'].startswith('text/html')
    html = r['body']
    assert '"https://api.example.com/v1"' in html and "/mail/attachments/" in html
    assert 'apikey' not in html.lower()


def test_receive_clips_oversized_body():
    big = 'x' * (400 * 1024)
    deliver(ADDR, body=big)
    st, b = call('pop', {'address': ADDR})
    assert st == 200 and b['bodyTruncated'] is True
    assert 290 * 1024 < len(b['body'].encode()) <= 300 * 1024 + 16


# ---------------------------------------------------- PDF layout and passwords
FIX = os.path.join(os.path.dirname(__file__), 'fixtures')


def save_fixture(name, key=None):
    save_b64(key or name, open(os.path.join(FIX, name), 'rb').read())
    return key or name


def find(key, **qs):
    return call('attachments/find', dict(key=key, **qs))


def test_invoice_phrases_spanning_label_and_value():
    k = save_fixture('invoice.pdf')
    for phrase in ('Invoice number INV-261002-1TFJP', 'Total including GST $2,972.00',
                   'Order number VD-261002-1TFJP', 'Visa ending 1111',
                   'Technician setup & dock commissioning - AutoPlanter Small × 1'):
        st, b = find(k, text=phrase)
        assert st == 200 and b['count'] == 1, (phrase, b)
        assert b['results'][0]['page'] == 1
    st, b = find(k, text='GST (10%)')
    assert b['count'] == 2


def test_invoice_boundaries():
    k = save_fixture('invoice.pdf')
    cases = [({'left': 'Total including GST'}, ['$2,972.00']),
             ({'left': 'Total including GST '}, ['$2,972.00']),          # trailing space is fine
             ({'left': 'Invoice number'}, ['INV-261002-1TFJP']),
             ({'left': 'Order number'}, ['VD-261002-1TFJP']),             # stops at the column
             ({'left': 'Authorisation '}, ['VNZRA5']),
             ({'left': 'Delivery: ', 'right': ' Morning'}, ['2026-10-06']),
             ({'left': 'Balance due'}, ['$0.00'])]
    for qs, want in cases:
        st, b = find(k, **qs)
        assert st == 200 and b['values'] == want, (qs, b['values'])


def test_empty_capture_is_not_a_match():
    k = save_fixture('invoice.pdf')
    st, b = find(k, left='Page 1 of 1')            # nothing after it on the line
    assert b['count'] == 0 and b['emptyMatches'] == 1 and b['values'] == []


def test_metadata_search():
    k = save_fixture('invoice.pdf')
    st, b = find(k, left='TotalIncludingGst', **{'in': 'metadata'})
    assert b['values'] == ['2972.00'] and b['results'][0]['page'] is None
    st, b = find(k, text='Verdant Dock lib/pdf.js')
    assert b['count'] == 0 and 'in=metadata' in b['hint']
    st, b = find(k, text='Verdant Dock lib/pdf.js', **{'in': 'all'})
    assert b['count'] == 1


def test_text_action():
    k = save_fixture('invoice.pdf')
    st, b = call('attachments/text', {'key': k})
    assert st == 200 and b['document']['pages'] == 1 and b['document']['encrypted'] is False
    assert 'Invoice number\tINV-261002-1TFJP' in b['pages'][0]['text']
    assert b['metadata']['InvoiceNumber'] == 'INV-261002-1TFJP'
    ev = {'httpMethod': 'GET', 'resource': '/mail/{proxy+}', 'path': '/v1/mail/attachments/text',
          'pathParameters': {'proxy': 'attachments/text'},
          'queryStringParameters': {'key': k, 'format': 'text'}, 'headers': {}, 'body': None,
          'isBase64Encoded': False}
    res = mail.handler(ev, None)
    assert res['headers']['Content-Type'].startswith('text/plain')
    assert 'Total including GST\t$2,972.00' in res['body']


def test_text_action_pages():
    k = save_fixture('two-pages.pdf')
    st, b = call('attachments/text', {'key': k})
    assert [p['page'] for p in b['pages']] == [1, 2]
    assert b['pages'][1]['text'] == 'Closing balance\t$9,876.54'
    st, b = call('attachments/text', {'key': k, 'page': '2'})
    assert len(b['pages']) == 1 and b['pages'][0]['page'] == 2
    assert call('attachments/text', {'key': k, 'page': '3'})[0] == 404
    st, b = find(k, left='Closing balance')
    assert b['values'] == ['$9,876.54'] and b['results'][0]['page'] == 2


def test_object_streams_and_tables():
    k = save_fixture('objstm-table.pdf')
    st, b = find(k, text='Widget large 2 $40.00')
    assert b['count'] == 1


@pytest.fixture
def pure_python_crypto(monkeypatch):
    # Lambda's Python has no 'cryptography' package: exercise the built-in AES/RC4.
    import mail_crypto
    monkeypatch.setattr(mail_crypto, '_HAVE_CRYPTOGRAPHY', False)


@pytest.mark.usefixtures('pure_python_crypto')
@pytest.mark.parametrize('name,method', [
    ('invoice-aes256.pdf', 'AES-256'), ('invoice-aes128.pdf', 'AES-128'),
    ('invoice-rc4-128.pdf', 'RC4-128'), ('invoice-rc4-40.pdf', 'RC4-40')])
def test_password_protected(name, method):
    k = save_fixture(name)
    st, b = find(k, text='Invoice number INV-261002-1TFJP')
    assert st == 422 and b['passwordRequired'] is True and b['encrypted'] is True
    st, b = find(k, text='Invoice number INV-261002-1TFJP', password='wrong')
    assert st == 422 and b['passwordIncorrect'] is True
    st, b = find(k, text='Invoice number INV-261002-1TFJP', password='secret')
    assert st == 200 and b['count'] == 1
    assert b['document']['encryption'] == method and b['document']['passwordUsed'] == 'user'
    st, b = find(k, left='Total including GST', password='owner')
    assert b['values'] == ['$2,972.00'] and b['document']['passwordUsed'] == 'owner'
    st, b = find(k, left='InvoiceNumber', password='secret', **{'in': 'metadata'})
    assert b['values'] == ['INV-261002-1TFJP']          # metadata strings are decrypted too


@pytest.mark.usefixtures('pure_python_crypto')
def test_password64_and_owner_only_pdf():
    k = save_fixture('invoice-owner-only.pdf')
    st, b = find(k, text='Visa ending 1111')            # opens without a password
    assert st == 200 and b['count'] == 1 and b['document']['passwordUsed'] == 'none'
    k2 = save_fixture('invoice-aes256.pdf', 'enc.pdf')
    st, b = call('attachments/text', {'key': k2, 'password64': base64.b64encode(b'secret').decode()})
    assert st == 200 and b['document']['encryption'] == 'AES-256'


# ------------------------------------------------- save filename + compare ----
INVOICE = open(os.path.join(FIX, 'invoice.pdf'), 'rb').read()


def save_raw(key, data, **qs):
    # Raw bytes, as a browser fetch() or curl --data-binary sends them. API
    # Gateway base64-encodes application/octet-stream bodies (BinaryMediaTypes).
    return call('attachments/save', dict(key=key, **qs), method='POST',
                body=base64.b64encode(data).decode(), b64=True,
                headers={'Content-Type': 'application/octet-stream'})


def test_save_filename_sets_name_and_type():
    st, b = save_b64_named('order-123-invoice', INVOICE, 'INV-261002-1TFJP.pdf')
    assert st == 200 and b['filename'] == 'INV-261002-1TFJP.pdf' and b['contentType'] == 'application/pdf'
    st, g = call('attachments/get', {'key': 'order-123-invoice'})
    assert g['filename'] == 'INV-261002-1TFJP.pdf' and g['contentType'] == 'application/pdf'
    st, r = call('attachments/get', {'key': 'order-123-invoice', 'view': 'true'})
    loc = urllib.parse.unquote(r['headers']['Location'])
    assert st == 302 and 'inline; filename="INV-261002-1TFJP.pdf"' in loc


def save_b64_named(key, data, filename):
    return call('attachments/save', {'key': key, 'filename': filename}, method='POST',
                body=base64.b64encode(data).decode(), headers={'Content-Type': 'text/plain'})


def test_save_without_filename_hints_and_is_octet_stream():
    st, b = save_b64('no-name', INVOICE)
    assert st == 200 and b['filename'] == 'no-name' and 'filename=' in b['hint']
    assert b['contentType'] == 'application/octet-stream'


def test_save_filename_is_sanitised():
    st, b = save_b64_named('k1', b'x', 'C:\\Users\\Paul\\My Invoice (1).pdf')
    assert b['filename'] == 'My_Invoice__1_.pdf'


def test_save_raw_binary_body():
    st, b = save_raw('raw-invoice', INVOICE, filename='raw.pdf')
    assert st == 200 and b['bodyFormat'] == 'binary' and b['bytes'] == len(INVOICE)
    st, g = call('attachments/get', {'key': 'raw-invoice'})
    assert base64.b64decode(g['data']) == INVOICE


def test_save_raw_pdf_as_text_is_refused_with_advice():
    st, b = call('attachments/save', {'key': 'bad'}, method='POST',
                 body=INVOICE.decode('latin-1'), headers={'Content-Type': 'application/pdf'})
    assert st == 400 and 'application/octet-stream' in b['error']


def test_promote_with_filename_renames():
    deliver(ADDR, attachments=[('attach.bin', INVOICE)])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    st, b = call('attachments/save', {'key': 'kept', 'ref': ref, 'filename': 'kept.pdf'})
    assert st == 200 and b['filename'] == 'kept.pdf'
    assert call('attachments/get', {'key': 'kept'})[1]['contentType'] == 'application/pdf'


def test_upload_url_carries_filename():
    st, b = call('attachments/upload/url', {'key': 'big', 'filename': 'big.pdf'})
    assert st == 200 and b['fields']['x-amz-meta-filename'] == 'big.pdf'
    assert b['fields']['Content-Type'] == 'application/pdf'


def test_compare_identical_files():
    deliver(ADDR, attachments=[('INV-261002-1TFJP.pdf', INVOICE)])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    save_raw('from-app', INVOICE, filename='INV-261002-1TFJP.pdf')
    st, b = call('attachments/compare', {'ref': ref, 'otherkey': 'from-app'})
    assert st == 200 and b['same'] and b['sameBytes'] and b['sameText'] and b['sameProperties']
    assert b['verdict'].startswith('identical')
    assert b['first']['ref'] == ref and b['second']['key'] == 'from-app'
    assert b['first']['sha256'] == b['second']['sha256'] and b['differenceCount'] == 0


def test_compare_regenerated_pdf_same_content():
    regen = INVOICE.replace(b'D:20261002140200Z', b'D:20261011093000Z')
    assert regen != INVOICE
    save_raw('emailed', INVOICE)
    save_raw('downloaded', regen)
    st, b = call('attachments/compare', {'key': 'emailed', 'otherkey': 'downloaded'})
    assert b['same'] is True and b['sameBytes'] is False and b['sameText'] is True
    assert b['verdict'].startswith('same content')
    assert b['propertyDifferences'] == {'CreationDate': {'first': 'D:20261002140200Z',
                                                         'second': 'D:20261011093000Z'}}


def test_compare_different_text_lists_the_lines():
    save_raw('a', make_pdf('Total including GST $114.00'))
    save_raw('b', make_pdf('Total including GST $141.00'))
    st, b = call('attachments/compare', {'key': 'a', 'otherkey': 'b'})
    assert b['same'] is False and b['verdict'].startswith('different')
    d = b['differences'][0]
    assert d['change'] == 'changed' and d['firstPage'] == 1
    assert d['first'] == ['Total including GST $114.00'] and d['second'] == ['Total including GST $141.00']


def test_compare_passwords_and_errors():
    save_raw('locked', open(os.path.join(FIX, 'invoice-aes256.pdf'), 'rb').read())
    save_raw('plain', INVOICE)
    st, b = call('attachments/compare', {'key': 'locked', 'otherkey': 'plain'})
    assert st == 422 and b['passwordRequired']
    st, b = call('attachments/compare', {'key': 'plain', 'otherkey': 'locked', 'otherpassword': 'secret'})
    assert st == 200 and b['sameText'] and not b['sameBytes'] and b['second']['encrypted']
    assert call('attachments/compare', {'key': 'plain'})[0] == 400
    assert call('attachments/compare', {'key': 'plain', 'otherkey': 'nope'})[0] == 404


# ------------------------------------------------- get with password (unlock) ----
AES256 = open(os.path.join(FIX, 'invoice-aes256.pdf'), 'rb').read()


def _s3_object(location):
    p = urllib.parse.urlsplit(location)
    key = urllib.parse.unquote(p.path.lstrip('/'))
    return key, requests_get(location)


def test_get_view_with_password_serves_an_unlocked_copy():
    deliver(ADDR, attachments=[('statement.pdf', AES256)])
    ref = call('pop', {'address': ADDR})[1]['attachments'][0]['ref']
    st, r = call('attachments/get', {'ref': ref, 'view': 'true', 'password': 'secret'})
    loc = r['headers']['Location']
    assert st == 302 and 'inline; filename="statement.pdf"' in urllib.parse.unquote(loc)
    key, pdf = _s3_object(loc)
    assert key.startswith('_unlocked/') and key.endswith('.pdf')
    assert not mail_pdf.is_encrypted(pdf)
    assert mail_pdf.extract(pdf)[0] == mail_pdf.extract(INVOICE)[0]
    # the same request reuses the same copy
    st, r2 = call('attachments/get', {'ref': ref, 'view': 'true', 'password': 'secret'})
    assert _s3_object(r2['headers']['Location'])[0] == key


def test_get_data_and_url_with_password():
    save_raw('locked-saved', AES256, filename='locked.pdf')
    st, b = call('attachments/get', {'key': 'locked-saved', 'password64': base64.b64encode(b'owner').decode()})
    assert st == 200 and b['unlocked'] is True and b['encrypted'] is True
    assert b['originalBytes'] == len(AES256) and b['contentType'] == 'application/pdf'
    pdf = base64.b64decode(b['data'])
    assert len(pdf) == b['bytes'] and not mail_pdf.is_encrypted(pdf)
    st, b = call('attachments/get', {'key': 'locked-saved', 'password': 'secret', 'url': 'true'})
    assert st == 200 and '_unlocked/' in urllib.parse.unquote(b['url'])
    st, r = call('attachments/get', {'key': 'locked-saved', 'password': 'secret', 'download': 'true'})
    assert st == 302 and 'attachment; filename="locked.pdf"' in urllib.parse.unquote(r['headers']['Location'])


def test_get_password_errors_and_unencrypted():
    save_raw('locked2', AES256, filename='locked2.pdf')
    st, b = call('attachments/get', {'key': 'locked2', 'password': 'nope', 'view': 'true'})
    assert st == 422 and b['passwordIncorrect'] is True
    save_raw('plain2', INVOICE, filename='plain2.pdf')
    st, b = call('attachments/get', {'key': 'plain2', 'password': 'whatever'})
    assert st == 200 and b['unlocked'] is False and b['encrypted'] is False
    assert base64.b64decode(b['data']) == INVOICE
    save_raw('notpdf', b'hello world', filename='a.txt')
    st, b = call('attachments/get', {'key': 'notpdf', 'password': 'x'})
    assert st == 400 and 'PDF' in b['error']
