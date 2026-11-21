#!/usr/bin/env python3
"""Build the discovery documents that ship inside the Lambda package.

  docs/openapi/<service>.json  ->  src/discovery_openapi.json   (OpenAPI 3.2.1, published)
                               ->  src/discovery_openapi30.json (OpenAPI 3.0.3 copy)
    The fragments are written in 3.0 style; to_oas32() converts them (JSON Schema
    2020-12, $self, server names, tag summaries, and `in: querystring` for the
    operations marked x-free-form-query).
  docs/llms.md (hand-written, the published llms.txt)  ->  src/discovery_llms.md
    (docs/guide/*.md is the older per-service guide, used only if docs/llms.md is absent)
  docs/site/index.html (+ generated service cards)  ->  src/discovery_site.html

The outputs keep the placeholders {BASE_URL}, {ROOT_URL} and {MAIL_DOMAIN}; discovery.py
substitutes them at runtime from its environment, so the same package works
on any domain. Run this after editing any fragment or guide section:

    python tools/build_docs.py          # build
    python tools/build_docs.py --check  # fail if the built files are stale
"""
import json
import os
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER = ['lists', 'names', 'mail', 'sms', 'totp', 'webhook', 'lock', 'qr', 'perf', 'recon', 'voice', 'health',
         'trace']
VERSION = json.load(open(os.path.join(ROOT, 'src', 'version.json'), encoding='utf-8'))['version']

ERROR_SCHEMA = {
    'type': 'object',
    'description': (
        'Every error. Errors raised by a service carry status (the HTTP status, repeated) and '
        'often extra fields such as hint or received. Errors produced by API Gateway itself - '
        'throttling (429), timeouts (504), an unreachable function (502) or an unknown route - '
        'carry gatewayError instead of status.'),
    'required': ['error'],
    'properties': {
        'error': {'type': 'string', 'description': 'Human-readable message.'},
        'status': {'type': 'integer', 'description': 'The HTTP status, repeated (service errors).'},
        'gatewayError': {'type': 'string', 'description': 'API Gateway response type, e.g. '
                         'THROTTLED, INTEGRATION_TIMEOUT, MISSING_AUTHENTICATION_TOKEN '
                         '(gateway errors only).'},
        'hint': {'type': 'string', 'description': 'What to change, when the service knows.'},
    },
    'additionalProperties': True,
}

LIST_RECORD = {
    'type': 'object',
    'description': 'A list record: the fields it was given, plus key, index and createdTime.',
    'required': ['key', 'index', 'createdTime'],
    'properties': {
        'key': {'type': 'string', 'description': 'The list key.'},
        'index': {'type': 'integer', 'description': 'Position in the list, from 1.'},
        'createdTime': {'type': 'integer', 'description': 'When it was added, UTC epoch seconds.'},
    },
    'additionalProperties': True,
}

BUILD_SCHEMA = {
    'type': 'object',
    'description': (
        'The deployment that answered. Every function of a deployment carries the same build, '
        'which is also sent on every response as the X-Build header.'),
    'required': ['version', 'build'],
    'properties': {
        'version': {'type': 'string', 'nullable': True, 'description': 'API contract version (semver).'},
        'build': {'type': 'string', 'description': 'Build id stamped at deploy time: the UTC '
                  'deploy time as yyyymmdd.hhmm, or dev for an unstamped deployment.'},
        'commit': {'type': 'string', 'nullable': True, 'description': 'Source commit, when the source '
                   'was deployed from git; a trailing + means uncommitted changes.'},
        'deployedTime': {'type': 'integer', 'nullable': True, 'description': 'When it was deployed, UTC '
                         'epoch seconds.'},
    },
}

SERVICE_INDEX = {
    'type': 'object',
    'required': ['service', 'actions'],
    'properties': {
        'service': {'type': 'string'},
        'description': {'type': 'string'},
        'actions': {'type': 'array', 'items': {'type': 'string'}},
    },
}

ERROR_REF = {'$ref': '#/components/responses/Error'}
METHODS = ('get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace', 'query')


