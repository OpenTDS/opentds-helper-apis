"""Contract check: every response the suite produced matches the OpenAPI documents.

The other test files exercise the services; common.run logs each response to
CONTRACT_LOG (set up in conftest.py). Named test_zz_* so it runs last. It
checks, for both the published 3.2.1 document and the 3.0.3 copy, that:

  * the status code is documented for that operation (exactly, as NXX, or as
    `default` - which then means the body must be an Error);
  * the Content-Type is one the response declares;
  * declared headers (e.g. Location on a 302) are present;
  * a JSON body validates against the declared schema.

Run alone it has nothing to check and skips.
"""
import json
import os
import sys

import pytest
from jsonschema import Draft202012Validator
from openapi_schema_validator import OAS30Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, DRAFT4

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'src')
METHODS = ('get', 'put', 'post', 'delete', 'patch', 'head', 'options', 'trace', 'query')


def _spec(name):
    text = open(os.path.join(SRC, name), encoding='utf-8').read()
    for k, v in (('{BASE_URL}', 'https://api.example.com/v1'), ('{ROOT_URL}', 'https://example.com'),
                 ('{MAIL_DOMAIN}', 'mail.example.com')):
        text = text.replace(k, v)
    return json.loads(text)


def _records():
    log = os.environ.get('CONTRACT_LOG')
    if not log or not os.path.exists(log):
        return []
    with open(log, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def _ptr(*parts):
    return '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def _pick(responses, status):
    s = str(status)
    for k in (s, s[0] + 'XX', 'default'):
        if k in responses:
            return k
    return None


def _check(spec, validate, rec):
    """Return a problem description, or None."""
    path, method, status = rec['path'], rec['method'].lower(), rec['status']
    if method in ('options', 'head'):
        return None
    item = spec['paths'].get(path)
    if item is None:
        # Unknown action: the router's 404, an Error that lists the valid actions.
        if status != 404:
            return f'undocumented path answered {status}'
        errors = validate(json.loads(rec['body']), 'components/schemas/Error')
        return '404: ' + '; '.join(errors[:3]) if errors else None
    if method not in item:
        # Every action answers any method; an undocumented one behaves like the documented one.
        method = 'get' if 'get' in item else next(m for m in item if m in METHODS)
    op = item[method]
    key = _pick(op['responses'], status)
    if key is None:
        return f'status {status} not documented'
    resp = op['responses'][key]
    rptr = ('paths', path, method, 'responses', key)
    if '$ref' in resp:
        rptr = tuple(resp['$ref'][2:].split('/'))
        resp = spec['components']['responses'][rptr[-1]]
    for h in (resp.get('headers') or {}):
        if not any(k.lower() == h.lower() for k in rec['headers']):
            return f'{status}: header {h} missing'
    ctype = (rec['headers'].get('Content-Type') or '').split(';')[0].strip()
    body = rec['body']
    content = resp.get('content') or {}
    if not body:
        return None
    if ctype not in content and '*/*' not in content:
        return f'{status}: Content-Type {ctype} not declared ({sorted(content)})'
    if ctype != 'application/json' or 'schema' not in content.get(ctype, {}):
        return None
    errors = validate(json.loads(body), _ptr(*rptr, 'content', ctype, 'schema'))
    if errors:
        return f'{status} ({key}): ' + '; '.join(errors[:3])


def _validator32(spec):
    registry = Registry().with_resource('urn:oas', Resource.from_contents(spec, DRAFT202012))

    def validate(instance, ptr):
        v = Draft202012Validator({'$ref': 'urn:oas#/' + ptr}, registry=registry)
        return [f'{"/".join(map(str, e.absolute_path)) or "<body>"}: {e.message[:160]}'
                for e in v.iter_errors(instance)]
    return validate


def _validator30(spec):
    registry = Registry().with_resource('urn:oas', Resource.from_contents(spec, DRAFT4))

    def validate(instance, ptr):
        v = OAS30Validator({'$ref': 'urn:oas#/' + ptr}, registry=registry)
        return [f'{"/".join(map(str, e.absolute_path)) or "<body>"}: {e.message[:160]}'
                for e in v.iter_errors(instance)]
    return validate


@pytest.mark.parametrize('doc,make', [('discovery_openapi.json', _validator32),
                                      ('discovery_openapi30.json', _validator30)])
def test_every_response_matches_the_openapi_document(doc, make):
    recs = _records()
    if len(recs) < 50:
        pytest.skip('run with the whole suite: no responses were logged')
    spec = _spec(doc)
    validate = make(spec)
    problems = {}
    for rec in recs:
        p = _check(spec, validate, rec)
        if p:
            problems.setdefault(f"{rec['method']} {rec['path']}: {p}", 0)
            problems[f"{rec['method']} {rec['path']}: {p}"] += 1
    assert not problems, '\n'.join(f'{n}x {k}' for k, n in sorted(problems.items()))


def test_contract_checker_catches_mistakes():
    """The checker itself: wrong types, undocumented statuses and missing headers fail."""
    spec = _spec('discovery_openapi.json')
    v = _validator32(spec)
    good = {'path': '/lists/codes', 'method': 'GET', 'status': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'rand3': 'abc', 'rand4': 'abcd', 'rand6': 'abcdef',
                                'rand8': 'abcdefgh', 'rand10': 'abcdefghij'})}
    assert _check(spec, v, good) is None
    wrong = dict(json.loads(good['body']), rand3=3)
    assert 'rand3' in _check(spec, v, dict(good, body=json.dumps(wrong)))
    assert 'not declared' in _check(spec, v, dict(good, headers={'Content-Type': 'text/html'}))
    # a 2xx that is not documented falls to `default`, whose Error body it does not match
    assert _check(spec, v, dict(good, status=201)) is not None
    assert 'Location' in _check(spec, v, {'path': '/mail/attachments/get', 'method': 'GET',
                                          'status': 302, 'headers': {}, 'body': ''})
    gateway = {'path': '/lists/pop', 'method': 'GET', 'status': 429,
               'headers': {'Content-Type': 'application/json'},
               'body': json.dumps({'error': 'Too Many Requests', 'gatewayError': 'THROTTLED'})}
    assert _check(spec, v, gateway) is None
    assert _check(_spec('discovery_openapi30.json'),
                  _validator30(_spec('discovery_openapi30.json')), gateway) is None
