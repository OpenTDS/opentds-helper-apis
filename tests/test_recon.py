"""Tests for the recon service: count-based (multiset) reconciliation, DynamoDB via moto."""
import base64
import collections
import json
import os
import random
import sys
import threading

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
os.environ.update({'AWS_DEFAULT_REGION': 'ap-southeast-2', 'AWS_ACCESS_KEY_ID': 'testing',
                   'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_SECURITY_TOKEN': 'testing',
                   'AWS_SESSION_TOKEN': 'testing', 'BASE_URL': 'https://api.example.com/v1'})

import perf   # noqa: E402
import recon  # noqa: E402


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    monkeypatch.setenv('RECON_TABLE', 'test-recon')
    monkeypatch.setenv('PERF_TABLE', 'test-recon-perf')
    monkeypatch.setattr(recon, 'POLL_S', 0.05)
    # One writer for batches: moto's updates are not atomic across threads (DynamoDB's
    # are). test_parallel_batch_writers covers the threaded path on distinct keys.
    monkeypatch.setattr(recon, 'WRITERS', 1)
    with mock_aws():
        recon._reset()
        perf._res = perf._tbl = None
        perf._registered.clear()
        c = boto3.client('dynamodb')
        c.create_table(
            TableName='test-recon', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': n, 'AttributeType': t}
                                  for n, t in (('PK', 'S'), ('SK', 'S'), ('gk', 'S'), ('gt', 'N'))],
            KeySchema=[{'AttributeName': 'PK', 'KeyType': 'HASH'},
                       {'AttributeName': 'SK', 'KeyType': 'RANGE'}],
            GlobalSecondaryIndexes=[{'IndexName': 'ByState', 'Projection': {'ProjectionType': 'ALL'},
                                     'KeySchema': [{'AttributeName': 'gk', 'KeyType': 'HASH'},
                                                   {'AttributeName': 'gt', 'KeyType': 'RANGE'}]}])
        c.create_table(
            TableName='test-recon-perf', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': 'PK', 'AttributeType': 'S'},
                                  {'AttributeName': 'SK', 'AttributeType': 'S'}],
            KeySchema=[{'AttributeName': 'PK', 'KeyType': 'HASH'},
                       {'AttributeName': 'SK', 'KeyType': 'RANGE'}])
        yield
        recon._reset()
        perf._res = perf._tbl = None


def event(action, qs=None, method='GET', body=None, ctype=None, b64=False):
    return {'httpMethod': method, 'resource': '/recon/{proxy+}', 'path': '/v1/recon/' + action,
            'pathParameters': {'proxy': action} if action else None,
            'queryStringParameters': qs,
            'headers': dict({'Host': 'api.example.com'}, **({'Content-Type': ctype} if ctype else {})),
            'body': body, 'isBase64Encoded': b64,
            'requestContext': {'identity': {'sourceIp': '203.0.113.10'}}}


def call_raw(action, qs=None, **kw):
    return recon.handler(event(action, qs, **kw), None)


def call(action, qs=None, **kw):
    res = call_raw(action, qs, **kw)
    return res['statusCode'], json.loads(res['body'])


def ok(action, qs=None, **kw):
    s, b = call(action, qs, **kw)
    assert s == 200, b
    return b


def post(action, key, values, **qs):
    return ok(action, dict({'key': key}, **{k: str(v) for k, v in qs.items()}), method='POST',
              body='\n'.join(values), ctype='text/plain')


def summary(key, **qs):
    return ok('get', dict({'key': key}, **{k: str(v) for k, v in qs.items()}))


def groups(key, status='all', **qs):
    return {g['value']: g for g in ok('list', dict({'key': key, 'status': status, 'limit': '1000'},
                                                    **qs))['groups']}