def _discovery_paths():
    """The discovery function answers on both servers: /v1/... and the domain root."""
    both = [{'url': '{BASE_URL}', 'description': 'API base'},
            {'url': '{ROOT_URL}', 'description': 'Domain root'}]
    json_obj = {'type': 'object'}

    def op(op_id, summary, content, description=None, status='200', desc='OK'):
        o = {'tags': ['discovery'], 'operationId': op_id, 'summary': summary,
             'responses': {status: {'description': desc, 'content': content}}}
        if description:
            o['description'] = description
        return {'servers': both, 'get': o}

    index_schema = {'type': 'object', 'required': ['name', 'baseUrl', 'services'], 'properties': {
        'name': {'type': 'string'}, 'version': {'type': 'string'},
        'build': {'$ref': '#/components/schemas/Build'},
        'baseUrl': {'type': 'string', 'format': 'uri'}, 'mailDomain': {'type': 'string'},
        'docs': {'type': 'object', 'additionalProperties': {'type': 'string', 'format': 'uri'}},
        'conventions': {'type': 'array', 'items': {'type': 'string'}},
        'services': {'type': 'array', 'items': {'type': 'object'}}}}
    md = {'text/plain': {'schema': {'type': 'string'}}}
    return {
        '/': op('discoveryIndex', 'Service index (JSON) or landing page (HTML)', {
            'application/json': {'schema': index_schema},
            'text/html': {'schema': {'type': 'string'}}},
            'Lists every service with its actions and the documentation links. A request whose '
            'Accept header includes text/html gets the landing page instead. '
            'GET /v1/<service> returns the actions of one service.'),
        '/openapi.json': op('discoveryOpenApi', 'This OpenAPI document (3.2.1)',
                            {'application/json': {'schema': json_obj}},
                            'The API described in OpenAPI 3.2.1. The same API in OpenAPI 3.0.3, '
                            'for tools that do not read 3.2 yet, is at /openapi-3.0.json.'),
        '/openapi-3.0.json': op('discoveryOpenApi30', 'The OpenAPI document in 3.0.3',
                                {'application/json': {'schema': json_obj}},
                                'The same API in OpenAPI 3.0.3. It cannot express the free-form '
                                'parameters of lists/push and perf/assert, so it lists examples.'),
        '/llms.txt': op('discoveryLlms', 'LLM guide (Markdown)', md,
                        'Intent-focused guide for AI-assisted test scripting: conventions, every '
                        'action as a copy-paste URL, fields to branch on, recipes, pitfalls.'),
        '/robots.txt': op('discoveryRobots', 'robots.txt', md),
        '/.well-known/openapi.json': op('wellKnownOpenApi', 'OpenAPI document (well-known location)',
                                        {'application/json': {'schema': json_obj}}),
        '/.well-known/llms.txt': op('wellKnownLlms', 'LLM guide (well-known location)', md),
        '/.well-known/guide.md': op('wellKnownGuide', 'LLM guide as text/markdown',
                                    {'text/markdown': {'schema': {'type': 'string'}}}),
        '/.well-known/api-catalog': op('wellKnownApiCatalog', 'API catalog (RFC 9727 linkset)',
                                       {'application/linkset+json': {'schema': {
                                           'type': 'object', 'required': ['linkset'],
                                           'properties': {'linkset': {'type': 'array',
                                                                      'items': {'type': 'object'}}}}}}),
        '/.well-known/endpoints': op('wellKnownEndpoints', 'Every service and action (JSON)',
                                     {'application/json': {'schema': {'type': 'object', 'properties': {
                                         'baseUrl': {'type': 'string', 'format': 'uri'},
                                         'services': {'type': 'array', 'items': {'type': 'object'}}}}}}),
    }


