"""Tests for the names service (pure in-memory; no AWS)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.setdefault('AWS_DEFAULT_REGION', 'ap-southeast-2')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ['BASE_URL'] = 'https://api.example.com/v1'

import names  # noqa: E402


def event(action, qs=None, method='GET'):
    return {
        'httpMethod': method,
        'resource': '/names/{proxy+}',
        'path': '/v1/names/' + action,
        'pathParameters': {'proxy': action} if action else None,
        'queryStringParameters': qs,
        'headers': {'Host': 'api.example.com', 'User-Agent': 'pytest'},
        'body': None,
        'isBase64Encoded': False,
        'requestContext': {'identity': {'sourceIp': '203.0.113.10'}},
    }


def call(action, qs=None, method='GET'):
    res = names.handler(event(action, qs, method), None)
    body = res['body']
    return res['statusCode'], (json.loads(body) if body else None), res


def test_regions():
    s, b, res = call('regions')
    assert s == 200
    assert res['headers']['Content-Type'].startswith('application/json')
    assert b['count'] == 15 == len(b['regions'])
    codes = [r['region'] for r in b['regions']]
    assert codes[:3] == ['AU', 'NZ', 'GB']
    pl = next(r for r in b['regions'] if r['region'] == 'PL')
    assert pl['genderedSurnames'] is True and pl['familyNameFirst'] is False
    jp = next(r for r in b['regions'] if r['region'] == 'JP')
    assert jp['familyNameFirst'] is True
    assert jp['combinations'] == jp['femaleFirstNames'] * jp['maleFirstNames'] * jp['lastNames']


def test_get_region_case_insensitive_param_and_value():
    s, b, _ = call('get', {'Region': 'au'})
    assert s == 200
    assert b['region'] == 'AU' and b['regionName'] == 'Australia'
    assert b['femaleLastName'] == b['maleLastName']
    assert b['femaleFullName'] == ' '.join([b['femaleFirstName'], b['femaleMiddleName'],
                                            b['femaleLastName']])
    assert b['seed'] is None
    assert set(b['ascii']) == {'femaleFirstName', 'femaleMiddleName', 'femaleLastName',
                               'femaleFullName', 'maleFirstName', 'maleMiddleName',
                               'maleLastName', 'maleFullName'}


def test_seed_is_repeatable_and_golden():
    # Golden values: the seeded generator must match the pre-v1 service exactly.
    _, a, _ = call('get', {'region': 'AU', 'seed': 'golden'})
    _, b, _ = call('get', {'region': 'AU', 'seed': 'golden'})
    assert a == b
    assert a['femaleFullName'] == 'Eleni Hui Wakefield'
    assert a['maleFullName'] == 'Khalid Idris Wakefield'
    assert a['seed'] == 'golden'
    _, c, _ = call('get', {'notregion': 'AU,NZ,GB', 'seed': 'golden'})
    assert c['region'] == 'IT'
    assert c['femaleFullName'] == 'Laura Bruna Valli'
    assert c['excludedRegions'] == ['AU', 'GB', 'NZ'] and c['chosenFrom'] == 12


def test_poland_gendered_surname_and_diacritics():
    _, b, _ = call('get', {'region': 'PL', 'seed': '1'})
    assert b['femaleLastName'] == 'Rogowska' and b['maleLastName'] == 'Rogowski'
    assert b['genderedSurname'] is True
    assert b['maleMiddleName'] == 'Radosław'
    assert b['ascii']['maleMiddleName'] == 'Radoslaw'
    assert b['hasDiacritics'] is True


def test_family_name_first():
    _, b, _ = call('get', {'region': 'JP', 'seed': 'golden'})
    assert b['familyNameFirst'] is True
    assert b['femaleFullName'] == 'Matsuda Akane'
    assert 'comment' in b


def test_region_any():
    s, b, _ = call('get', {'region': 'ANY', 'seed': 'x'})
    assert s == 200 and b['region'] in names.REGIONS


def test_notregion_never_returns_excluded():
    for i in range(40):
        _, b, _ = call('get', {'notregion': 'au, nz', 'seed': str(i)})
        assert b['region'] not in ('AU', 'NZ')


def test_errors():
    s, b, _ = call('get')
    assert s == 400 and b['status'] == 400 and 'ANY' in b['allowed']
    s, b, _ = call('get', {'region': 'AU', 'notregion': 'NZ'})
    assert s == 400
    s, b, _ = call('get', {'region': 'AUS'})
    assert s == 404 and b['region'] == 'AUS'
    s, b, _ = call('get', {'notregion': 'AUS'})
    assert s == 404 and b['unknown'] == ['AUS']
    s, b, _ = call('get', {'notregion': ','.join(names.REGIONS)})
    assert s == 400


def test_unknown_action_index_and_options():
    s, b, _ = call('random')
    assert s == 404 and b['actions'] == ['get', 'regions']
    s, b, _ = call('')
    assert s == 200 and b['service'] == 'names'
    res = names.handler(event('get', method='OPTIONS'), None)
    assert res['statusCode'] == 204
    # POST with query parameters behaves like GET
    s, b, _ = call('get', {'region': 'NZ'}, method='POST')
    assert s == 200 and b['region'] == 'NZ'