# ------------------------------------------------------------------ the simple case
def test_unique_values_three_ways():
    for v in ('a@x.com', 'b@x.com', 'c@x.com'):
        b = ok('expect', {'key': 'r1', 'value': v})
        assert b['status'] == 'short' and b['previousStatus'] is None and b['expected'] == 1
    b = ok('confirm', {'key': 'r1', 'value': 'a@x.com'})
    assert b['status'] == 'matched' and b['previousStatus'] == 'short' and b['latencyMs'] >= 0
    assert ok('confirm', {'key': 'r1', 'value': 'z@x.com'})['status'] == 'over'
    s = summary('r1')
    assert (s['expected'], s['confirmed'], s['matched'], s['outstanding'], s['unexpected'],
            s['mismatched'], s['ambiguousGroups']) == (3, 2, 1, 2, 1, 0, 0)
    assert s['groups'] == {'matched': 1, 'mismatched': 0, 'reconciledByCount': 0, 'short': 2, 'over': 1}
    assert s['reconciled'] is False and s['source'] == 'counters' and s['suffix'] == 0


def test_reconciled_and_strict():
    ok('expect', {'key': 'r2', 'value': 'one'})
    s, b = call('get', {'key': 'r2', 'strict': 'true'})
    assert s == 412 and b['reconciled'] is False and b['outstanding'] == 1
    ok('confirm', {'key': 'r2', 'value': 'one'})
    s, b = call('get', {'key': 'r2', 'strict': 'true'})
    assert s == 200 and b['reconciled'] is True


def test_empty_key_never_reconciles():
    s, b = call('get', {'key': 'nothing', 'strict': 'true'})
    assert s == 412 and b['expected'] == 0 and b['reconciled'] is False and 'comment' in b
    assert ok('list', {'key': 'nothing'})['groups'] == []
    assert ok('cyclepop', {'key': 'nothing'})['exhausted'] is True


# ------------------------------------------------------------------ count matching
def test_the_three_count_cases():
    """2 expected / 2 confirmed, 3 / 2 and 1 / 2 under one key - as the health probe does."""
    post('expect', 'cnt', ['A', 'A', 'B', 'B', 'B', 'C'])
    post('confirm', 'cnt', ['A', 'A', 'B', 'B', 'C', 'C'])
    g = groups('cnt')
    assert g['A']['status'] == 'reconciledByCount' and (g['A']['expected'], g['A']['confirmed']) == (2, 2)
    assert g['B']['status'] == 'short' and g['B']['shortBy'] == 1 and 'overBy' not in g['B']
    assert g['C']['status'] == 'over' and g['C']['overBy'] == 1
    s = summary('cnt')
    assert (s['expected'], s['confirmed'], s['matched'], s['outstanding'], s['unexpected']) == (6, 6, 5, 1, 1)
    assert s['ambiguousGroups'] == 3 and s['reconciled'] is False
    assert s['groups'] == {'matched': 0, 'mismatched': 0, 'reconciledByCount': 1, 'short': 1, 'over': 1}


def test_repeats_are_never_collapsed():
    ok('expect', {'key': 'rep', 'value': 'v'})
    b = ok('expect', {'key': 'rep', 'value': 'v'})
    assert b['expected'] == 2 and b['status'] == 'short'
    assert ok('confirm', {'key': 'rep', 'value': 'v'})['status'] == 'short'      # 2 vs 1: one missing
    b = ok('confirm', {'key': 'rep', 'value': 'v'})
    assert b['status'] == 'reconciledByCount' and b['previousStatus'] == 'short'
    assert ok('confirm', {'key': 'rep', 'value': 'v'})['status'] == 'over'       # a resent message
    s = summary('rep')
    assert (s['expected'], s['confirmed'], s['matched'], s['outstanding'], s['unexpected']) == (2, 3, 2, 0, 1)