def build_openapi():
    """The description in OpenAPI 3.0.3 (the fragments are written in 3.0 style)."""
    tags, paths = [], {}
    for svc in ORDER:
        frag = json.load(open(os.path.join(ROOT, 'docs', 'openapi', f'{svc}.json'), encoding='utf-8'))
        tags.append(frag['tag'])
        # GET /v1/<service>: the service's actions.
        paths[f'/{svc}'] = {'get': {
            'tags': [svc], 'operationId': f'{svc}Index', 'summary': f'Actions of {svc}',
            'description': 'The service description and its action names.',
            'responses': {'200': {'description': 'Service index', 'content': {'application/json': {
                'schema': {'$ref': '#/components/schemas/ServiceIndex'}}}}}}}
        for p, ops in frag['paths'].items():
            if p in paths:
                raise SystemExit(f'duplicate path {p}')
            paths[p] = ops
    tags.append({'name': 'discovery', 'description': 'Service index and API documents.'})
    for p, item in _discovery_paths().items():
        paths[p] = item
    for item in paths.values():
        for m, o in item.items():
            if m in METHODS:
                # Any operation can fail with a service error (500) or a gateway error (429/502/504).
                o['responses'].setdefault('default', dict(ERROR_REF))
    return {
        'openapi': '3.0.3',
        'info': {
            'title': 'Test Helper API',
            'version': VERSION,
            'description': (
                'Helper endpoints for end-to-end and load test automation: shared test-data '
                'lists, regional names, locks, an email catcher and sender with attachment '
                'search, a webhook catcher, TOTP codes, QR codes, performance statistics, '
                'reconciliation of submitted against processed records '
                'and spoken utterances for voice interfaces, plus build information and an end-to-end '
                'self-test of the deployment (health). Every response carries an X-Build header '
                'naming the deployment that produced it. '
                'Every action answers GET; parameter names are case-insensitive; no '
                'authentication (except the SMS capture app\'s device key). An LLM-oriented '
                'guide is at {BASE_URL}/llms.txt. Import this document into Postman '
                '(Import > Link) to generate a collection.'),
        },
        'servers': [{'url': '{BASE_URL}', 'description': 'Test Helper API v1'}],
        # Open by design; sms/receive declares its own requirement (the device key).
        'security': [],
        'tags': tags,
        'paths': paths,
        'components': {
            'schemas': {'Error': ERROR_SCHEMA, 'ListRecord': LIST_RECORD,
                        'ServiceIndex': SERVICE_INDEX, 'Build': BUILD_SCHEMA},
            'responses': {'Error': {
                'description': 'Error: {"error": "...", "status": N} from a service, or '
                               '{"error": "...", "gatewayError": "THROTTLED"} from API Gateway.',
                'content': {'application/json': {
                    'schema': {'$ref': '#/components/schemas/Error'},
                    'example': {'error': "Missing required query parameter 'key'.",
                                'status': 400}}}}},
            'securitySchemes': {'smsDeviceKey': {
                'type': 'apiKey', 'in': 'header', 'name': 'X-Device-Key',
                'description': 'Secret device key built into the SMS capture app and configured '
                               'on the stack (SmsDeviceKeys). Only sms/receive needs it.'}},
        },
    }


# ------------------------------------------------------------------ OpenAPI 3.2
TAG_SUMMARY = {'lists': 'Lists', 'names': 'Names', 'mail': 'Mail', 'sms': 'SMS', 'totp': 'TOTP',
               'webhook': 'Webhooks', 'lock': 'Locks', 'qr': 'QR codes', 'perf': 'Performance',
               'recon': 'Reconciliation',
               'voice': 'Voice', 'health': 'Health', 'trace': 'Traces', 'discovery': 'Discovery'}


def _schema32(s):
    """A 3.0 Schema Object as JSON Schema 2020-12 (OAS 3.1+)."""
    if isinstance(s, list):
        return [_schema32(x) for x in s]
    if not isinstance(s, dict):
        return s
    s = dict(s)
    for k in ('properties', 'patternProperties'):
        if k in s:
            s[k] = {n: _schema32(v) for n, v in s[k].items()}
    for k in ('items', 'additionalProperties', 'not'):
        if isinstance(s.get(k), dict):
            s[k] = _schema32(s[k])
    for k in ('allOf', 'anyOf', 'oneOf'):
        if k in s:
            s[k] = [_schema32(x) for x in s[k]]
    if s.get('format') == 'byte':
        s.pop('format')
        s['contentEncoding'] = 'base64'
    if 'example' in s:
        s['examples'] = [s.pop('example')]
    if s.pop('nullable', False):
        if 'type' in s:
            s['type'] = [s['type'], 'null']
            if 'enum' in s and None not in s['enum']:
                s['enum'] = s['enum'] + [None]
        elif s:
            s = {'anyOf': [s, {'type': 'null'}]}
    return s


