"""Cross-checks template.yaml against the code and the OpenAPI document.

For every documented v1 path: find the API Gateway resource the template would
match (static resources win over {proxy+}, as in API Gateway), build the event
API Gateway would send, invoke that function's handler, and assert the action
is recognised (anything but the router's "Unknown action" 404).
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml
from moto import mock_aws

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.environ.update({'AWS_DEFAULT_REGION': 'ap-southeast-2', 'AWS_ACCESS_KEY_ID': 'x',
                   'AWS_SECRET_ACCESS_KEY': 'x', 'BASE_URL': 'https://api.example.com/v1'})
for v in ('LISTS_TABLE', 'VARS_TABLE', 'COUNTERS_TABLE', 'CLEANUP_TABLE', 'IMPORT_BUCKET',
          'WORKER_FUNCTION', 'MAIL_TABLE', 'INBOUND_BUCKET', 'ATTACHMENT_BUCKET',
          'WEBHOOK_TABLE', 'TOTP_TABLE', 'LOCK_TABLE', 'PERF_TABLE', 'VOICE_BUCKET', 'VOICE_TABLE'):
    os.environ.setdefault(v, 'unused')
os.environ.setdefault('MAIL_DOMAIN', 'example.com')


class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}


CfnLoader.add_multi_constructor('!', _tag)
TEMPLATE = yaml.load(open(os.path.join(ROOT, 'template.yaml')), Loader=CfnLoader)
RES = TEMPLATE['Resources']


def _v1_routes():
    """[(path_parts, function_logical_id)] for every V1Api method."""
    def path_of(ref):
        if isinstance(ref, dict) and 'GetAtt' in ref:
            return []
        r = RES[ref['Ref']]['Properties']
        return path_of(r['ParentId']) + [r['PathPart']]

    routes = []
    for lid, r in RES.items():
        if r['Type'] != 'AWS::ApiGateway::Method':
            continue
        p = r['Properties']
        if p['RestApiId'] != {'Ref': 'V1Api'}:
            continue
        uri = p['Integration']['Uri']['Sub']
        fn = re.search(r'\$\{(\w+)\.Arn\}', uri).group(1)
        routes.append((path_of(p['ResourceId']), fn))
    return routes


ROUTES = _v1_routes()


def match(parts):
    """API Gateway matching: exact static path first, else the deepest {proxy+}."""
    for rp, fn in ROUTES:
        if rp == parts:
            return rp, fn, None
    best = None
    for rp, fn in ROUTES:
        if rp and rp[-1] == '{proxy+}' and parts[:len(rp) - 1] == rp[:-1] and len(parts) >= len(rp):
            if best is None or len(rp) > len(best[0]):
                best = (rp, fn, '/'.join(parts[len(rp) - 1:]))
    return best


def event(rp, proxy, method='GET'):
    return {'httpMethod': method, 'resource': '/' + '/'.join(rp),
            'path': '/v1/' + '/'.join(rp), 'pathParameters': {'proxy': proxy} if proxy else None,
            'queryStringParameters': None, 'headers': {'Host': 'api.example.com'},
            'body': None, 'isBase64Encoded': False,
            'requestContext': {'identity': {'sourceIp': '203.0.113.1'}}}


SPEC = json.load(open(os.path.join(ROOT, 'src', 'discovery_openapi.json')))
# Service actions (not the discovery documents, nor GET /v1/<service> itself).
DOC_PATHS = [p for p in SPEC['paths'] if p.count('/') >= 2 and not p.startswith('/.')]


def test_every_service_has_resources():
    services = {p.strip('/').split('/')[0] for p in DOC_PATHS}
    top = {rp[0] for rp, _ in ROUTES if rp}
    assert services <= top, services - top


def test_every_function_handler_exists():
    for lid, r in RES.items():
        if r['Type'] != 'AWS::Lambda::Function':
            continue
        mod, fn = r['Properties']['Handler'].split('.')
        assert os.path.exists(os.path.join(ROOT, 'src', mod + '.py')) or \
            os.path.exists(os.path.join(ROOT, 'src', mod + '.js')), (lid, mod)


@pytest.mark.parametrize('path', [p for p in DOC_PATHS if not p.startswith('/lists/')])
def test_python_route_is_recognised(path):
    parts = path.strip('/').split('/')
    rp, fn, proxy = match(parts)
    module = RES[fn]['Properties']['Handler'].split('.')[0]
    mod = __import__(module)
    with mock_aws():
        res = mod.handler(event(rp, proxy), None)
    body = res.get('body') or ''
    assert not (res['statusCode'] == 404 and 'Unknown action' in body), (path, fn, body[:200])


def test_mail_inbox_actions_route_to_small_function():
    for a in ('pop', 'reset', 'clear'):
        _, fn, proxy = match(['mail', a])
        assert fn == 'MailInboxFunction' and proxy is None
    assert match(['mail', 'send'])[1] == 'MailFunction'


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_lists_routes_are_recognised():
    node_modules = os.path.join(HERE, 'node', 'node_modules')
    if not os.path.isdir(node_modules):
        pytest.skip('run npm install in tests/node first')
    cases = []
    for path in DOC_PATHS:
        if path.startswith('/lists/'):
            rp, fn, proxy = match(path.strip('/').split('/'))
            assert fn == 'ListsFunction', path
            cases.append(event(rp, proxy))
    script = (
        "const h=require(process.argv[1]).handler;"
        "const cases=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "(async()=>{const out=[];for(const e of cases){const r=await h(e);"
        "out.push([e.pathParameters&&e.pathParameters.proxy,r.statusCode,String(r.body).slice(0,120)]);}"
        "console.log(JSON.stringify(out));})();")
    env = dict(os.environ, NODE_PATH=node_modules, AWS_ENDPOINT_URL='http://127.0.0.1:9')
    proc = subprocess.run(['node', '-e', script, os.path.join(ROOT, 'src', 'lists.js')],
                          input=json.dumps(cases), capture_output=True, text=True, env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    for proxy, status, body in json.loads(proc.stdout):
        assert not (status == 404 and 'Unknown action' in body), (proxy, body)