def test_suffix_matches_full_values_with_masked_ones():
    post('expect', 'sfx', ['609841262', '609921262', '609850661'], suffix=4)
    b = ok('confirm', {'key': 'sfx', 'value': 'xxxxx1262'})
    assert b['match'] == '1262' and b['status'] == 'short' and b['suffix'] == 4
    b = ok('get', {'key': 'sfx', 'value': '609921262'})          # any value with that suffix
    g = b['group']
    assert b['match'] == '1262' and g['shortBy'] == 1
    assert g['candidates'] == ['609841262', '609921262'] and g['confirmedValues'] == ['xxxxx1262']
    ok('confirm', {'key': 'sfx', 'value': '1262'})               # a bare suffix works too
    g = ok('get', {'key': 'sfx', 'value': '1262'})['group']
    assert g['status'] == 'reconciledByCount' and g['confirmedValues'] == ['1262', 'xxxxx1262']
    short = groups('sfx', 'short')
    assert list(short) == ['0661'] and short['0661']['candidates'] == ['609850661']


def test_suffix_is_fixed_by_the_first_write():
    ok('expect', {'key': 'sf2', 'value': '123456', 'suffix': '4'})
    assert ok('confirm', {'key': 'sf2', 'value': '3456'})['status'] == 'matched'   # no suffix needed
    s, b = call('expect', {'key': 'sf2', 'value': 'x', 'suffix': '3'})
    assert s == 400 and 'last 4' in b['error'] and 'hint' in b
    assert call('expect', {'key': 'sf3', 'value': 'x', 'suffix': '-1'})[0] == 400
    assert call('expect', {'key': 'sf3', 'value': 'x', 'suffix': 'four'})[0] == 400
    assert ok('expect', {'key': 'sf4', 'value': '12', 'suffix': '4'})['match'] == '12'  # shorter than N


def test_whitespace_trimmed_and_value64():
    ok('expect', {'key': 'r5', 'value': '  jane+tag@x.com '})
    v64 = base64.b64encode(b'jane+tag@x.com').decode()
    assert ok('confirm', {'key': 'r5', 'value64': v64})['status'] == 'matched'


def test_ignorecase_fixed_by_first_write():
    b = ok('expect', {'key': 'r6', 'value': 'Jane.Doe@X.com', 'ignorecase': 'true'})
    assert b['ignorecase'] is True
    b = ok('confirm', {'key': 'r6', 'value': 'jane.doe@x.com'})
    assert b['status'] == 'matched' and b['match'] == 'jane.doe@x.com'
    g = ok('get', {'key': 'r6', 'value': 'JANE.DOE@x.COM'})['group']
    assert g['candidates'] == ['Jane.Doe@X.com'] and g['confirmedValues'] == ['jane.doe@x.com']
    s, b = call('expect', {'key': 'r6', 'value': 'x', 'ignorecase': 'false'})
    assert s == 400 and 'ignorecase' in b['error']
    ok('expect', {'key': 'r7', 'value': 'ABC'})
    assert ok('confirm', {'key': 'r7', 'value': 'abc'})['status'] == 'over'


def test_keys_from_the_first_version_are_refused():
    boto3.resource('dynamodb').Table('test-recon').put_item(
        Item={'PK': 'old#M', 'SK': 'M', 'ic': False, 'created': 1, 'exp': 9999999999})
    s, b = call('expect', {'key': 'old', 'value': 'v'})
    assert s == 400 and 'earlier version' in b['error'] and 'hint' in b
    assert ok('clear', {'key': 'old'})['cleared'] is True
    assert ok('expect', {'key': 'old', 'value': 'v'})['status'] == 'short'


# ------------------------------------------------------------------ fields
def test_fields_compared_only_when_the_key_is_unique():
    ok('expect', {'key': 'f1', 'value': 'A1', 'amount': '120.00', 'state': 'lodged'})
    assert ok('confirm', {'key': 'f1', 'value': 'A1', 'amount': '120'})['status'] == 'matched'
    ok('expect', {'key': 'f1', 'value': 'A2', 'amount': '50'})
    b = ok('confirm', {'key': 'f1', 'value': 'A2', 'amount': '55'})
    assert b['status'] == 'mismatched'
    assert b['differences'] == [{'field': 'amount', 'expected': '50', 'confirmed': '55'}]
    # Two records share A3: which confirmation pairs with which is unknown, so no comparison.
    post('expect', 'f1', ['A3', 'A3'])
    ok('confirm', {'key': 'f1', 'value': 'A3', 'amount': '1'})
    assert ok('confirm', {'key': 'f1', 'value': 'A3', 'amount': '2'})['status'] == 'reconciledByCount'
    s = summary('f1')
    assert s['mismatched'] == 1 and s['groups']['mismatched'] == 1 and s['reconciled'] is False
    g = groups('f1', 'mismatched')['A2']
    assert g['expectedFields'] == {'amount': '50'} and g['confirmedFields'] == {'amount': '55'}