def _media32(media_type, m):
    m = dict(m)
    sch = m.get('schema')
    if isinstance(sch, dict) and sch.get('format') == 'binary':
        # A binary body: no schema needed; contentMediaType says what it is.
        m['schema'] = {k: v for k, v in sch.items() if k not in ('format', 'type')}
        # (JSON imports also accept JSON Lines, so application/json stays raw bytes.)
        if '*' not in media_type and media_type not in ('application/octet-stream', 'application/json'):
            m['schema']['contentMediaType'] = media_type
    elif sch is not None:
        m['schema'] = _schema32(sch)
    return m


def _content32(content):
    return {mt: _media32(mt, m) for mt, m in content.items()}


def _form(v):
    return json.dumps(v) if isinstance(v, bool) else str(v)


def _querystring(op, spec):
    """Free-form query (x-free-form-query): all query parameters become one 3.2
    `in: querystring` parameter whose form-encoded object schema allows the extra names."""
    ff = op.pop('x-free-form-query')
    params = [p for p in op.get('parameters', []) if p.get('in') != 'query']
    query = [p for p in op.get('parameters', []) if p.get('in') == 'query']
    props, required, example = {}, [], {}
    for p in query:
        # The named example parameters stay as properties, so tools still prefill them.
        props[p['name']] = dict(_schema32(p.get('schema', {})), description=p.get('description', ''))
        if p.get('required'):
            required.append(p['name'])
        if 'example' in p:
            example[p['name']] = p['example']
    schema = {'type': 'object', 'properties': props}
    if required:
        schema['required'] = required
    for k in ('additionalProperties', 'patternProperties'):
        if k in ff:
            schema[k] = _schema32(ff[k])
    params.append({
        'name': 'query', 'in': 'querystring', 'required': bool(required),
        'description': ff['description'],
        'content': {'application/x-www-form-urlencoded': {
            'schema': schema,
            'examples': {'typical': {'dataValue': example,
                                     'serializedValue': urllib.parse.urlencode({k: _form(v) for k, v in example.items()})}}}}})
    op['parameters'] = params


def to_oas32(spec30):
    spec = json.loads(json.dumps(spec30))
    spec['openapi'] = '3.2.1'
    spec = {'openapi': spec['openapi'], '$self': '{BASE_URL}/openapi.json',
            **{k: v for k, v in spec.items() if k != 'openapi'}}
    spec['servers'] = [dict(s, name='v1') for s in spec['servers']]
    spec['tags'] = [dict(t, summary=TAG_SUMMARY.get(t['name'], t['name'])) for t in spec['tags']]
    comp = spec['components']
    comp['schemas'] = {n: _schema32(s) for n, s in comp['schemas'].items()}
    for r in comp['responses'].values():
        r['content'] = _content32(r['content'])
    for path, item in spec['paths'].items():
        for srv in item.get('servers', []):
            srv['name'] = 'root' if srv['url'] == '{ROOT_URL}' else 'v1'
        for m, op in item.items():
            if m not in METHODS:
                continue
            if 'x-free-form-query' in op:
                _querystring(op, spec)
            for p in op.get('parameters', []):
                if 'schema' in p:
                    p['schema'] = _schema32(p['schema'])
                if 'content' in p:
                    p['content'] = _content32(p['content'])
            if 'requestBody' in op:
                op['requestBody']['content'] = _content32(op['requestBody']['content'])
            for r in op['responses'].values():
                if 'content' in r:
                    r['content'] = _content32(r['content'])
                for h in (r.get('headers') or {}).values():
                    if 'schema' in h:
                        h['schema'] = _schema32(h['schema'])
    spec['info']['description'] += (' The same API in OpenAPI 3.0.3, for tools that do not '
                                    'read 3.2 yet: {BASE_URL}/openapi-3.0.json.')
    return spec


