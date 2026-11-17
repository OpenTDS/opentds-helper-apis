"""discovery - the documents an LLM or a person reads to learn the API cold.

Serves two API Gateway APIs from one function:
  * the v1 API root:   /v1, /v1/openapi.json (OpenAPI 3.2.1), /v1/openapi-3.0.json
                       (the same in 3.0.3), /v1/llms.txt, /v1/<unknown>
  * the domain root:   /, /llms.txt, /robots.txt, /.well-known/{openapi.json,
                       llms.txt, guide.md, api-catalog, endpoints}

The documents are built into the package by tools/build_docs.py with the
placeholders {BASE_URL} and {MAIL_DOMAIN}, substituted here from the
environment - so a redeploy of the code is all it takes to publish a change
(no API Gateway deployment to bump for content).
"""
import json
import os

from common import CORS, dumps, error, raw

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_URL = os.environ.get('BASE_URL', 'https://api.example.com/v1').rstrip('/')
ROOT_URL = BASE_URL.rsplit('/v1', 1)[0]
MAIL_DOMAIN = os.environ.get('MAIL_DOMAIN', 'example.com')
CACHE = {'Cache-Control': 'public, max-age=300'}

_docs = {}


def _load(name):
    if name not in _docs:
        text = open(os.path.join(HERE, name), encoding='utf-8').read()
        _docs[name] = (text.replace('{BASE_URL}', BASE_URL).replace('{MAIL_DOMAIN}', MAIL_DOMAIN)
                       .replace('{ROOT_URL}', ROOT_URL))
    return _docs[name]


def openapi_text():
    return _load('discovery_openapi.json')


def openapi30_text():
    return _load('discovery_openapi30.json')


def llms_text():
    return _load('discovery_llms.md')


def _services():
    spec = json.loads(openapi_text())
    out = {}
    for t in spec['tags']:
        if t['name'] != 'discovery':
            out[t['name']] = {'name': t['name'], 'description': t.get('description', ''),
                              'url': f"{BASE_URL}/{t['name']}", 'actions': []}
    for path, ops in spec['paths'].items():
        svc = path.strip('/').split('/')[0]
        if svc not in out or '/' not in path.strip('/'):
            continue        # discovery documents, and GET /v1/<service> itself
        methods = sorted(m.upper() for m in ops if m in ('get', 'post', 'put', 'patch', 'delete'))
        summary = next((o.get('summary') for o in ops.values() if isinstance(o, dict)), '')
        action = path.strip('/').split('/', 1)[1]
        existing = next((a for a in out[svc]['actions'] if a['action'] == action), None)
        if existing is None:
            out[svc]['actions'].append({'action': action, 'url': BASE_URL + path,
                                        'methods': methods, 'summary': summary})
    return list(out.values())


def index():
    return {
        'name': 'Test Helper API',
        'version': 'v1',
        'baseUrl': BASE_URL,
        'mailDomain': MAIL_DOMAIN,
        'docs': {
            'llms': f'{BASE_URL}/llms.txt',
            'openapi': f'{BASE_URL}/openapi.json',
            'openapi30': f'{BASE_URL}/openapi-3.0.json',
            'apiCatalog': f'{ROOT_URL}/.well-known/api-catalog',
        },
        'conventions': [
            'Every action answers GET: {baseUrl}/<service>/<action>?param=value.',
            'Parameter names are lower case and case-insensitive; names ending in 64 take base64.',
            'key = your namespace; use a fresh one per test run (lists/codes -> rand10).',
            'No authentication. Responses are camelCase JSON; times are UTC epoch seconds.',
            'Empty queue = HTTP 200 with exhausted:true. Errors = {"error": "...", "status": N}.',
        ],
        'services': _services(),
    }


def api_catalog():
    return {'linkset': [{
        'anchor': BASE_URL,
        'service-desc': [{'href': f'{BASE_URL}/openapi.json',
                          'type': 'application/vnd.oai.openapi+json;version=3.2'},
                         {'href': f'{BASE_URL}/openapi-3.0.json',
                          'type': 'application/vnd.oai.openapi+json;version=3.0'}],
        'service-doc': [{'href': f'{BASE_URL}/llms.txt', 'type': 'text/markdown'}],
        'service-meta': [{'href': f'{ROOT_URL}/.well-known/endpoints',
                          'type': 'application/json'}],
    }]}


def _json(obj, status=200, headers=None):
    h = dict(CACHE)
    if headers:
        h.update(headers)
    return raw(status, dumps(obj), 'application/json; charset=utf-8', h)


def _path(event):
    # Both APIs map '/' (no proxy parameter) and '/{proxy+}' to this function.
    return ((event.get('pathParameters') or {}).get('proxy') or '').strip('/')


def _wants_html(event):
    headers = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
    accept = str(headers.get('accept') or '')
    return 'text/html' in accept


def handler(event, context):
    method = (event.get('httpMethod') or 'GET').upper()
    if method == 'OPTIONS':
        return raw(204, '', 'text/plain')
    path = _path(event)
    low = path.lower()
    head = method == 'HEAD'

    if low == '' and _wants_html(event):
        # The landing page for people (and for anyone reviewing the service);
        # scripts asking for JSON still get the index.
        return raw(200, '' if head else _load('discovery_site.html'),
                   'text/html; charset=utf-8', CACHE)
    if low in ('', 'v1', 'index.json'):
        return _json(index())
    if low in ('openapi.json', '.well-known/openapi.json'):
        return raw(200, openapi_text(), 'application/json; charset=utf-8', CACHE)
    if low == 'openapi-3.0.json':
        return raw(200, openapi30_text(), 'application/json; charset=utf-8', CACHE)
    if low in ('llms.txt', '.well-known/llms.txt'):
        return raw(200, llms_text(), 'text/plain; charset=utf-8', CACHE)
    if low == '.well-known/guide.md':
        return raw(200, llms_text(), 'text/markdown; charset=utf-8', CACHE)
    if low == '.well-known/api-catalog':
        link = {'Link': f'<{ROOT_URL}/.well-known/api-catalog>; rel="api-catalog"'}
        body = '' if head else dumps(api_catalog())
        return raw(200, body, 'application/linkset+json; charset=utf-8', {**CACHE, **link})
    if low == '.well-known/endpoints':
        return _json({'baseUrl': BASE_URL, 'services': _services()},
                     headers={'Link': f'<{ROOT_URL}/.well-known/api-catalog>; rel="api-catalog"'})
    if low == 'robots.txt':
        return raw(200, 'User-agent: *\nAllow: /llms.txt\nAllow: /.well-known/\n'
                        'Allow: /v1/openapi.json\nAllow: /v1/openapi-3.0.json\nAllow: /v1/llms.txt\n'
                        'Disallow: /v1/\n',
                   'text/plain; charset=utf-8', CACHE)
    services = [s['name'] for s in _services()]
    return error(404, f"No such path '/{path}'. Start at {BASE_URL} (service index) or "
                      f"{BASE_URL}/llms.txt (guide).", services=services)