def test_mismatch_clears_when_more_records_arrive():
    ok('expect', {'key': 'f3', 'value': 'A', 'amount': '1'})
    assert ok('confirm', {'key': 'f3', 'value': 'A', 'amount': '2'})['status'] == 'mismatched'
    assert ok('expect', {'key': 'f3', 'value': 'A'})['status'] == 'short'
    s = summary('f3')
    assert s['mismatched'] == 0 and s['groups']['mismatched'] == 0 and s['groups']['short'] == 1


def test_fields_param_requires_named_fields():
    ok('expect', {'key': 'f2', 'value': 'A1', 'amount': '10', 'state': 'new'})
    ok('confirm', {'key': 'f2', 'value': 'A1', 'amount': '10'})
    assert summary('f2')['groups']['matched'] == 1
    s = summary('f2', fields='amount,state')
    assert s['source'] == 'scan' and s['groups']['mismatched'] == 1 and s['fields'] == ['amount', 'state']
    assert s['latencyMs']['p50'] is not None
    lst = ok('list', {'key': 'f2', 'status': 'mismatched', 'fields': 'state'})
    assert lst['total'] == 1
    assert lst['groups'][0]['differences'] == [{'field': 'state', 'expected': 'new', 'confirmed': None}]


def test_reserved_and_misplaced_parameters():
    s, b = call('expect', {'key': 'p', 'value': 'v', 'wait': '5'})
    assert s == 400 and 'wait' in b['error'] and 'reserved' in b
    assert call('expect', {'key': 'p'})[0] == 400
    assert call('expect', {'key': 'p', 'value': '   '})[0] == 400
    s, b = call('expect', {'key': 'p', 'value': 'x' * 1001})
    assert s == 400 and 'bytes' in b['error']
    assert call('list', {'key': 'p', 'status': 'done'})[0] == 400
    for old, new in (('outstanding', 'short'), ('unexpected', 'over')):
        s, b = call('list', {'key': 'p', 'status': old})
        assert s == 400 and ("'%s'" % new) in b['error']
    assert call('list', {'key': 'p', 'start': '9950', 'limit': '100'})[0] == 400
    assert call('get', {'key': 'p', 'fields': ' , '})[0] == 400


# ------------------------------------------------------------------ batches
def test_post_text_json_csv():
    b = post('expect', 'b1', ['a@x.com', 'b@x.com', '', '# note', 'c@x.com'])
    assert b['format'] == 'text' and b['received'] == 3 and b['recorded'] == 3 and b['groups'] == 3
    body = json.dumps([{'value': 'a@x.com', 'Amount': 5}, 'b@x.com', {'value': 'q@x.com'}])
    b = ok('confirm', {'key': 'b1'}, method='POST', body=body, ctype='application/json')
    assert b['format'] == 'json' and b['paired'] == 2
    csv_body = 'value,amount\nc@x.com,7\n,9\n'
    b = ok('confirm', {'key': 'b1'}, method='POST', body=base64.b64encode(csv_body.encode()).decode(),
           ctype='text/csv', b64=True)
    assert b['format'] == 'csv' and b['recorded'] == 1 and b['skipped'] == 1 and b['warnings']
    s = summary('b1')
    assert (s['expected'], s['matched'], s['unexpected'], s['outstanding']) == (3, 3, 1, 0)
    assert ok('get', {'key': 'b1', 'value': 'a@x.com'})['group']['confirmedFields'] == {'amount': '5'}