def strip_extensions(spec30):
    spec = json.loads(json.dumps(spec30))
    for item in spec['paths'].values():
        for m, op in item.items():
            if isinstance(op, dict):
                op.pop('x-free-form-query', None)
    return spec


def build_llms():
    hand = os.path.join(ROOT, 'docs', 'llms.md')
    if os.path.exists(hand):
        return open(hand, encoding='utf-8').read().rstrip() + '\n'
    parts = [open(os.path.join(ROOT, 'docs', 'guide', '_intro.md'), encoding='utf-8').read().rstrip()]
    for svc in ORDER:
        text = open(os.path.join(ROOT, 'docs', 'guide', f'{svc}.md'), encoding='utf-8').read().strip()
        # The intro defines {BASE} once; drop each section's own reminder.
        text = text.replace('`{BASE}` is `https://api.<domain>/v1`. ', '')
        for old, new in (('https://api.example.com/v1', '{BASE}'),
                         ('<mail domain>', '{MAIL_DOMAIN}'),
                         ('mail.example.com', '{MAIL_DOMAIN}')):
            text = text.replace(old, new)
        parts.append(text)
    parts.append('## Discovery\n\n```\n{BASE}                  # service index (JSON)\n'
                 '{BASE}/<service>        # actions for one service\n'
                 '{BASE}/openapi.json     # OpenAPI 3.2.1\n{BASE}/llms.txt         # this guide\n'
                 '```')
    return '\n\n'.join(parts).replace('{BASE}', '{BASE_URL}') + '\n'


def _esc(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def build_site(spec):
    """docs/site/index.html with the service cards generated from the spec."""
    cards = []
    for tag in spec['tags']:
        name = tag['name']
        if name == 'discovery':
            continue
        items, seen = [], set()
        for path, ops in spec['paths'].items():
            parts = path.strip('/').split('/')
            if parts[0] != name or len(parts) < 2:
                continue
            action = '/'.join(parts[1:])
            if action in seen:
                continue
            seen.add(action)
            summary = next((o.get('summary', '') for o in ops.values() if isinstance(o, dict)), '')
            items.append(f'          <li><code>{_esc(action)}</code><span>{_esc(summary)}</span></li>')
        desc = tag.get('description', '').split('. ')[0].rstrip('.') + '.'
        cards.append('\n'.join([
            '      <div class="card">',
            f'        <h3><code>{_esc(name)}</code></h3>',
            f'        <p class="muted">{_esc(desc)}</p>',
            '        <ul>', *items, '        </ul>',
            '      </div>']))
    page = open(os.path.join(ROOT, 'docs', 'site', 'index.html'), encoding='utf-8').read()
    return page.replace('{SERVICE_CARDS}', '\n'.join(cards))


def main():
    check = '--check' in sys.argv
    spec30 = build_openapi()
    spec = to_oas32(spec30)
    openapi = json.dumps(spec, indent=1, ensure_ascii=False) + '\n'
    openapi30 = json.dumps(strip_extensions(spec30), indent=1, ensure_ascii=False) + '\n'
    llms = build_llms()
    site = build_site(spec)
    outputs = {os.path.join(ROOT, 'src', 'discovery_openapi.json'): openapi,
               os.path.join(ROOT, 'src', 'discovery_openapi30.json'): openapi30,
               os.path.join(ROOT, 'src', 'discovery_llms.md'): llms,
               os.path.join(ROOT, 'src', 'discovery_site.html'): site}
    stale = []
    for path, content in outputs.items():
        current = open(path, encoding='utf-8').read() if os.path.exists(path) else None
        if current != content:
            stale.append(os.path.relpath(path, ROOT))
            if not check:
                open(path, 'w', encoding='utf-8').write(content)
    if check and stale:
        raise SystemExit('stale: ' + ', '.join(stale) + ' - run python tools/build_docs.py')
    print('up to date' if not stale else 'built: ' + ', '.join(stale))


if __name__ == '__main__':
    main()
