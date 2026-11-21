#!/usr/bin/env python3
"""Cross-check the Terraform configuration against ../template.yaml.

Compares, field by field: every Lambda function (runtime, handler, memory,
timeout, role, environment), every IAM statement, every DynamoDB table, every
API route and Lambda permission, the S3 lifecycle and CORS rules, the gateway
responses and the outputs. It does not need Terraform or AWS - it reads both
files - so it catches translation mistakes, not provider-schema ones (that is
what `terraform validate` is for).

    pip install python-hcl2 pyyaml
    python terraform/tests/check_parity.py
"""
import glob
import os
import re
import sys

import hcl2
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TF = os.path.dirname(HERE)
ROOT = os.path.dirname(TF)
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


# ------------------------------------------------------------------ load CFN
class Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {'!' + suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {'!' + suffix: loader.construct_sequence(node, deep=True)}
    return {'!' + suffix: loader.construct_mapping(node, deep=True)}


Loader.add_multi_constructor('!', _tag)
cfn = yaml.load(open(os.path.join(ROOT, 'template.yaml'), encoding='utf-8'), Loader=Loader)
R = cfn['Resources']

# ------------------------------------------------------------------ load TF
tf = {'locals': {}, 'resource': {}, 'variable': {}, 'output': {}}
def unq(x):
    """hcl2 keeps the quotes of quoted keys and labels; drop them."""
    if isinstance(x, dict):
        return {(k[1:-1] if isinstance(k, str) and len(k) > 1 and k[0] == k[-1] == '"' else k): unq(v)
                for k, v in x.items()}
    if isinstance(x, list):
        return [unq(v) for v in x]
    if isinstance(x, str) and len(x) > 1 and x[0] == x[-1] == '"':
        return x[1:-1].replace('\\"', '"')
    return x


for f in glob.glob(os.path.join(TF, '*.tf')):
    d = unq(hcl2.load(open(f, encoding='utf-8')))
    for block in d.get('locals', []):
        tf['locals'].update(block)
    for block in d.get('resource', []):
        for rtype, body in block.items():
            for name, attrs in body.items():
                tf['resource'][(rtype.strip('"'), name.strip('"'))] = attrs
    for kind in ('variable', 'output'):
        for block in d.get(kind, []):
            tf[kind].update({k.strip('"'): v for k, v in block.items()})
L = tf['locals']
VARS = {k: v.get('default') for k, v in tf['variable'].items()}


def s(x):
    """hcl2 leaves expressions as '${...}' strings; strip the wrapper."""
    x = str(x)
    m = re.fullmatch(r'\$\{(.*)\}', x, re.S)
    return m.group(1).strip() if m else x


# ------------------------------------------------------------------ name maps
FN = {  # CFN function logical id -> TF functions key
    'ListsFunction': 'lists', 'ListsWorkerFunction': 'lists_worker', 'MailFunction': 'mail',
    'MailInboxFunction': 'mail_inbox', 'MailReceiveFunction': 'mail_receive',
    'WebhookFunction': 'webhook', 'TotpFunction': 'totp', 'LockFunction': 'lock',
    'PerfFunction': 'perf', 'ReconFunction': 'recon', 'SmsFunction': 'sms', 'VoiceFunction': 'voice',
    'NamesFunction': 'names', 'QrFunction': 'qr',
    'DiscoveryFunction': 'discovery', 'HealthFunction': 'health', 'TraceFunction': 'trace',
}
ROLE = {
    'ListsRole': 'lists', 'ListsWorkerRole': 'lists_worker', 'MailRole': 'mail',
    'MailReceiveRole': 'mail_receive', 'WebhookRole': 'webhook', 'TotpRole': 'totp',
    'LockRole': 'lock', 'PerfRole': 'perf', 'ReconRole': 'recon', 'SmsRole': 'sms', 'VoiceRole': 'voice',
    'StatelessRole': 'stateless', 'HealthRole': 'health', 'TraceRole': 'trace',
}
TABLE = {
    'ListsTable': 'lists', 'ListsVarsTable': 'lists_vars', 'ListsCountersTable': 'lists_counters',
    'ListsCleanupTable': 'lists_cleanup', 'MailTable': 'mail', 'WebhookTable': 'webhook',
    'TotpTable': 'totp', 'PerfTable': 'perf', 'ReconTable': 'recon', 'SmsTable': 'sms', 'LockTable': 'lock',
    'VoiceTable': 'voice', 'HealthTable': 'health',
}
BUCKET = {'ListsImportBucket': 'imports', 'MailAttachmentBucket': 'attachments',
          'MailInboundBucket': 'inbound', 'VoiceBucket': 'voice'}
BUCKET_SUB = {'imports': 'imports', 'mail-attachments': 'attachments', 'mail-inbound': 'inbound',
              'voice': 'voice'}
PARAM_VAR = {
    'EmailSendAllowedTo': 'join(",", var.email_send_allowed_to)',
    'EmailSendAllowedDomains': 'join(",", var.email_send_allowed_domains)',
    'WebhookRetentionDays': 'tostring(var.webhook_retention_days)',
    'SmsDeviceKeys': 'join(",", local.sms_device_keys)',
    'SmsSendAllowedNumbers': 'join(",", var.sms_send_allowed_numbers)',
    'SmsRegion': 'var.sms_region',
    'SmsSendIntervalSeconds': 'tostring(var.sms_send_interval_seconds)',
    'VoiceRegion': 'var.voice_region',
    'VoiceName': 'var.voice_name',
    'VoiceEngine': 'var.voice_engine',
    'VoiceDailyCharacters': 'tostring(var.voice_daily_characters)',
    'VoiceLanguage': 'var.voice_language',
    'VoiceDailyListenSeconds': 'tostring(var.voice_daily_listen_seconds)',
    'Domain': 'var.domain',
    'BuildId': 'local.build_id',
    'BuildCommit': 'var.build_commit',
    'DeployedTime': 'tostring(local.deployed_time)',
}


def tf_table_ref(key, attr):
    if key == 'sms':
        return f'aws_dynamodb_table.sms.{attr}'
    if key in ('lock', 'health', 'recon'):
        return f'aws_dynamodb_table.{key}.{attr}'
    return f'aws_dynamodb_table.this["{key}"].{attr}'


def cfn_env_value(v):
    """CFN env value -> the TF expression it should translate to."""
    if isinstance(v, dict) and '!Ref' in v:
        ref = v['!Ref']
        if ref in TABLE:
            return tf_table_ref(TABLE[ref], 'name')
        if ref in BUCKET:
            return f'aws_s3_bucket.this["{BUCKET[ref]}"].id'
        if ref == 'ListsWorkerFunction':
            return 'local.fn_name.lists_worker'
        return PARAM_VAR[ref]
    if isinstance(v, dict) and '!Sub' in v:
        sub = v['!Sub']
        if sub == 'https://${ApiHostLabel}.${Domain}/v1':
            return 'local.base_url'
        if sub == '${AWS::StackName}-':
            return '${var.name_prefix}-'
        if re.fullmatch(r'(\w+=\$\{\w+Function\},?)+', sub):
            # HEALTH_FUNCTIONS: key=<function name>,... for every function but health.
            pairs = [p.split('=') for p in sub.split(',')]
            check(sorted(k for k, _ in pairs) == sorted(k for k in FN.values() if k != 'health'),
                  f'HEALTH_FUNCTIONS keys {[k for k, _ in pairs]} are not every other function')
            check(all(FN.get(v[2:-1]) == k for k, v in pairs), 'HEALTH_FUNCTIONS maps a key to another function')
            return 'join(",", [for k, n in local.fn_name : "${k}=${n}" if k != "health"])'
        m = re.fullmatch(r'\$\{BucketPrefix\}-(.+)-\$\{AWS::AccountId\}-\$\{AWS::Region\}', sub)
        if m:
            return f'local.bucket.{BUCKET_SUB[m.group(1)]}'
    raise ValueError(f'unmapped env value {v!r}')


def cfn_arn(r):
    """CFN IAM Resource -> normalised TF form."""
    if isinstance(r, str):
        return r
    if '!GetAtt' in r:
        logical = r['!GetAtt'].split('.')[0]
        if logical in TABLE:
            k = TABLE[logical]
            return {'sms': 'aws_dynamodb_table.sms.arn', 'lock': 'aws_dynamodb_table.lock.arn',
                    'health': 'aws_dynamodb_table.health.arn',
                    'recon': 'aws_dynamodb_table.recon.arn'}.get(k, f'local.t.{k}')
        if logical == 'ListsWorkerFunction':
            return 'lambda:function:lists_worker'
    sub = r['!Sub']
    if sub == '${SmsTable.Arn}/index/*':
        return '${aws_dynamodb_table.sms.arn}/index/*'
    if sub == '${ReconTable.Arn}/index/*':
        return '${aws_dynamodb_table.recon.arn}/index/*'
    m = re.fullmatch(r'arn:\$\{AWS::Partition\}:s3:::\$\{BucketPrefix\}-(.+?)-\$\{AWS::AccountId\}-\$\{AWS::Region\}(/.*)?', sub)
    if m:
        b = BUCKET_SUB[m.group(1)]
        return f'${{local.bucket_arn.{b}}}{m.group(2)}' if m.group(2) else f'local.bucket_arn.{b}'
    if sub == 'arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${AWS::StackName}-*':
        return 'lambda:function:<prefix>-*'
    m = re.fullmatch(r'arn:\$\{AWS::Partition\}:ses:\$\{AWS::Region\}:\$\{AWS::AccountId\}:(.*)', sub)
    if m:
        return '${local.ses_identity_arn}:' + m.group(1).replace('${Domain}', '${var.domain}')
    raise ValueError(f'unmapped ARN {r!r}')


def tf_arn(r):
    r = str(r)
    if 'function:${local.fn_name.lists_worker}' in r:
        return 'lambda:function:lists_worker'
    if r == 'arn:${local.partition}:lambda:${local.region}:${local.account}:function:${var.name_prefix}-*':
        return 'lambda:function:<prefix>-*'
    return s(r) if re.fullmatch(r'\$\{[^}]*\}', r) else r


# ------------------------------------------------------------------ functions
fns = L['functions']
cfn_fns = {k: v for k, v in R.items() if v['Type'] == 'AWS::Lambda::Function' and k != 'SesActivatorFunction'}
check(set(FN) == set(cfn_fns), f'function sets differ: {set(FN) ^ set(cfn_fns)}')
check(set(FN.values()) == set(fns), f'TF functions differ: {set(FN.values()) ^ set(fns)}')
check(set(L['fn_name']) == set(fns), 'fn_name keys != functions keys')
for logical, key in FN.items():
    p, t = cfn_fns[logical]['Properties'], fns[key]
    rt = {'python3.12': 'var.python_runtime', 'nodejs24.x': 'var.nodejs_runtime'}[p['Runtime']]
    check(s(t['runtime']) == rt, f'{key}: runtime {t["runtime"]} vs {p["Runtime"]}')
    check(VARS[rt.split('.')[1]] == p['Runtime'], f'{key}: default runtime differs')
    check(t['handler'] == p['Handler'], f'{key}: handler {t["handler"]} vs {p["Handler"]}')
    check(t['memory'] == p['MemorySize'], f'{key}: memory {t["memory"]} vs {p["MemorySize"]}')
    check(t['timeout'] == p['Timeout'], f'{key}: timeout {t["timeout"]} vs {p["Timeout"]}')
    check(t['role'] == ROLE[p['Role']['!GetAtt'].split('.')[0]], f'{key}: role {t["role"]}')
    tc = (p.get('TracingConfig') or {}).get('Mode')
    check(tc == {'!If': ['UseTracing', 'Active', 'PassThrough']}, f'{key}: TracingConfig {tc}')
    env = t['env']
    if isinstance(env, str):
        env = L[s(env).split('.')[1]]
    # The resource merges local.build_env into every function's environment.
    env = {**L['build_env'], **env}
    cenv = p.get('Environment', {}).get('Variables', {})
    check(set(env) == set(cenv), f'{key}: env keys differ {set(env) ^ set(cenv)}')
    for k, v in cenv.items():
        if k in env:
            check(s(env[k]) == cfn_env_value(v), f'{key}: env {k} = {s(env[k])} expected {cfn_env_value(v)}')

# ------------------------------------------------------------------ IAM
rp = L['role_policies']
cfn_roles = {k: v for k, v in R.items() if v['Type'] == 'AWS::IAM::Role' and k != 'SesActivatorRole'}
check(set(ROLE) == set(cfn_roles), f'role sets differ: {set(ROLE) ^ set(cfn_roles)}')
for logical, key in ROLE.items():
    p = cfn_roles[logical]['Properties']
    check(any('AWSLambdaBasicExecutionRole' in str(m) for m in p['ManagedPolicyArns']), f'{logical} lacks logs')
    stmts = [st for pol in p.get('Policies', []) for st in pol['PolicyDocument']['Statement']]
    tst = rp[key]
    check(len(stmts) == len(tst), f'{key}: {len(tst)} statements vs {len(stmts)}')
    for i, (c, t) in enumerate(zip(stmts, tst)):
        check(c['Effect'] == t['Effect'], f'{key}[{i}] effect')
        check(list(c['Action']) == list(t['Action']), f'{key}[{i}] actions {t["Action"]} vs {c["Action"]}')
        cr = [cfn_arn(r) for r in c['Resource']]
        tr = [tf_arn(r) for r in t['Resource']]
        check(cr == tr, f'{key}[{i}] resources\n   tf  {tr}\n   cfn {cr}')

# ------------------------------------------------------------------ tables
tables = L['tables']
for logical, key in TABLE.items():
    p = R[logical]['Properties']
    ks = {k['KeyType']: k['AttributeName'] for k in p['KeySchema']}
    types = {a['AttributeName']: a['AttributeType'] for a in p['AttributeDefinitions']}
    ttl = p.get('TimeToLiveSpecification', {}).get('AttributeName')
    retained = R[logical].get('DeletionPolicy') == 'Retain'
    if key in tables:
        t = tables[key]
        check(t['hash'] == [ks['HASH'], types[ks['HASH']]], f'table {key} hash')
        check(t['range'] == [ks['RANGE'], types[ks['RANGE']]], f'table {key} range')
        check(t['ttl'] == ttl, f'table {key} ttl {t["ttl"]} vs {ttl}')
        res = tf['resource'][('aws_dynamodb_table', 'this')]
    else:
        res = tf['resource'][('aws_dynamodb_table', key)]
        check(res['hash_key'] == ks['HASH'] and res.get('range_key') == ks.get('RANGE'), f'table {key} keys')
        tttl = res.get('ttl', [{}])[0].get('attribute_name') if res.get('ttl') else None
        check(tttl == ttl, f'table {key} ttl')
        if p.get('GlobalSecondaryIndexes'):
            g, tg = p['GlobalSecondaryIndexes'][0], res['global_secondary_index'][0]
            gks = {k['KeyType']: k['AttributeName'] for k in g['KeySchema']}
            check((tg['name'], tg['hash_key'], tg['range_key'], tg['projection_type']) ==
                  (g['IndexName'], gks['HASH'], gks['RANGE'], g['Projection']['ProjectionType']), f'table {key} GSI')
    protected = bool(res.get('lifecycle', [{}])[0].get('prevent_destroy')) if res.get('lifecycle') else False
    check(protected == retained, f'table {key}: prevent_destroy {protected} vs Retain {retained}')

# ------------------------------------------------------------------ API routes
cfn_routes = set()
paths = {}
for k, v in R.items():
    if v['Type'] == 'AWS::ApiGateway::Resource':
        paths[k] = v['Properties']
for k, v in R.items():
    if v['Type'] != 'AWS::ApiGateway::Method':
        continue
    p = v['Properties']
    api = p['RestApiId']['!Ref']
    rid = p['ResourceId']
    segs = []
    if isinstance(rid, dict) and '!Ref' in rid:
        cur = rid['!Ref']
        while True:
            segs.insert(0, paths[cur]['PathPart'])
            parent = paths[cur]['ParentId']
            if '!Ref' in parent:
                cur = parent['!Ref']
            else:
                break
    fn = re.search(r'functions/\$\{(\w+)\.Arn\}', p['Integration']['Uri']['!Sub']).group(1)
    check(p['HttpMethod'] == 'ANY' and p['Integration']['Type'] == 'AWS_PROXY', f'{k} method/type')
    cfn_routes.add(({'V1Api': 'v1', 'RootApi': 'root'}[api], '/' + '/'.join(segs), FN[fn]))

svc = L['services']
tf_routes = {('v1', '/', 'discovery'), ('v1', '/{proxy+}', 'discovery'),
             ('root', '/', 'discovery'), ('root', '/{proxy+}', 'discovery')}
for name, fn in svc.items():
    tf_routes.add(('v1', f'/{name}', fn))
    tf_routes.add(('v1', f'/{name}/{{proxy+}}', fn))
for a in L['mail_inbox_actions']:
    tf_routes.add(('v1', f'/mail/{a}', 'mail_inbox'))
check(cfn_routes == tf_routes, f'routes differ:\n   only CFN {sorted(cfn_routes - tf_routes)}\n   only TF  {sorted(tf_routes - cfn_routes)}')
routes_src = open(os.path.join(TF, 'api.tf'), encoding='utf-8').read()
for key in ('v1_root', 'v1_proxy', 'root_root', 'root_proxy'):
    check(key in routes_src, f'route {key} missing from local.routes')

# Lambda permissions for API Gateway
cfn_perm = set()
for k, v in R.items():
    if v['Type'] == 'AWS::Lambda::Permission' and v['Properties']['Principal'] == 'apigateway.amazonaws.com':
        fn = v['Properties']['FunctionName']['!Ref']
        api = re.search(r'\$\{(V1Api|RootApi)\}', v['Properties']['SourceArn']['!Sub']).group(1)
        cfn_perm.add((FN[fn], {'V1Api': 'v1', 'RootApi': 'root'}[api]))
tf_perm = {(k, v) for k, v in L['api_invokers'].items()} | {('discovery', 'v1'), ('discovery', 'root')}
check(cfn_perm == tf_perm, f'API invoke permissions differ: {cfn_perm ^ tf_perm}')
used = {fn for _a, _p, fn in cfn_routes}
check(used <= {f for f, _ in tf_perm}, f'routed functions without permission: {used - {f for f, _ in tf_perm}}')

# Gateway responses
gr = [v['Properties'] for v in R.values() if v['Type'] == 'AWS::ApiGateway::GatewayResponse']
tgr = tf['resource'][('aws_api_gateway_gateway_response', 'default')]
check(len(gr) == 4, 'expected 4 gateway responses in CFN')
check(tgr['response_templates']['application/json'] ==
      gr[0]['ResponseTemplates']['application/json'], 'gateway response template differs')
check(tgr['response_parameters'] == gr[0]['ResponseParameters'], 'gateway response headers differ')

# X-Ray tracing: stage, role policy, sampling rule
check(R['V1Stage']['Properties'].get('TracingEnabled') == {'!If': ['UseTracing', True, False]},
      'V1Stage TracingEnabled')
check(s(tf['resource'][('aws_api_gateway_stage', 'v1')].get('xray_tracing_enabled')) == 'var.tracing',
      'TF v1 stage xray_tracing_enabled')
check('tracing_config' in tf['resource'][('aws_lambda_function', 'this')], 'TF functions lack tracing_config')
check(('aws_iam_role_policy_attachment', 'xray') in tf['resource'], 'TF roles lack the X-Ray policy')
for logical in ROLE:
    check(any('AWSXRayDaemonWriteAccess' in str(m) for m in R[logical]['Properties']['ManagedPolicyArns']),
          f'{logical} lacks the X-Ray policy')
rule = R['HealthSamplingRule']['Properties']['SamplingRule']
trule = tf['resource'][('aws_xray_sampling_rule', 'health')]
check((rule['URLPath'], rule['FixedRate'], rule['ReservoirSize'], rule['Priority']) ==
      (trule['url_path'], trule['fixed_rate'], trule['reservoir_size'], trule['priority']),
      'health sampling rule differs')

# REST API settings
v1 = tf['resource'][('aws_api_gateway_rest_api', 'v1')]
check(v1['binary_media_types'] == R['V1Api']['Properties']['BinaryMediaTypes'], 'binary media types')
check(int(v1['minimum_compression_size']) == R['V1Api']['Properties']['MinimumCompressionSize'], 'compression')

# ------------------------------------------------------------------ buckets
def tf_rules(name):
    res = tf['resource'][('aws_s3_bucket_lifecycle_configuration', name)]
    out = []
    for r in res['rule']:
        prefix = (r.get('filter') or [{}])[0].get('prefix')
        exp = r['expiration'][0]['days'] if r.get('expiration') else None
        abort = r['abort_incomplete_multipart_upload'][0]['days_after_initiation'] if r.get('abort_incomplete_multipart_upload') else None
        out.append((r['id'], r['status'], prefix, exp, abort))
    return out


for logical, key in BUCKET.items():
    p = R[logical]['Properties']
    rules = [(r['Id'], r['Status'], r.get('Prefix'), r.get('ExpirationInDays'),
              r.get('AbortIncompleteMultipartUpload', {}).get('DaysAfterInitiation'))
             for r in p['LifecycleConfiguration']['Rules']]
    check(rules == tf_rules(key), f'bucket {key} lifecycle\n   tf  {tf_rules(key)}\n   cfn {rules}')
    check(R[logical].get('DeletionPolicy') == 'Retain', f'{logical} not retained in CFN?')
    if p.get('CorsConfiguration'):
        c = p['CorsConfiguration']['CorsRules'][0]
        t = tf['resource'][('aws_s3_bucket_cors_configuration', key)]['cors_rule'][0]
        check(t['allowed_methods'] == c['AllowedMethods'], f'{key} CORS methods')
        check(t['expose_headers'] == c['ExposedHeaders'] and t['max_age_seconds'] == c['MaxAge'], f'{key} CORS headers')
check(tf['resource'][('aws_s3_bucket', 'this')]['lifecycle'][0]['prevent_destroy'] is True, 'buckets not protected')

# ------------------------------------------------------------------ outputs
snake = {{'OpenApi': 'openapi'}.get(k, re.sub(r'(?<!^)(?=[A-Z])', '_', k).lower()) for k in cfn['Outputs']}
check(snake <= set(tf['output']), f'outputs missing: {snake - set(tf["output"])}')

# ------------------------------------------------------------------ parameters -> variables
for k in cfn['Parameters']:
    v = {'CreateReceiptRules': 'create_receipt_rule', 'ExistingReceiptRuleSet': 'existing_receipt_rule_set',
         'ActivateReceiptRuleSet': 'activate_receipt_rule_set',
         'ForceActivateReceiptRuleSet': None}.get(k, re.sub(r'(?<!^)(?=[A-Z])', '_', k).lower())
    if v:
        check(v in tf['variable'], f'parameter {k} has no variable {v}')

if errors:
    print('PARITY CHECK FAILED')
    for e in errors:
        print(' -', e)
    sys.exit(1)
print(f'parity OK: {len(FN)} functions, {len(ROLE)} roles, {len(TABLE)} tables, '
      f'{len(cfn_routes)} routes, {len(cfn_perm)} API permissions, {len(BUCKET)} buckets, '
      f'{len(snake)} outputs match template.yaml')
