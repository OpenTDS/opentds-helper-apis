"""Tests for the qr service (pure computation; no AWS)."""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.setdefault('AWS_DEFAULT_REGION', 'ap-southeast-2')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ['BASE_URL'] = 'https://api.example.com/v1'

import qr  # noqa: E402

try:                                    # optional independent decoder
    import cv2
    import numpy as np
except Exception:                       # pragma: no cover
    cv2 = None


def event(action, qs=None, method='GET'):
    return {
        'httpMethod': method,
        'resource': '/qr/{proxy+}',
        'path': '/v1/qr/' + action,
        'pathParameters': {'proxy': action} if action else None,
        'queryStringParameters': qs,
        'headers': {'Host': 'api.example.com'},
        'body': None,
        'isBase64Encoded': False,
        'requestContext': {'identity': {'sourceIp': '203.0.113.10'}},
    }


def call(qs=None, action='get', method='GET'):
    return qr.handler(event(action, qs, method), None)


def decode_png(png_bytes):
    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return text


def test_json_default():
    res = call({'text': 'hello world'})
    assert res['statusCode'] == 200
    assert res['headers']['Content-Type'].startswith('application/json')
    b = json.loads(res['body'])
    assert b['text'] == 'hello world' and b['bytes'] == 11
    assert b['ecc'] == 'M' and b['version'] == 1 and b['modules'] == 21
    assert b['scale'] == 8 and b['border'] == 4 and b['pixels'] == (21 + 8) * 8
    png = base64.b64decode(b['png'])
    assert png[:8] == b'\x89PNG\r\n\x1a\n' and b['pngBytes'] == len(png)
    assert b['dataUri'] == 'data:image/png;base64,' + b['png']
    assert 0 <= b['mask'] <= 7


@pytest.mark.skipif(cv2 is None, reason='OpenCV not installed')
@pytest.mark.parametrize('text,ecc,version', [
    ('hello world', 'M', 1),
    ('https://www.example.com/account/verify?code=ABC123&user=42', 'H', 1),
    ('x' * 300, 'L', 1),
    ('short', 'Q', 7),                  # forced version >= 7 carries version info
])
def test_round_trip_through_independent_decoder(text, ecc, version):
    b = json.loads(call({'text': text, 'ecc': ecc, 'version': str(version)})['body'])
    assert b['version'] >= version
    assert decode_png(base64.b64decode(b['png'])) == text


def test_text64_url_safe_and_unicode():
    payload = 'a+b&c%d#e ünï'
    enc = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    b = json.loads(call({'text64': enc})['body'])
    assert b['text'] == payload and b['bytes'] == len(payload.encode())


def test_svg():
    res = call({'text': 'hi', 'format': 'SVG', 'scale': '2', 'border': '1'})
    assert res['statusCode'] == 200
    assert res['headers']['Content-Type'] == 'image/svg+xml; charset=utf-8'
    assert res['body'].startswith('<svg ') and 'viewBox="0 0 23 23"' in res['body']
    assert 'width="46"' in res['body']


def test_html_page_targets_v1_and_has_no_key():
    res = call({'text': '<b>"x"</b>', 'format': 'html', 'ecc': 'q'})
    assert res['statusCode'] == 200
    assert res['headers']['Content-Type'] == 'text/html; charset=utf-8'
    page = res['body']
    assert 'action="https://api.example.com/v1/qr/get"' in page
    assert '<input type="hidden" name="format" value="html">' in page
    assert '<input type="hidden" name="ecc" value="Q">' in page
    assert 'apikey' not in page.lower() and 'api key' not in page.lower()
    assert '&lt;b&gt;&quot;x&quot;&lt;/b&gt;' in page and '<b>"x"</b>' not in page
    assert 'data:image/png;base64,' in page


def test_clamping():
    b = json.loads(call({'text': 'x', 'scale': '999', 'border': '-3', 'version': '99'})['body'])
    assert b['scale'] == 40 and b['border'] == 0 and b['version'] == 40


def test_errors_json():
    for qs in ({}, {'text': ''}, {'text': 'a', 'text64': 'YQ'}, {'text': 'a', 'ecc': 'Z'},
               {'text': 'a', 'scale': 'big'}, {'text': 'a', 'format': 'png'},
               {'text': 'x' * 3000}, {'text': 'x' * 1500, 'ecc': 'H'}):
        res = call(qs)
        assert res['statusCode'] == 400, qs
        b = json.loads(res['body'])
        assert b['status'] == 400 and b['error']


def test_error_as_html_page_when_format_html():
    res = call({'format': 'html'})
    assert res['statusCode'] == 400
    assert res['headers']['Content-Type'].startswith('text/html')
    assert 'https://api.example.com/v1/qr/get?format=html&amp;text=hello%20world' in res['body']


def test_old_routes_are_gone_and_index():
    for old in ('show', 'svg'):
        res = call({'text': 'x'}, action=old)
        assert res['statusCode'] == 404
        assert json.loads(res['body'])['actions'] == ['get']
    assert json.loads(call(action='')['body'])['service'] == 'qr'
    assert call(method='OPTIONS')['statusCode'] == 204