def test_post_errors():
    assert call('expect', {'key': 'b2', 'value': 'v'}, method='POST', body='x')[0] == 400
    assert call('expect', {'key': 'b2', 'amount': '1'}, method='POST', body='x')[0] == 400
    assert call('expect', {'key': 'b2'}, method='POST', body='{"a": 1}')[0] == 400
    s, b = call('expect', {'key': 'b2'}, method='POST', body='name,email\nx,y\n')
    assert s == 400 and 'value' in b['error']
    assert call('expect', {'key': 'b2'}, method='POST', body='\n\n')[0] == 400
    s, b = call('expect', {'key': 'b2'}, method='POST', body='\n'.join(str(i) for i in range(5001)))
    assert s == 400 and b['maximum'] == 5000


def test_parallel_batch_writers(monkeypatch):
    """The batch path with several writer threads, on distinct keys (no group is
    contended, so moto's lack of atomicity cannot matter)."""
    monkeypatch.setattr(recon, 'WRITERS', 8)
    b = post('expect', 'c2', ['p%d' % i for i in range(64)])
    assert b['recorded'] == 64 and b['groups'] == 64
    assert summary('c2', fields='none')['expected'] == 64


def test_counters_agree_with_an_independent_multiset_count():
    """Random repeats, both orders, interleaved: the running counters, a full recount
    from the groups, and a plain Counter must all agree."""
    rng = random.Random(7)
    vals = ['k%02d' % rng.randrange(25) for _ in range(300)]
    ops = [('expect', v) for v in vals[:170]] + [('confirm', v) for v in vals[130:]]
    rng.shuffle(ops)
    for side in ('expect', 'confirm'):
        mine = [v for s, v in ops if s == side]
        for i in range(0, len(mine), 37):
            post(side, 'mx', mine[i:i + 37])
    e = collections.Counter(v for s, v in ops if s == 'expect')
    c = collections.Counter(v for s, v in ops if s == 'confirm')
    keys = set(e) | set(c)
    want = {'expected': sum(e.values()), 'confirmed': sum(c.values()),
            'matched': sum(min(e[k], c[k]) for k in keys),
            'outstanding': sum(max(0, e[k] - c[k]) for k in keys),
            'unexpected': sum(max(0, c[k] - e[k]) for k in keys),
            'ambiguousGroups': sum(1 for k in keys if max(e[k], c[k]) > 1)}
    for s in (summary('mx'), summary('mx', fields='nothing-to-compare')):
        assert {k: s[k] for k in want} == want
    g = groups('mx')
    assert len(g) == len(keys)
    for k in keys:
        assert (g[k]['expected'], g[k]['confirmed']) == (e[k], c[k])


def test_a_lost_state_update_is_put_right_by_the_next_record(monkeypatch):
    """Record A's follow-up (pointing the state index at its new state) must fail when
    record B moved the counts between A's ADD and A's follow-up; B's follow-up then
    leaves the right state. B is injected at exactly that moment."""
    ok('expect', {'key': 'race', 'value': 'v'})
    real = recon._update_group
    fired = {'done': False}

    def add_then_interleave(*a, **kw):
        res = real(*a, **kw)                  # A's ADD: 1 vs 1
        if not fired['done']:
            fired['done'] = True
            cfg = recon.settings('race')
            recon.write_one('race', cfg, 'confirm', 'v', {}, recon.now_ms())   # B: 1 vs 2
        return res

    monkeypatch.setattr(recon, '_update_group', add_then_interleave)
    b = ok('confirm', {'key': 'race', 'value': 'v'})
    assert b['status'] == 'matched'           # what A saw
    monkeypatch.setattr(recon, '_update_group', real)
    assert set(groups('race', 'over')) == {'v'} and not groups('race', 'matched')


def test_candidate_list_is_capped_not_the_count(monkeypatch):
    monkeypatch.setattr(recon, 'MAX_CANDIDATES', 3)
    post('expect', 'cap', ['10%d1234' % i for i in range(6)], suffix=4)
    g = ok('get', {'key': 'cap', 'value': '1234'})['group']
    assert g['expected'] == 6 and len(g['candidates']) == 3


