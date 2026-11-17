"""Discovery documents: built files are current, valid, and served correctly."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'src'))
os.environ['BASE_URL'] = 'https://api.example.org/v1'
os.environ['MAIL_DOMAIN'] = 'example.org'

import discovery  # noqa: E402


def ev(proxy=None, method='GET', accept=None):
    return {'httpMethod': method, 'resource': '/{proxy+}' if proxy else '/',
            'pathParameters': {'proxy': proxy} if proxy else None,
            'queryStringParameters': None, 'headers': {'Accept': accept} if accept else {}}


def call(proxy=None, method='GET', accept=None):
    return discovery.handler(ev(proxy, method, accept), None)


def test_landing_page_for_browsers():
    r = call(accept='text/html,application/xhtml+xml,*/*;q=0.8')
    assert r['statusCode'] == 200 and r['headers']['Content-Type'].startswith('text/html')
    page = r['body']
    for placeholder in ('{BASE_URL}', '{MAIL_DOMAIN}', '{ROOT_URL}', '{SERVICE_CARDS}'):
        assert placeholder not in page
    assert 'https://api.example.org/v1/llms.txt' in page and 'testthis.now' in page
    for svc in ('lists', 'names', 'mail', 'sms', 'totp', 'webhook', 'lock', 'qr', 'perf', 'voice'):
        assert f'<h3><code>{svc}</code></h3>' in page
    assert 'vars/get' in page and 'attachments/find' in page
    assert '<code>say</code>' in page
    # scripts still get JSON at the same URL
    assert call(accept='application/json')['headers']['Content-Type'].startswith('application/json')


def test_built_docs_are_current():
    r = subprocess.run([sys.executable, os.path.join(HERE, '..', 'tools', 'build_docs.py'),
                        '--check'], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_openapi_valid_and_substituted():
    from openapi_spec_validator import validate
    r = call('openapi.json')
    assert r['statusCode'] == 200
    spec = json.loads(r['body'])
    assert spec['servers'][0]['url'] == 'https://api.example.org/v1'
    validate(spec)
    ids = [o['operationId'] for p in spec['paths'].values() for m, o in p.items() if m != 'servers']
    assert len(ids) == len(set(ids))
    assert '{BASE_URL}' not in r['body']
    assert call('.well-known/openapi.json')['body'] == r['body']


def test_openapi_32_and_30_copy():
    from openapi_spec_validator import validate
    spec = json.loads(call('openapi.json')['body'])
    assert spec['openapi'] == '3.2.1' and spec['$self'] == 'https://api.example.org/v1/openapi.json'
    r = call('openapi-3.0.json')
    assert r['statusCode'] == 200 and r['headers']['Content-Type'].startswith('application/json')
    old = json.loads(r['body'])
    assert old['openapi'] == '3.0.3' and '$self' not in old and '{ROOT_URL}' not in r['body']
    validate(old)

    def ops(sp):
        return {o['operationId'] for p in sp['paths'].values() for m, o in p.items() if m != 'servers'}
    assert ops(spec) == ops(old)
    # 3.2 describes the free-form parameters; the 3.0 copy keeps the named examples.
    qs = spec['paths']['/lists/push']['get']['parameters']
    assert [p['in'] for p in qs] == ['querystring']
    assert 'email' in [p['name'] for p in old['paths']['/lists/push']['get']['parameters']]
    assert 'nullable' not in call('openapi.json')['body']
    assert spec['paths']['/sms/receive']['post']['security'] == [{'smsDeviceKey': []}]
    for p in spec['paths'].values():
        for m, o in p.items():
            if m != 'servers':
                assert 'default' in o['responses'], o['operationId']
    cat = json.loads(call('.well-known/api-catalog')['body'])
    assert [d['href'].rsplit('/', 1)[1] for d in cat['linkset'][0]['service-desc']] == \
        ['openapi.json', 'openapi-3.0.json']


def test_llms_guide():
    r = call('llms.txt')
    assert r['statusCode'] == 200 and r['headers']['Content-Type'].startswith('text/plain')
    body = r['body']
    assert 'https://api.example.org/v1/mail/pop' in body
    assert '{BASE' not in body and '{MAIL_DOMAIN}' not in body and '{ROOT_URL}' not in body
    for svc in ('lists', 'names', 'mail', 'sms', 'totp', 'webhook', 'lock', 'qr', 'perf', 'voice'):
        assert f'`{svc}` —' in body, svc


def test_llms_mentions_every_documented_action():
    # The guide is hand-written: fail when an action in the OpenAPI document is missing from it.
    body = call('llms.txt')['body']
    spec = json.loads(call('openapi.json')['body'])
    actions = [p.lstrip('/') for p in spec['paths'] if p.count('/') >= 2 and not p.startswith('/.')]
    missing = [a for a in actions if f'`{a}`' not in body and f'/{a}?' not in body and f'/{a}\n' not in body]
    assert not missing, missing
    assert call('.well-known/guide.md')['headers']['Content-Type'].startswith('text/markdown')


def test_index_and_catalog():
    b = json.loads(call()['body'])
    names = [s['name'] for s in b['services']]
    assert names == ['lists', 'names', 'mail', 'sms', 'totp', 'webhook', 'lock', 'qr', 'perf', 'voice']
    lists = b['services'][0]
    assert any(a['action'] == 'vars/get' for a in lists['actions'])
    r = call('.well-known/api-catalog')
    assert r['headers']['Content-Type'].startswith('application/linkset+json')
    cat = json.loads(r['body'])
    assert cat['linkset'][0]['service-desc'][0]['href'].endswith('/v1/openapi.json')
    assert call('.well-known/api-catalog', 'HEAD')['body'] == ''
    assert call('.well-known/endpoints')['statusCode'] == 200
    assert call('robots.txt')['statusCode'] == 200


def test_unknown_path():
    r = call('nosuch')
    assert r['statusCode'] == 404
    assert 'lists' in json.loads(r['body'])['services']


def _private_words():
    # Names that must never be published (e.g. clients), one per line, in a
    # git-ignored tests/private-words.txt, or comma-separated in the
    # TEST_PRIVATE_WORDS environment variable. Kept out of the repository so the
    # check itself names nobody; with neither present only the generic checks run.
    words = [w.strip() for w in os.environ.get('TEST_PRIVATE_WORDS', '').split(',')]
    path = os.path.join(HERE, 'private-words.txt')
    if os.path.isfile(path):
        words += [l.strip() for l in open(path, encoding='utf-8') if not l.lstrip().startswith('#')]
    return sorted({w.lower() for w in words if w})


def test_no_private_or_old_domain_references():
    import glob
    import re
    private = _private_words()
    words = re.compile(r'\b(' + '|'.join(map(re.escape, private)) + r')\b') if private else None
    bad = ('loadtest.help', 'testdata.live', 'apikey',
           'x-api-key', '0494')
    root = os.path.join(HERE, '..')
    files = [f for pat in ('src/*', 'docs/**/*.md', 'docs/**/*.json', 'template.yaml',
                           'deploy.ps1', 'README.md')
             for f in glob.glob(os.path.join(root, pat), recursive=True) if os.path.isfile(f)]
    hits = []
    for f in files:
        if f.endswith('AGENT_BRIEF.md'):
            continue
        text = open(f, encoding='utf-8', errors='ignore').read().lower()
        if f.endswith('.json'):
            # OpenAPI's own name for a header credential (the SMS device key).
            text = text.replace('"type": "apikey"', '')
        for b in bad:
            if b in text:
                hits.append((os.path.relpath(f, root), b))
        if words:
            for m in words.finditer(text):
                hits.append((os.path.relpath(f, root), m.group(0)))
    assert not hits, hits