# ------------------------------------------------------------------ get one, wait
def test_get_one_value():
    s, b = call('get', {'key': 'g1', 'value': 'none'})
    assert s == 404 and 'hint' in b
    ok('expect', {'key': 'g1', 'value': 'v1'})
    b = ok('get', {'key': 'g1', 'value': 'v1'})
    assert b['exists'] is True and b['status'] == 'short' and b['reconciled'] is False
    assert call('get', {'key': 'g1', 'value': 'v1', 'strict': 'true'})[0] == 412
    b = ok('get', {'key': 'g1', 'value': 'nope', 'wait': '1'})
    assert b['exists'] is False and b['group'] is None and b['waitedMs'] >= 500


def test_wait_returns_when_confirmed():
    ok('expect', {'key': 'w1', 'value': 'v'})
    timer = threading.Timer(0.3, lambda: call('confirm', {'key': 'w1', 'value': 'v'}))
    timer.start()
    b = ok('get', {'key': 'w1', 'wait': '5'})
    timer.join()
    assert b['outstanding'] == 0 and b['reconciled'] is True and b['waitedMs'] < 4000
    ok('expect', {'key': 'w1', 'value': 'v2'})
    b = ok('get', {'key': 'w1', 'value': 'v2', 'wait': '1'})
    assert b['status'] == 'short' and b['waitedMs'] >= 500


def test_grace_and_sla():
    ok('expect', {'key': 'g2', 'value': 'a'})
    ok('confirm', {'key': 'g2', 'value': 'a'})
    post('confirm', 'g2', ['stray', 'stray'])
    s = summary('g2')
    assert s['unexpected'] == 2 and s['reconciled'] is False and 'withinGrace' not in s
    s = summary('g2', grace=60)
    assert s['withinGrace'] == 2 and s['graceSeconds'] == 60 and s['reconciled'] is True
    post('expect', 'g2', ['late', 'late'])
    assert summary('g2', sla=3600)['overdue'] == 0
    t = boto3.resource('dynamodb').Table('test-recon')
    t.update_item(Key={'PK': recon.gpk('g2', recon.shard_of('late')), 'SK': 'late'},
                  UpdateExpression='SET et = :t', ExpressionAttributeValues={':t': 1000})
    assert summary('g2', sla=60)['overdue'] == 2


# ------------------------------------------------------------------ list
def test_list_by_status_unreconciled_and_paging():
    post('expect', 'l1', ['e%02d' % i for i in range(30)] + ['dup', 'dup'])
    post('confirm', 'l1', ['e%02d' % i for i in range(10)] + ['dup', 'dup', 'zz'])
    b = ok('list', {'key': 'l1', 'status': 'short', 'limit': '15'})
    assert b['total'] == 20 and b['returned'] == 15
    b2 = ok('list', {'key': 'l1', 'status': 'short', 'limit': '15', 'start': '16'})
    assert b2['returned'] == 5
    assert {g['value'] for g in b['groups'] + b2['groups']} == {'e%02d' % i for i in range(10, 30)}
    assert set(groups('l1', 'over')) == {'zz'} and groups('l1', 'over')['zz']['candidates'] == []
    assert set(groups('l1', 'reconciledByCount')) == {'dup'}
    assert len(groups('l1', 'unreconciled')) == 21
    b = ok('list', {'key': 'l1', 'status': 'all', 'limit': '1000'})
    assert b['total'] == 32 and b['returned'] == 32
    assert ok('list', {'key': 'l1', 'status': 'Short'})['status'] == 'short'      # case-insensitive


def test_list_csv_only_has_columns_in_use():
    ok('expect', {'key': 'l2', 'value': 'A1', 'amount': '5'})
    ok('confirm', {'key': 'l2', 'value': 'A1', 'amount': '6'})
    res = call_raw('list', {'key': 'l2', 'format': 'csv'})
    assert res['statusCode'] == 200 and res['headers']['Content-Type'].startswith('text/csv')
    lines = res['body'].strip().split('\n')
    assert lines[0] == ('value,status,expected,confirmed,shortBy,overBy,candidates,confirmedValues,'
                        'expectedTime,confirmedTime,latencyMs,differences,expected.amount,confirmed.amount')
    assert lines[1].startswith('A1,mismatched,1,1,,,A1,,') and lines[1].endswith(',amount,5,6')
    ok('expect', {'key': 'l3', 'value': 'B', 'note': 'x'})
    head = call_raw('list', {'key': 'l3', 'format': 'csv'})['body'].split('\n')[0]
    assert head.endswith(',differences,expected.note')       # no empty confirmed.* columns


# ------------------------------------------------------------------ cyclepop
def test_cyclepop_rotates_short_groups_only():
    post('expect', 'cp', ['a', 'b', 'c', 'c'])
    ok('confirm', {'key': 'cp', 'value': 'b'})
    got = [ok('cyclepop', {'key': 'cp'})['value'] for _ in range(4)]
    assert set(got[:2]) == {'a', 'c'} and got[2] == got[0] and got[3] == got[1]
    b = ok('cyclepop', {'key': 'cp'})
    assert b['group']['checks'] == 3 and b['exhausted'] is False
    post('confirm', 'cp', ['a', 'c'])
    assert ok('cyclepop', {'key': 'cp'})['value'] == 'c'              # c is 2 vs 1: still short
    ok('confirm', {'key': 'cp', 'value': 'c'})
    assert ok('cyclepop', {'key': 'cp'})['exhausted'] is True
    s = summary('cp')
    assert s['matched'] == 4 and s['outstanding'] == 0


# ------------------------------------------------------------------ perfkey
def test_perfkey_records_lag():
    post('expect', 'pk', ['a', 'b', 'c'])
    post('confirm', 'pk', ['a', 'b'], perfkey='pk.lag')
    ok('confirm', {'key': 'pk', 'value': 'c', 'perfkey': 'pk.lag'})
    ok('confirm', {'key': 'pk', 'value': 'stray', 'perfkey': 'pk.lag'})   # pairs nothing: no lag
    s = perf.handler({'httpMethod': 'GET', 'resource': '/perf/{proxy+}', 'path': '/v1/perf/stats',
                      'pathParameters': {'proxy': 'stats'}, 'queryStringParameters': {'key': 'pk.lag'},
                      'headers': {}, 'body': None, 'isBase64Encoded': False}, None)
    assert json.loads(s['body'])['count'] == 3
    assert summary('pk')['latencyMs']['count'] == 3


def test_perfkey_unavailable(monkeypatch):
    monkeypatch.delenv('PERF_TABLE')
    s, b = call('confirm', {'key': 'pk2', 'value': 'v', 'perfkey': 'x'})
    assert s == 400 and 'perfkey' in b['error']


# ------------------------------------------------------------------ clear, index
def test_clear():
    post('expect', 'cl', ['a', 'b', 'c'], ignorecase='true', suffix=1)
    b = ok('clear', {'key': 'cl'})
    assert b['deleted'] >= 3 and b['cleared'] is True and b['more'] is False
    s = summary('cl')
    assert s['expected'] == 0 and 'ignorecase' not in s
    b = ok('expect', {'key': 'cl', 'value': 'A', 'ignorecase': 'false'})
    assert b['ignorecase'] is False and b['suffix'] == 0
    b = ok('clear', {'key': 'never'})
    assert b['cleared'] is False and b['comment'] == 'nothing to clear'


def test_clear_in_pieces(monkeypatch):
    monkeypatch.setattr(recon, 'MAX_CLEAR', 7)
    post('expect', 'cl2', [str(i) for i in range(20)])
    total, rounds = 0, 0
    while True:
        b = ok('clear', {'key': 'cl2'})
        total += b['deleted']
        rounds += 1
        if not b['more']:
            break
    assert total >= 20 and rounds >= 3 and summary('cl2')['expected'] == 0


def test_index_and_unknown_action():
    b = ok('')
    assert b['service'] == 'recon' and 'expect' in b['actions'] and 'count' in b['description']
    s, b = call('peek', {'key': 'x'})
    assert s == 404 and 'cyclepop' in b['actions']
