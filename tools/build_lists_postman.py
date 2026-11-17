"""Builds postman/lists.testdata-help.postman_collection.json - a self-checking
Postman collection that exercises every /v1/lists action.

    python tools/build_lists_postman.py

Run the whole collection (Collection Runner or newman): later requests use
variables saved by earlier ones, and the loops use setNextRequest, which only
works in a run. Every run makes its own random keys, so it can be re-run any
number of times against the same deployment.
"""
import json
import os
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'postman', 'lists.testdata-help.postman_collection.json')

NS = uuid.UUID('6f1d2a52-1c55-4c43-9a51-0b7c2d0a7e11')


def _id(*parts):
    return str(uuid.uuid5(NS, '/'.join(parts)))


# ------------------------------------------------------------------ JS snippets
NEXT = """function next(name) {
  if (pm.execution && pm.execution.setNextRequest) pm.execution.setNextRequest(name);
  else postman.setNextRequest(name);
}
"""

REF_RE = r"/^[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$/"


def status(code, label=None):
    return "pm.test('%s', () => pm.response.to.have.status(%d));\n" % (label or 'HTTP %d' % code, code)


def json_ok(code=200):
    return status(code) + "const b = pm.response.json();\n"


def error(code, contains=None):
    s = status(code) + "const b = pm.response.json();\n"
    if contains:
        s += ("pm.test('error mentions %s', () => pm.expect(b.error.toLowerCase()).to.include(%s));\n"
              % (contains.replace("'", ''), json.dumps(contains.lower())))
    return s


# ------------------------------------------------------------------ builders
def url(path, params=(), base='{{baseUrl}}'):
    """params: list of (name, value) or (name, value, description)."""
    q = []
    for p in params:
        e = {'key': p[0], 'value': p[1]}
        if len(p) > 2:
            e['description'] = p[2]
        q.append(e)
    raw = base + '/lists' + (('/' + path) if path else '')
    if q:
        raw += '?' + '&'.join('%s=%s' % (e['key'], e['value']) for e in q)
    u = {'raw': raw, 'host': [base], 'path': ['lists'] + ([x for x in path.split('/')] if path else [])}
    if q:
        u['query'] = q
    return u


def request(name, path, params=(), tests='', pre='', method='GET', body=None, ctype=None,
            desc='', raw_url=None):
    r = {'method': method, 'header': [], 'url': raw_url or url(path, params)}
    if desc:
        r['description'] = desc
    if body is not None:
        r['body'] = {'mode': 'raw', 'raw': body}
    if ctype:
        r['header'].append({'key': 'Content-Type', 'value': ctype})
    ev = []
    if pre:
        ev.append({'listen': 'prerequest', 'script': {'type': 'text/javascript', 'exec': pre.strip('\n').split('\n')}})
    if tests:
        ev.append({'listen': 'test', 'script': {'type': 'text/javascript', 'exec': tests.strip('\n').split('\n')}})
    item = {'name': name, 'request': r, 'response': []}
    if ev:
        item['event'] = ev
    return item


def folder(name, desc, items):
    return {'name': name, 'description': desc, 'item': items}


# ================================================================== 00 setup
setup = folder('00 Setup', """Makes this run's keys from `lists/codes` (`rand10`), so every run is isolated
and the collection can be re-run against the same deployment. Also checks the service index.""", [
    request('Codes - random keys for this run', 'codes', tests=json_ok() + r"""
pm.test('rand3..rand10 are upper-case A-Z0-9 of the right length', () => {
  for (const [f, n] of [['rand3', 3], ['rand4', 4], ['rand6', 6], ['rand8', 8], ['rand10', 10]]) {
    pm.expect(b[f], f).to.match(new RegExp('^[A-Z0-9]{' + n + '}$'));
  }
});
const run = 'pm-' + b.rand10;
const keys = { runKey: run, qKey: run + '-queue', emptyKey: run + '-empty', varsKey: run + '-vars',
  ctrKey: run + '-counters', cleanKey: run + '-cleanup', impKey: run + '-csv', jsonKey: run + '-json',
  asyncKey: run + '-async', upKey: run + '-upload', e2eKey: run + '-e2e', unusedKey: run + '-never-used',
  cycKey: run + '-cycle' };
for (const [k, v] of Object.entries(keys)) pm.collectionVariables.set(k, v);
pm.collectionVariables.set('loopCount', '0');
console.log('Keys for this run start with ' + run);
""", desc="""Five random codes from a uniform A-Z0-9 alphabet. `rand10` (36^10 ~ 3.7e15 values) is
the recommended per-run key. The test script derives every key this run uses from it."""),
    request('Codes - every call is different', 'codes', tests=json_ok() + r"""
pm.test('a fresh rand10', () => pm.expect('pm-' + b.rand10).to.not.eql(pm.collectionVariables.get('runKey')));
"""),
    request('Service index - GET /lists', '', tests=json_ok() + r"""
const expected = ['cleanup/list', 'cleanup/pop', 'cleanup/push', 'clear', 'codes', 'counters/add',
  'counters/get', 'cyclepop', 'import', 'import/status', 'list', 'pop', 'push', 'random', 'reset', 'upload',
  'upload/url', 'vars/get', 'vars/list', 'vars/set'];
pm.test('service is lists', () => pm.expect(b.service).to.eql('lists'));
pm.test('all 20 actions are listed', () => pm.expect(b.actions).to.eql(expected));
""", desc='The bare service URL returns its action list - a quick liveness check.'),
])

# ================================================================== 01 queue on an empty key
empty = folder('01 Queue - empty key behaviour', """Nothing here is an error: an empty or unknown key answers 200 with a flag
(`exhausted`, `empty`, `reset:false`), so load-test tools don't count it as a failed request.""", [
    request('pop - empty key is exhausted (200)', 'pop', [('key', '{{emptyKey}}')], tests=json_ok() + r"""
pm.test('exhausted, null record', () => {
  pm.expect(b.exhausted).to.eql(true); pm.expect(b.record).to.eql(null); pm.expect(b.index).to.eql(null);
});
pm.test('key echoed', () => pm.expect(b.key).to.eql(pm.collectionVariables.get('emptyKey')));
"""),
    request('random - empty key', 'random', [('key', '{{emptyKey}}')], tests=json_ok() + r"""
pm.test('empty, count 0, null record', () => {
  pm.expect(b.empty).to.eql(true); pm.expect(b.count).to.eql(0); pm.expect(b.record).to.eql(null);
});
"""),
    request('list - empty key', 'list', [('key', '{{emptyKey}}')], tests=json_ok() + r"""
pm.test('no records', () => {
  pm.expect(b.count).to.eql(0); pm.expect(b.returned).to.eql(0); pm.expect(b.records).to.eql([]);
  pm.expect(b.nextPop).to.eql(null); pm.expect(b.nextStart).to.eql(null); pm.expect(b.truncated).to.eql(false);
});
"""),
    request('reset - empty key', 'reset', [('key', '{{emptyKey}}')], tests=json_ok() + r"""
pm.test('reset:false, nothing to reset', () => { pm.expect(b.reset).to.eql(false); pm.expect(b.count).to.eql(0); });
"""),
    request('clear - empty key', 'clear', [('key', '{{emptyKey}}')], tests=json_ok() + r"""
pm.test('cleared:false, deleted 0', () => { pm.expect(b.cleared).to.eql(false); pm.expect(b.deleted).to.eql(0); });
"""),
])

# ================================================================== 02 queue lifecycle
PUSH_COMMON = r"""
const now = Math.floor(Date.now() / 1000);
pm.test('record carries key, index and createdTime', () => {
  pm.expect(b.record.key).to.eql(pm.collectionVariables.get('qKey'));
  pm.expect(b.record.index).to.eql(b.index);
  pm.expect(b.createdTime).to.be.within(now - 600, now + 600);
  pm.expect(b.record.createdTime).to.eql(b.createdTime);
});
pm.test('count equals the new index', () => pm.expect(b.count).to.eql(b.index));
"""

queue = folder('02 Queue - push, list, random, pop, reset, clear', """The full life of a queue. `push` turns every query parameter except `key` into a
string field (names keep their case); `pop` hands each record out exactly once across all callers;
`random` never consumes; `reset` rewinds; `clear` deletes the records.""", [
    request('push - record 1 (case of field names kept)', 'push',
            [('key', '{{qKey}}'), ('email', 'alice@example.com'), ('FirstName', 'Alice'), ('plan', 'gold')],
            tests=json_ok() + r"""
pm.test('index 1', () => pm.expect(b.index).to.eql(1));
pm.test('fields stored as sent, FirstName keeps its case', () => {
  pm.expect(b.record.email).to.eql('alice@example.com');
  pm.expect(b.record.FirstName).to.eql('Alice');
  pm.expect(b.record.plan).to.eql('gold');
});
""" + PUSH_COMMON),
    request('push - record 2', 'push',
            [('key', '{{qKey}}'), ('email', 'bob@example.com'), ('FirstName', 'Bob'), ('plan', 'silver')],
            tests=json_ok() + "pm.test('index 2', () => pm.expect(b.index).to.eql(2));\n" + PUSH_COMMON),
    request('push - special characters (URL-encoded + & % #)', 'push',
            [('key', '{{qKey}}'), ('email', 'carol@example.com'),
             ('note', 'A%2BB%20%26%20C%20100%25%20%231', 'The value A+B & C 100% #1, URL-encoded. A raw + would arrive as a space.')],
            tests=json_ok() + r"""
pm.test('value comes back decoded and exact', () => pm.expect(b.record.note).to.eql('A+B & C 100% #1'));
pm.test('index 3', () => pm.expect(b.index).to.eql(3));
""" + PUSH_COMMON),
    request('push - empty value and leading zeros stay strings', 'push',
            [('key', '{{qKey}}'), ('email', 'dave@example.com'), ('middle', ''), ('pin', '007')],
            tests=json_ok() + r"""
pm.test('empty value stored as empty string', () => pm.expect(b.record.middle).to.eql(''));
pm.test('pin keeps its leading zeros (pushed values are strings)', () => pm.expect(b.record.pin).to.eql('007'));
pm.test('index 4', () => pm.expect(b.index).to.eql(4));
""" + PUSH_COMMON),
    request('push - record 5', 'push', [('key', '{{qKey}}'), ('email', 'eve@example.com'), ('plan', 'gold')],
            tests=json_ok() + "pm.test('index 5', () => pm.expect(b.index).to.eql(5));\n" + PUSH_COMMON),
    request('list - all records', 'list', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('5 records, all returned, pop pointer at 1', () => {
  pm.expect(b.count).to.eql(5); pm.expect(b.returned).to.eql(5); pm.expect(b.nextPop).to.eql(1);
  pm.expect(b.truncated).to.eql(false); pm.expect(b.nextStart).to.eql(null); pm.expect(b.importRef).to.eql(null);
});
pm.test('records in index order', () => pm.expect(b.records.map(r => r.index)).to.eql([1, 2, 3, 4, 5]));
pm.test('record 2 is bob', () => pm.expect(b.records[1].email).to.eql('bob@example.com'));
"""),
    request('list - page 1 (start=2, limit=2)', 'list', [('key', '{{qKey}}'), ('start', '2'), ('limit', '2')],
            tests=json_ok() + r"""
pm.test('records 2 and 3', () => pm.expect(b.records.map(r => r.index)).to.eql([2, 3]));
pm.test('nextStart says where to resume', () => pm.expect(b.nextStart).to.eql(4));
pm.collectionVariables.set('nextStart', String(b.nextStart));
"""),
    request('list - next page (start={{nextStart}})', 'list', [('key', '{{qKey}}'), ('start', '{{nextStart}}'), ('limit', '2')],
            tests=json_ok() + r"""
pm.test('records 4 and 5', () => pm.expect(b.records.map(r => r.index)).to.eql([4, 5]));
pm.test('last page: nextStart null', () => pm.expect(b.nextStart).to.eql(null));
"""),
    request('list - start beyond the end', 'list', [('key', '{{qKey}}'), ('start', '99')], tests=json_ok() + r"""
pm.test('nothing returned, count still 5', () => {
  pm.expect(b.returned).to.eql(0); pm.expect(b.count).to.eql(5); pm.expect(b.nextStart).to.eql(null);
});
"""),
    request('random - any record, not consumed', 'random', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('not empty, count 5', () => { pm.expect(b.empty).to.eql(false); pm.expect(b.count).to.eql(5); });
pm.test('index within 1..5 and matches the record', () => {
  pm.expect(b.index).to.be.within(1, 5); pm.expect(b.record.index).to.eql(b.index);
});
pm.test('a real record', () => pm.expect(b.record.email).to.match(/@example\.com$/));
"""),
    request('list - random did not move the pop pointer', 'list', [('key', '{{qKey}}'), ('limit', '1')],
            tests=json_ok() + "pm.test('nextPop still 1', () => pm.expect(b.nextPop).to.eql(1));\n"),
    request('pop - first record', 'pop', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('not exhausted', () => pm.expect(b.exhausted).to.eql(false));
pm.test('index 1 of 5, 4 remaining', () => {
  pm.expect(b.index).to.eql(1); pm.expect(b.count).to.eql(5); pm.expect(b.remaining).to.eql(4);
});
pm.test('alice', () => pm.expect(b.record.email).to.eql('alice@example.com'));
"""),
    request('pop - second record', 'pop', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('index 2, bob, 3 remaining', () => {
  pm.expect(b.index).to.eql(2); pm.expect(b.record.email).to.eql('bob@example.com'); pm.expect(b.remaining).to.eql(3);
});
pm.collectionVariables.set('lastIndex', '2');
"""),
    request('pop - loop until exhausted', 'pop', [('key', '{{qKey}}')],
            desc="""Calls itself (setNextRequest) until `exhausted` is true - the pattern a script uses to consume a
queue. Each pass checks the index goes up by exactly one. Capped at 20 passes.""",
            tests=NEXT + json_ok() + r"""
const n = Number(pm.collectionVariables.get('loopCount')) + 1;
const last = Number(pm.collectionVariables.get('lastIndex'));
if (!b.exhausted) {
  pm.test('pass ' + n + ': index ' + (last + 1), () => pm.expect(b.index).to.eql(last + 1));
  pm.test('pass ' + n + ': remaining = count - index', () => pm.expect(b.remaining).to.eql(b.count - b.index));
  pm.collectionVariables.set('lastIndex', String(b.index));
}
if (!b.exhausted && n < 20) {
  pm.collectionVariables.set('loopCount', String(n));
  next(pm.info.requestName);
} else {
  pm.collectionVariables.set('loopCount', '0');
  pm.test('loop ended because the queue is exhausted', () => pm.expect(b.exhausted).to.eql(true));
  pm.test('every record was handed out once (last index 5)', () => pm.expect(last).to.eql(5));
}
"""),
    request('pop - still exhausted (200, not an error)', 'pop', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('exhausted, null record, a hint to reset', () => {
  pm.expect(b.exhausted).to.eql(true); pm.expect(b.record).to.eql(null); pm.expect(b.message).to.include('reset');
});
"""),
    request('list - pop pointer past the end', 'list', [('key', '{{qKey}}'), ('limit', '1')],
            tests=json_ok() + "pm.test('nextPop 6', () => pm.expect(b.nextPop).to.eql(6));\n"),
    request('reset - rewind to record 1', 'reset', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('reset, count 5, nextPop 1', () => {
  pm.expect(b.reset).to.eql(true); pm.expect(b.count).to.eql(5); pm.expect(b.nextPop).to.eql(1);
});
"""),
    request('pop - after reset starts again at 1', 'pop', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('index 1, alice again', () => { pm.expect(b.index).to.eql(1); pm.expect(b.record.email).to.eql('alice@example.com'); });
"""),
    request('push - append while part-consumed', 'push', [('key', '{{qKey}}'), ('email', 'frank@example.com')],
            tests=json_ok() + "pm.test('index 6', () => pm.expect(b.index).to.eql(6));\n"),
    request('pop - pointer unaffected by the push', 'pop', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('index 2 of 6', () => { pm.expect(b.index).to.eql(2); pm.expect(b.count).to.eql(6); pm.expect(b.remaining).to.eql(4); });
"""),
    request('clear - delete every record', 'clear', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('cleared 6 records now', () => {
  pm.expect(b.cleared).to.eql(true); pm.expect(b.deleted).to.eql(6); pm.expect(b.backgroundDelete).to.eql(0);
});
"""),
    request('list - empty after clear', 'list', [('key', '{{qKey}}')], tests=json_ok() + r"""
pm.test('count 0', () => { pm.expect(b.count).to.eql(0); pm.expect(b.records).to.eql([]); });
"""),
    request('pop - exhausted after clear', 'pop', [('key', '{{qKey}}')],
            tests=json_ok() + "pm.test('exhausted', () => pm.expect(b.exhausted).to.eql(true));\n"),
    request('push - a cleared key starts again at index 1', 'push', [('key', '{{qKey}}'), ('email', 'grace@example.com')],
            tests=json_ok() + "pm.test('index 1', () => pm.expect(b.index).to.eql(1));\n"),
])

# ================================================================== 02b cyclepop
CYC_STEP = NEXT + json_ok() + r"""
// Expected sequence over 3 records: (index, pass) = (1,1) (2,1) (3,1) (1,2) (2,2) (3,2) (1,3)
const n = Number(pm.collectionVariables.get('loopCount'));
const want = [[1, 1], [2, 1], [3, 1], [1, 2], [2, 2], [3, 2], [1, 3]][n];
pm.test('call ' + (n + 1) + ': record ' + want[0] + ', pass ' + want[1], () => {
  pm.expect(b.exhausted).to.eql(false);
  pm.expect([b.index, b.pass]).to.eql(want);
  pm.expect(b.record.n).to.eql(['a', 'b', 'c'][want[0] - 1]);
});
pm.test('call ' + (n + 1) + ': wrapped only on the first record of a later pass', () => pm.expect(b.wrapped).to.eql(want[0] === 1 && want[1] > 1));
pm.test('call ' + (n + 1) + ': remaining in this pass', () => pm.expect(b.remaining).to.eql(3 - want[0]));
if (n < 6) { pm.collectionVariables.set('loopCount', String(n + 1)); next(pm.info.requestName); }
else pm.collectionVariables.set('loopCount', '0');
"""

cyclepop = folder('02b Queue - cyclepop (pop that starts again)', """`cyclepop` hands records out in order like `pop`, but after the last one it starts
again at record 1 instead of reporting `exhausted`. `pass` counts the times round. It shares pop's
pointer, so `reset` restarts it and `list` shows it as `nextPop`. Use it for reusable data that must
not be in use by two scripts at once; use `pop` for data that may only be used once.""", [
    request('cyclepop - empty key reports exhausted + empty', 'cyclepop', [('key', '{{cycKey}}')], tests=json_ok() + r"""
pm.test('exhausted and empty, null record', () => {
  pm.expect(b.exhausted).to.eql(true); pm.expect(b.empty).to.eql(true); pm.expect(b.record).to.eql(null);
});
"""),
    request('push a', 'push', [('key', '{{cycKey}}'), ('n', 'a')], tests=json_ok()),
    request('push b', 'push', [('key', '{{cycKey}}'), ('n', 'b')], tests=json_ok()),
    request('push c', 'push', [('key', '{{cycKey}}'), ('n', 'c')], tests=json_ok() + "pm.test('3 records', () => pm.expect(b.count).to.eql(3));\n"),
    request('cyclepop - seven calls go round more than twice (loop)', 'cyclepop', [('key', '{{cycKey}}')],
            desc='Calls itself 7 times over 3 records: a, b, c, a (pass 2), b, c, a (pass 3).', tests=CYC_STEP),
    request('list - the shared pointer keeps counting', 'list', [('key', '{{cycKey}}'), ('limit', '1')], tests=json_ok() + r"""
pm.test('nextPop 8 after 7 cyclepops', () => pm.expect(b.nextPop).to.eql(8));
"""),
    request('pop - plain pop sees the key as used up', 'pop', [('key', '{{cycKey}}')], tests=json_ok() + r"""
pm.test('exhausted (shared pointer is past the end)', () => pm.expect(b.exhausted).to.eql(true));
"""),
    request('reset - back to record 1', 'reset', [('key', '{{cycKey}}')], tests=json_ok()),
    request('cyclepop - after reset: record 1, pass 1', 'cyclepop', [('key', '{{cycKey}}')], tests=json_ok() + r"""
pm.test('record 1, pass 1, not wrapped', () => {
  pm.expect(b.index).to.eql(1); pm.expect(b.pass).to.eql(1); pm.expect(b.wrapped).to.eql(false);
});
"""),
    request('push d - the cycle grows from the next call', 'push', [('key', '{{cycKey}}'), ('n', 'd')], tests=json_ok()),
    request('cyclepop - b', 'cyclepop', [('key', '{{cycKey}}')], tests=json_ok() + "pm.test('b', () => pm.expect(b.record.n).to.eql('b'));\n"),
    request('cyclepop - c', 'cyclepop', [('key', '{{cycKey}}')], tests=json_ok() + "pm.test('c', () => pm.expect(b.record.n).to.eql('c'));\n"),
    request('cyclepop - d (the new record is in this pass)', 'cyclepop', [('key', '{{cycKey}}')], tests=json_ok() + r"""
pm.test('d, record 4 of 4', () => { pm.expect(b.record.n).to.eql('d'); pm.expect(b.count).to.eql(4); pm.expect(b.remaining).to.eql(0); });
"""),
    request('cyclepop - then round to a, pass 2', 'cyclepop', [('key', '{{cycKey}}')], tests=json_ok() + r"""
pm.test('a, pass 2, wrapped', () => { pm.expect(b.record.n).to.eql('a'); pm.expect(b.pass).to.eql(2); pm.expect(b.wrapped).to.eql(true); });
"""),
])

# ================================================================== 03 validation
LONG129 = r"""pm.collectionVariables.set('longKey', 'k'.repeat(129));
pm.collectionVariables.set('maxKey', 'k'.repeat(127) + pm.collectionVariables.get('runKey').slice(-1));
"""
validation = folder('03 Conventions and validation', """Rules shared by every lists action: the `key` format, case-insensitive
parameter names and actions, the `{error, status}` error shape, 404 with the valid actions for an
unknown action, and CORS pre-flight.""", [
    request('missing key - 400', 'pop', tests=error(400, 'key')),
    request('key with # - 400', 'push', [('key', 'bad%23key'), ('a', '1')], tests=error(400, 'key') + r"""
pm.test('received value echoed', () => pm.expect(b.received).to.eql('bad#key'));
"""),
    request('key starting with . - 400', 'list', [('key', '.hidden')], tests=error(400, 'key')),
    request('key of 129 characters - 400', 'list', [('key', '{{longKey}}')], pre=LONG129, tests=error(400, 'key')),
    request('key of 128 characters - allowed', 'list', [('key', '{{maxKey}}')], pre=LONG129, tests=json_ok() + r"""
pm.test('128-character key accepted', () => pm.expect(b.key.length).to.eql(128));
"""),
    request('key with all allowed punctuation . _ - : @ + =', 'list', [('key', '{{runKey}}.a_b-c:d@e%2Bf=g')],
            tests=json_ok() + r"""
pm.test('key echoed exactly', () => pm.expect(b.key).to.eql(pm.collectionVariables.get('runKey') + '.a_b-c:d@e+f=g'));
"""),
    request('push - reserved field "index" - 400', 'push', [('key', '{{qKey}}'), ('index', '9')],
            tests=error(400, 'cannot be a record field') + r"""
pm.test('lists the reserved names', () => pm.expect(b.reserved).to.eql(['key', 'index', 'createdTime']));
"""),
    request('push - reserved field "createdTime" (any case) - 400', 'push', [('key', '{{qKey}}'), ('CREATEDTIME', '1')],
            tests=error(400, 'cannot be a record field')),
    request('parameter names are case-insensitive (KEY=)', 'list', [('KEY', '{{qKey}}'), ('LIMIT', '1')],
            tests=json_ok() + r"""
pm.test('KEY treated as key', () => pm.expect(b.key).to.eql(pm.collectionVariables.get('qKey')));
pm.test('LIMIT treated as limit', () => pm.expect(b.returned).to.be.at.most(1));
"""),
    request('actions are case-insensitive (/lists/LIST)', '', raw_url={
        'raw': '{{baseUrl}}/lists/LIST?key={{qKey}}', 'host': ['{{baseUrl}}'], 'path': ['lists', 'LIST'],
        'query': [{'key': 'key', 'value': '{{qKey}}'}]}, tests=json_ok() + r"""
pm.test('handled as list', () => pm.expect(b).to.have.property('records'));
"""),
    request('unknown action - 404 with the valid actions', 'peek', [('key', '{{qKey}}')], tests=error(404, 'unknown action') + r"""
pm.test('actions listed', () => pm.expect(b.actions).to.include.members(['pop', 'push', 'vars/get', 'import']));
"""),
    request('list - start must be a number - 400', 'list', [('key', '{{qKey}}'), ('start', 'abc')],
            tests=error(400, 'whole number')),
    request('CORS pre-flight - OPTIONS 204', 'pop', [('key', '{{qKey}}')], method='OPTIONS', tests=r"""
pm.test('HTTP 204', () => pm.response.to.have.status(204));
pm.test('Access-Control-Allow-Origin *', () => pm.expect(pm.response.headers.get('Access-Control-Allow-Origin')).to.eql('*'));
"""),
])

# ================================================================== 04 vars
vars_ = folder('04 Variables - vars/set, vars/get, vars/list', """Named string values under a key: hand a value from a setup script to a test
script, or from one virtual user to another. `vars/get` of a name that was never set is a 404.
With `equals=` and/or `wait=` it is always 200 (`exists`, `matched`, `waitedMs`), and `wait=N` holds the
request up to N seconds until the value equals `equals` or changes - the test-cycle setup gate
(llms.txt recipe 4.12). `lists/clear` does not touch variables.""", [
    request('vars/get - never set - 404', 'vars/get', [('key', '{{varsKey}}'), ('name', 'orderId')],
            tests=error(404, 'not found') + r"""
pm.test('key and name echoed', () => { pm.expect(b.name).to.eql('orderId'); pm.expect(b.key).to.eql(pm.collectionVariables.get('varsKey')); });
"""),
    request('vars/list - none yet', 'vars/list', [('key', '{{varsKey}}')], tests=json_ok() + r"""
pm.test('count 0', () => { pm.expect(b.count).to.eql(0); pm.expect(b.variables).to.eql([]); });
"""),
    request('vars/set - orderId', 'vars/set', [('key', '{{varsKey}}'), ('name', 'orderId'), ('value', 'SO-1001')],
            tests=json_ok() + r"""
pm.test('value and savedTime returned', () => {
  pm.expect(b.value).to.eql('SO-1001'); pm.expect(b.name).to.eql('orderId');
  pm.expect(b.savedTime).to.be.within(Date.now() / 1000 - 600, Date.now() / 1000 + 600);
});
"""),
    request('vars/get - orderId', 'vars/get', [('key', '{{varsKey}}'), ('name', 'orderId')], tests=json_ok() + r"""
pm.test('SO-1001', () => pm.expect(b.value).to.eql('SO-1001'));
pm.test('savedTime is a number', () => pm.expect(b.savedTime).to.be.a('number'));
"""),
    request('vars/set - overwrite orderId', 'vars/set', [('key', '{{varsKey}}'), ('name', 'orderId'), ('value', 'SO-1002')],
            tests=json_ok() + "pm.test('SO-1002', () => pm.expect(b.value).to.eql('SO-1002'));\n"),
    request('vars/get - sees the new value', 'vars/get', [('key', '{{varsKey}}'), ('name', 'orderId')],
            tests=json_ok() + "pm.test('SO-1002', () => pm.expect(b.value).to.eql('SO-1002'));\n"),
    request('vars/set - a URL with & = ? (URL-encoded)', 'vars/set',
            [('key', '{{varsKey}}'), ('name', 'baseUrl'), ('value', 'https%3A%2F%2Fshop.example.com%2Fa%3Fb%3D1%26c%3D2')],
            tests=json_ok() + r"""
pm.test('stored exactly', () => pm.expect(b.value).to.eql('https://shop.example.com/a?b=1&c=2'));
"""),
    request('vars/set - empty value is allowed', 'vars/set', [('key', '{{varsKey}}'), ('name', 'blank'), ('value', '')],
            tests=json_ok() + "pm.test('empty string', () => pm.expect(b.value).to.eql(''));\n"),
    request('vars/get - empty value', 'vars/get', [('key', '{{varsKey}}'), ('name', 'blank')],
            tests=json_ok() + "pm.test('empty string, not 404', () => pm.expect(b.value).to.eql(''));\n"),
    request('vars/list - all three', 'vars/list', [('key', '{{varsKey}}')], tests=json_ok() + r"""
pm.test('count 3', () => pm.expect(b.count).to.eql(3));
const m = Object.fromEntries(b.variables.map(v => [v.name, v.value]));
pm.test('names and values', () => pm.expect(m).to.eql({ baseUrl: 'https://shop.example.com/a?b=1&c=2', blank: '', orderId: 'SO-1002' }));
pm.test('each has savedTime', () => b.variables.forEach(v => pm.expect(v.savedTime).to.be.a('number')));
"""),
    request('vars/get - names are case-sensitive (OrderId) - 404', 'vars/get', [('key', '{{varsKey}}'), ('name', 'OrderId')],
            tests=error(404)),
    request('vars/set - missing value - 400', 'vars/set', [('key', '{{varsKey}}'), ('name', 'x')], tests=error(400, 'value')),
    request('vars/set - missing name - 400', 'vars/set', [('key', '{{varsKey}}'), ('value', 'x')], tests=error(400, 'name')),
    request('vars/set - name over 256 characters - 400', 'vars/set', [('key', '{{varsKey}}'), ('name', '{{longName}}'), ('value', 'x')],
            pre="pm.collectionVariables.set('longName', 'n'.repeat(257));", tests=error(400, '256')),
    request('gate: equals=1 on a flag never set - 200, exists false', 'vars/get',
            [('key', '{{varsKey}}'), ('name', 'setupReady'), ('equals', '1')], tests=json_ok() + r"""
pm.test('not a 404', () => { pm.expect(b.exists).to.eql(false); pm.expect(b.matched).to.eql(false); pm.expect(b.value).to.eql(null); });
pm.test('equals echoed', () => pm.expect(b.equals).to.eql('1'));
"""),
    request('gate: wait=2 without equals, never set - waits, then exists false', 'vars/get',
            [('key', '{{varsKey}}'), ('name', 'setupReady'), ('wait', '2')], tests=json_ok() + r"""
pm.test('exists false', () => pm.expect(b.exists).to.eql(false));
pm.test('held about 2 s', () => pm.expect(b.waitedMs).to.be.within(1500, 6000));
"""),
    request('gate: initiation closes the gate - setupReady=0', 'vars/set',
            [('key', '{{varsKey}}'), ('name', 'setupReady'), ('value', '0')], tests=json_ok()),
    request('gate: equals=1&wait=2 while 0 - not matched after the wait', 'vars/get',
            [('key', '{{varsKey}}'), ('name', 'setupReady'), ('equals', '1'), ('wait', '2')], tests=json_ok() + r"""
pm.test('still 0', () => { pm.expect(b.matched).to.eql(false); pm.expect(b.value).to.eql('0'); pm.expect(b.exists).to.eql(true); });
pm.test('held about 2 s', () => pm.expect(b.waitedMs).to.be.within(1500, 6000));
"""),
    request('gate: initiation opens the gate - setupReady=1', 'vars/set',
            [('key', '{{varsKey}}'), ('name', 'setupReady'), ('value', '1')], tests=json_ok()),
    request('gate: equals=1&wait=20 when already 1 - returns at once', 'vars/get',
            [('key', '{{varsKey}}'), ('name', 'setupReady'), ('equals', '1'), ('wait', '20')], tests=json_ok() + r"""
pm.test('matched', () => { pm.expect(b.matched).to.eql(true); pm.expect(b.value).to.eql('1'); });
pm.test('no wait', () => pm.expect(b.waitedMs).to.be.below(2000));
"""),
    request('gate: equals is exact - equals=01 does not match 1', 'vars/get',
            [('key', '{{varsKey}}'), ('name', 'setupReady'), ('equals', '01')], tests=json_ok() + r"""
pm.test('not matched', () => pm.expect(b.matched).to.eql(false));
"""),
    request('clear the key (records only)', 'clear', [('key', '{{varsKey}}')], tests=json_ok()),
    request('vars/get - variables survive lists/clear', 'vars/get', [('key', '{{varsKey}}'), ('name', 'orderId')],
            tests=json_ok() + "pm.test('still SO-1002', () => pm.expect(b.value).to.eql('SO-1002'));\n"),
])

# ================================================================== 05 counters
counters = folder('05 Counters - counters/add, counters/get', """Atomic counters (DynamoDB ADD): concurrent callers never lose an increment.
`name` defaults to `default`; `increment` defaults to 1 and may be negative or zero. An unused counter
reads as 0 with `exists:false` (not a 404). About 1,000 updates/s per counter - for unique values at
high volume use `lists/codes`.""", [
    request('counters/get - unused counter reads 0', 'counters/get', [('key', '{{ctrKey}}')], tests=json_ok() + r"""
pm.test('value 0, exists false, name default', () => {
  pm.expect(b.value).to.eql(0); pm.expect(b.exists).to.eql(false); pm.expect(b.name).to.eql('default');
  pm.expect(b.updatedTime).to.eql(null);
});
"""),
    request('counters/add - default +1', 'counters/add', [('key', '{{ctrKey}}')], tests=json_ok() + r"""
pm.test('value 1, increment 1', () => { pm.expect(b.value).to.eql(1); pm.expect(b.increment).to.eql(1); });
pm.test('updatedTime set', () => pm.expect(b.updatedTime).to.be.a('number'));
"""),
    request('counters/add - increment=5', 'counters/add', [('key', '{{ctrKey}}'), ('increment', '5')],
            tests=json_ok() + "pm.test('value 6', () => pm.expect(b.value).to.eql(6));\n"),
    request('counters/add - negative increment', 'counters/add', [('key', '{{ctrKey}}'), ('increment', '-2')],
            tests=json_ok() + "pm.test('value 4', () => pm.expect(b.value).to.eql(4));\n"),
    request('counters/add - increment=0 returns the current value', 'counters/add', [('key', '{{ctrKey}}'), ('increment', '0')],
            tests=json_ok() + "pm.test('value 4', () => pm.expect(b.value).to.eql(4));\n"),
    request('counters/add - explicit plus sign (%2B3)', 'counters/add', [('key', '{{ctrKey}}'), ('increment', '%2B3')],
            tests=json_ok() + "pm.test('value 7', () => pm.expect(b.value).to.eql(7));\n"),
    request('counters/get - default counter', 'counters/get', [('key', '{{ctrKey}}')], tests=json_ok() + r"""
pm.test('value 7, exists', () => { pm.expect(b.value).to.eql(7); pm.expect(b.exists).to.eql(true); });
"""),
    request('counters/add - a named counter is independent', 'counters/add', [('key', '{{ctrKey}}'), ('name', 'orders')],
            tests=json_ok() + r"""
pm.test('orders = 1', () => { pm.expect(b.name).to.eql('orders'); pm.expect(b.value).to.eql(1); });
pm.collectionVariables.set('lastCounter', String(b.value));
"""),
    request('counters/add - sequence numbers (loop of 3)', 'counters/add', [('key', '{{ctrKey}}'), ('name', 'orders')],
            desc='Each pass must return exactly the previous value + 1 - the order-number pattern.',
            tests=NEXT + json_ok() + r"""
const n = Number(pm.collectionVariables.get('loopCount')) + 1;
const prev = Number(pm.collectionVariables.get('lastCounter'));
pm.test('pass ' + n + ': ' + (prev + 1), () => pm.expect(b.value).to.eql(prev + 1));
pm.collectionVariables.set('lastCounter', String(b.value));
if (n < 3) { pm.collectionVariables.set('loopCount', String(n)); next(pm.info.requestName); }
else pm.collectionVariables.set('loopCount', '0');
"""),
    request('counters/get - orders = 4', 'counters/get', [('key', '{{ctrKey}}'), ('name', 'orders')],
            tests=json_ok() + "pm.test('value 4', () => pm.expect(b.value).to.eql(4));\n"),
    request('counters/add - fractional increment - 400', 'counters/add', [('key', '{{ctrKey}}'), ('increment', '1.5')],
            tests=error(400, 'whole number')),
    request('counters/add - non-numeric increment - 400', 'counters/add', [('key', '{{ctrKey}}'), ('increment', 'abc')],
            tests=error(400, 'whole number')),
    request('counters/add - increment over 1e15 - 400', 'counters/add', [('key', '{{ctrKey}}'), ('increment', '1000000000000001')],
            tests=error(400, 'between')),
    request('counters/add - name with a space - 400', 'counters/add', [('key', '{{ctrKey}}'), ('name', 'bad%20name')],
            tests=error(400, 'name')),
    request('counters/get - name over 64 characters - 400', 'counters/get', [('key', '{{ctrKey}}'), ('name', '{{longCounter}}')],
            pre="pm.collectionVariables.set('longCounter', 'c'.repeat(65));", tests=error(400, 'name')),
])

# ================================================================== 06 cleanup register
cleanup = folder('06 Cleanup register - cleanup/push, cleanup/pop, cleanup/list', """Remember everything a test created, then let a teardown script walk the list
deleting each one. `cleanup/pop` hands entries out oldest first, each exactly once, and marks them
removed as it hands them over. Duplicates are kept (two creates = two deletes).""", [
    request('cleanup/list - empty register', 'cleanup/list', [('key', '{{cleanKey}}')], tests=json_ok() + r"""
pm.test('nothing saved', () => {
  pm.expect(b.saved).to.eql(0); pm.expect(b.removed).to.eql(0); pm.expect(b.pending).to.eql(0);
  pm.expect(b.nextPop).to.eql(1); pm.expect(b.filter).to.eql('all'); pm.expect(b.records).to.eql([]);
});
"""),
    request('cleanup/pop - empty register is exhausted', 'cleanup/pop', [('key', '{{cleanKey}}')], tests=json_ok() + r"""
pm.test('exhausted, null value', () => { pm.expect(b.exhausted).to.eql(true); pm.expect(b.value).to.eql(null); });
"""),
    request('cleanup/push - ORD-1 with a note', 'cleanup/push', [('key', '{{cleanKey}}'), ('value', 'ORD-1'), ('note', 'vu1')],
            tests=json_ok() + r"""
pm.test('registered as index 1 with its note', () => {
  pm.expect(b.registered).to.eql(true); pm.expect(b.index).to.eql(1);
  pm.expect(b.value).to.eql('ORD-1'); pm.expect(b.note).to.eql('vu1'); pm.expect(b.savedTime).to.be.a('number');
});
"""),
    request('cleanup/push - ORD-2 without a note', 'cleanup/push', [('key', '{{cleanKey}}'), ('value', 'ORD-2')],
            tests=json_ok() + r"""
pm.test('index 2, note null', () => { pm.expect(b.index).to.eql(2); pm.expect(b.note).to.eql(null); });
"""),
    request('cleanup/push - duplicate ORD-1 is kept', 'cleanup/push', [('key', '{{cleanKey}}'), ('value', 'ORD-1'), ('note', 'vu2')],
            tests=json_ok() + "pm.test('index 3', () => pm.expect(b.index).to.eql(3));\n"),
    request('cleanup/push - value with / ? & = (URL-encoded)', 'cleanup/push',
            [('key', '{{cleanKey}}'), ('value', 'cust%2F42%3Fx%3D1%26y%3D2')], tests=json_ok() + r"""
pm.test('stored exactly', () => pm.expect(b.value).to.eql('cust/42?x=1&y=2'));
pm.test('index 4', () => pm.expect(b.index).to.eql(4));
"""),
    request('cleanup/push - missing value - 400', 'cleanup/push', [('key', '{{cleanKey}}')], tests=error(400, 'value')),
    request('cleanup/push - empty value - 400', 'cleanup/push', [('key', '{{cleanKey}}'), ('value', '')], tests=error(400, 'value')),
    request('cleanup/list - status=pending', 'cleanup/list', [('key', '{{cleanKey}}'), ('status', 'pending')],
            tests=json_ok() + r"""
pm.test('4 saved, 4 pending', () => {
  pm.expect(b.saved).to.eql(4); pm.expect(b.pending).to.eql(4); pm.expect(b.returned).to.eql(4);
  pm.expect(b.filter).to.eql('pending');
});
pm.test('in saved order', () => pm.expect(b.records.map(r => r.value)).to.eql(['ORD-1', 'ORD-2', 'ORD-1', 'cust/42?x=1&y=2']));
"""),
    request('cleanup/pop - oldest first', 'cleanup/pop', [('key', '{{cleanKey}}')], tests=json_ok() + r"""
pm.test('index 1: ORD-1 / vu1', () => {
  pm.expect(b.exhausted).to.eql(false); pm.expect(b.index).to.eql(1);
  pm.expect(b.value).to.eql('ORD-1'); pm.expect(b.note).to.eql('vu1');
});
pm.test('marked removed now', () => pm.expect(b.removedTime).to.be.at.least(b.savedTime));
pm.collectionVariables.set('cleanupSeen', JSON.stringify([]));
"""),
    request('cleanup/list - status=removed', 'cleanup/list', [('key', '{{cleanKey}}'), ('status', 'removed')],
            tests=json_ok() + r"""
pm.test('1 removed, 3 pending', () => {
  pm.expect(b.removed).to.eql(1); pm.expect(b.pending).to.eql(3); pm.expect(b.returned).to.eql(1);
  pm.expect(b.records[0].removedTime).to.be.a('number'); pm.expect(b.nextPop).to.eql(2);
});
"""),
    request('cleanup/pop - teardown loop until exhausted', 'cleanup/pop', [('key', '{{cleanKey}}')],
            desc='The teardown pattern: keep popping, delete each `value` in the system under test, stop when `exhausted`.',
            tests=NEXT + json_ok() + r"""
const n = Number(pm.collectionVariables.get('loopCount')) + 1;
const seen = JSON.parse(pm.collectionVariables.get('cleanupSeen'));
if (!b.exhausted) {
  seen.push(b.value);
  pm.collectionVariables.set('cleanupSeen', JSON.stringify(seen));
  // ... delete b.value in the system under test here ...
}
if (!b.exhausted && n < 20) { pm.collectionVariables.set('loopCount', String(n)); next(pm.info.requestName); }
else {
  pm.collectionVariables.set('loopCount', '0');
  pm.test('remaining entries handed out once each, oldest first', () =>
    pm.expect(seen).to.eql(['ORD-2', 'ORD-1', 'cust/42?x=1&y=2']));
}
"""),
    request('cleanup/list - nothing pending', 'cleanup/list', [('key', '{{cleanKey}}')], tests=json_ok() + r"""
pm.test('4 saved, 4 removed, 0 pending', () => {
  pm.expect(b.saved).to.eql(4); pm.expect(b.removed).to.eql(4); pm.expect(b.pending).to.eql(0); pm.expect(b.nextPop).to.eql(5);
});
pm.test('audit trail keeps every entry', () => pm.expect(b.records.length).to.eql(4));
"""),
    request('cleanup/list - bad status - 400', 'cleanup/list', [('key', '{{cleanKey}}'), ('status', 'done')],
            tests=error(400, 'status') + "pm.test('allowed values listed', () => pm.expect(b.allowed).to.eql(['all', 'pending', 'removed']));\n"),
])

# ================================================================== 07 import CSV
CSV1 = ('email,password,firstName,postcode\n'
        'u1@example.com,Pa55-1,Ann,0800\n'
        'u2@example.com,Pa55-2,"Smith, Jo",3000\n'
        'u3@example.com,"Pa""55-3",Bo,3039\n')

IMPORT_DONE = r"""
pm.test('state done', () => pm.expect(b.state).to.eql('done'));
pm.test('ref looks like 20261011T031500Z-k3x9qa', () => pm.expect(b.ref).to.match(""" + REF_RE + r"""));
pm.test('ready-made next URLs', () => {
  for (const k of ['pop', 'random', 'reset', 'list', 'reload']) pm.expect(b.next[k], k).to.include('/lists/');
});
"""

csv_import = folder('07 Import - CSV (request body)', """`POST lists/import?key=...&format=csv` with the file as the body (Content-Type
`text/csv`, up to 4 MB). An import **replaces** the key's records and rewinds `pop` to 1, atomically;
a failed import leaves the old records alone. Values are strings (leading zeros kept); the
delimiter is auto-detected unless you pass `delimiter=comma|semicolon|tab|pipe`.""", [
    request('import CSV - 3 users (quoted comma, escaped quote, leading zero)', 'import',
            [('key', '{{impKey}}'), ('format', 'csv')], method='POST', body=CSV1, ctype='text/csv',
            tests=json_ok() + IMPORT_DONE + r"""
pm.test('3 loaded, none skipped', () => {
  pm.expect(b.loaded).to.eql(3); pm.expect(b.firstIndex).to.eql(1); pm.expect(b.lastIndex).to.eql(3);
  pm.expect(b.skippedCount).to.eql(0); pm.expect(b.skipped).to.eql([]);
});
pm.test('columns and delimiter', () => {
  pm.expect(b.columns).to.eql(['email', 'password', 'firstName', 'postcode']); pm.expect(b.delimiter).to.eql(',');
});
pm.test('source body, nothing replaced', () => { pm.expect(b.source).to.eql('body'); pm.expect(b.replaced).to.eql(null); });
pm.test('sample is record 1 as pop returns it', () => {
  pm.expect(b.sample.email).to.eql('u1@example.com'); pm.expect(b.sample.index).to.eql(1);
  pm.expect(b.sample.key).to.eql(pm.collectionVariables.get('impKey'));
});
pm.test('postcode keeps its leading zero', () => pm.expect(b.sample.postcode).to.eql('0800'));
pm.collectionVariables.set('csvRef', b.ref);
"""),
    request('pop - record 1', 'pop', [('key', '{{impKey}}')], tests=json_ok() + r"""
pm.test('u1 / Pa55-1 / 0800', () => {
  pm.expect(b.record.email).to.eql('u1@example.com'); pm.expect(b.record.password).to.eql('Pa55-1');
  pm.expect(b.record.postcode).to.eql('0800'); pm.expect(b.remaining).to.eql(2);
});
"""),
    request('pop - quoted value containing a comma', 'pop', [('key', '{{impKey}}')], tests=json_ok() + r"""
pm.test('firstName "Smith, Jo"', () => pm.expect(b.record.firstName).to.eql('Smith, Jo'));
"""),
    request('pop - escaped double quote', 'pop', [('key', '{{impKey}}')], tests=json_ok() + r"""
pm.test('password Pa"55-3', () => pm.expect(b.record.password).to.eql('Pa"55-3'));
"""),
    request('list - shows the import ref', 'list', [('key', '{{impKey}}')], tests=json_ok() + r"""
pm.test('count 3, importRef', () => {
  pm.expect(b.count).to.eql(3); pm.expect(b.importRef).to.eql(pm.collectionVariables.get('csvRef'));
});
"""),
    request('import CSV - semicolons (auto-detected) replace the records', 'import',
            [('key', '{{impKey}}'), ('format', 'csv')], method='POST', ctype='text/csv',
            body='email;plan\nx1@example.com;gold\nx2@example.com;silver\n', tests=json_ok() + IMPORT_DONE + r"""
pm.test('2 loaded, delimiter ;', () => { pm.expect(b.loaded).to.eql(2); pm.expect(b.delimiter).to.eql(';'); });
pm.test('warns that the delimiter was auto-detected', () => pm.expect(b.warnings.join(' ')).to.include('auto-detected'));
pm.test('reports what it replaced', () => {
  pm.expect(b.replaced.count).to.eql(3); pm.expect(b.replaced.importRef).to.eql(pm.collectionVariables.get('csvRef'));
});
"""),
    request('list - replaced, and pop rewound to 1', 'list', [('key', '{{impKey}}')], tests=json_ok() + r"""
pm.test('count 2, nextPop 1', () => { pm.expect(b.count).to.eql(2); pm.expect(b.nextPop).to.eql(1); });
pm.test('new records', () => pm.expect(b.records.map(r => r.plan)).to.eql(['gold', 'silver']));
"""),
    request('import CSV - delimiter=pipe', 'import', [('key', '{{impKey}}'), ('format', 'csv'), ('delimiter', 'pipe')],
            method='POST', ctype='text/csv', body='sku|qty\nAP-S|1\nDB-S|2\n', tests=json_ok() + IMPORT_DONE + r"""
pm.test('2 loaded, delimiter |', () => { pm.expect(b.loaded).to.eql(2); pm.expect(b.delimiter).to.eql('|'); });
pm.test('no auto-detect warning', () => pm.expect(b.warnings.join(' ')).to.not.include('auto-detected'));
pm.test('sample', () => pm.expect(b.sample).to.include({ sku: 'AP-S', qty: '1' }));
"""),
    request('import CSV - Windows line endings (CRLF)', 'import', [('key', '{{impKey}}'), ('format', 'csv')],
            method='POST', ctype='text/csv', body='id,name\r\n1,A\r\n2,B\r\n', tests=json_ok() + IMPORT_DONE + r"""
pm.test('2 loaded, no \r in values', () => { pm.expect(b.loaded).to.eql(2); pm.expect(b.sample.name).to.eql('A'); });
"""),
    request('import CSV - reserved column names are renamed', 'import', [('key', '{{impKey}}'), ('format', 'csv')],
            method='POST', ctype='text/csv', body='index,key,name\n7,k1,Zed\n', tests=json_ok() + IMPORT_DONE + r"""
pm.test('index -> index_2, key -> key_2', () => {
  pm.expect(b.sample.index_2).to.eql('7'); pm.expect(b.sample.key_2).to.eql('k1'); pm.expect(b.sample.index).to.eql(1);
});
pm.test('warns about the rename', () => pm.expect(b.warnings.join(' ').toLowerCase()).to.include('index_2'));
"""),
    request('import CSV - header only fails (422), key unchanged', 'import', [('key', '{{impKey}}'), ('format', 'csv')],
            method='POST', ctype='text/csv', body='email,password\n', tests=error(422, 'no records') + r"""
pm.test('state failed', () => pm.expect(b.state).to.eql('failed'));
"""),
    request('list - the failed import changed nothing', 'list', [('key', '{{impKey}}')], tests=json_ok() + r"""
pm.test('still the 1-record reserved-name import', () => { pm.expect(b.count).to.eql(1); pm.expect(b.records[0].name).to.eql('Zed'); });
"""),
    request('import by ref - reload the first CSV', 'import', [('key', '{{impKey}}'), ('format', 'csv'), ('ref', '{{csvRef}}')],
            desc='Every uploaded file is kept for 90 days. GET import with its `ref` loads it again - handy after a clear.',
            tests=json_ok() + IMPORT_DONE + r"""
pm.test('source reload, 3 loaded', () => { pm.expect(b.source).to.eql('reload'); pm.expect(b.loaded).to.eql(3); });
pm.test('same ref', () => pm.expect(b.ref).to.eql(pm.collectionVariables.get('csvRef')));
"""),
    request('import/status - newest upload for the key (no ref)', 'import/status', [('key', '{{impKey}}')],
            desc='Without `ref`, reports the newest upload for the key plus `recent` (up to 20 refs, newest first). Refs carry a one-second timestamp, so uploads within the same second have no defined order.',
            tests="const b = pm.response.json();\n" + r"""
pm.test('200 done or 422 failed - this key had a failed import', () => pm.expect(pm.response.code).to.be.oneOf([200, 422]));
pm.test('reports the newest ref (recent[0])', () => pm.expect(b.ref).to.eql(b.recent[0].ref));
pm.test('recent uploads listed newest first', () => {
  pm.expect(b.recent.length).to.be.at.least(5);
  const refs = b.recent.map(r => r.ref);
  pm.expect(refs).to.eql([...refs].sort().reverse());
  pm.expect(refs).to.include(pm.collectionVariables.get('csvRef'));
});
"""),
])

# ================================================================== 08 import JSON
JSON_TYPES = ('[\n'
              '  {"user": "j1", "age": 31, "active": true, "tags": ["a", "b"], '
              '"address": {"city": "Melbourne", "postcode": "3000"}, "accountId": 12345678901234567890},\n'
              '  {"user": "j2", "age": 40, "active": false, "tags": [], '
              '"address": {"city": "Sydney", "postcode": "2000"}, "accountId": "0042"}\n'
              ']\n')

json_import = folder('08 Import - JSON (request body)', """`format=json` accepts an array of objects, an object holding one such array,
JSON Lines, or a single object. JSON keeps its types (numbers, booleans, nested objects, arrays);
integers too big for JavaScript are kept exactly as strings.""", [
    request('import JSON array - types preserved', 'import', [('key', '{{jsonKey}}'), ('format', 'json')],
            method='POST', ctype='application/json', body=JSON_TYPES, tests=json_ok() + IMPORT_DONE + r"""
pm.test('2 loaded from an array', () => { pm.expect(b.loaded).to.eql(2); pm.expect(b.shape).to.eql('array'); });
pm.test('number, boolean, array and object keep their types', () => {
  pm.expect(b.sample.age).to.eql(31); pm.expect(b.sample.active).to.eql(true);
  pm.expect(b.sample.tags).to.eql(['a', 'b']); pm.expect(b.sample.address).to.eql({ city: 'Melbourne', postcode: '3000' });
});
pm.test('huge integer kept exactly, as a string', () => pm.expect(b.sample.accountId).to.eql('12345678901234567890'));
pm.test('warns about the huge integer', () => pm.expect(b.warnings.join(' ')).to.include('too large'));
pm.collectionVariables.set('jsonRef', b.ref);
"""),
    request('pop - JSON path into a nested field', 'pop', [('key', '{{jsonKey}}')], tests=json_ok() + r"""
pm.test('$.record.address.city', () => pm.expect(b.record.address.city).to.eql('Melbourne'));
pm.test('$.record.tags[1]', () => pm.expect(b.record.tags[1]).to.eql('b'));
"""),
    request('random - typed record', 'random', [('key', '{{jsonKey}}')], tests=json_ok() + r"""
pm.test('age is a number', () => pm.expect(b.record.age).to.be.a('number'));
"""),
    request('import JSON - object wrapping an array', 'import', [('key', '{{jsonKey}}'), ('format', 'json')],
            method='POST', ctype='application/json',
            body='{"meta": {"version": 1}, "users": [{"u": "a"}, {"u": "b"}, {"u": "c"}]}',
            tests=json_ok() + IMPORT_DONE + r"""
pm.test('loaded the users array', () => { pm.expect(b.shape).to.eql('object.users'); pm.expect(b.loaded).to.eql(3); });
pm.test('warns that meta was ignored', () => pm.expect(b.warnings.join(' ')).to.include('meta'));
"""),
    request('import JSON Lines - bad line skipped, rest loaded', 'import', [('key', '{{jsonKey}}'), ('format', 'json')],
            method='POST', ctype='application/json', body='{"u": "l1"}\n{"u": "l2"}\nnot json\n{"u": "l3"}\n',
            tests=json_ok() + IMPORT_DONE + r"""
pm.test('json lines, 3 loaded, 1 skipped', () => {
  pm.expect(b.shape).to.eql('json lines'); pm.expect(b.loaded).to.eql(3); pm.expect(b.skippedCount).to.eql(1);
});
pm.test('skipped entry says which line and why', () => {
  pm.expect(b.skipped[0].line).to.eql(3); pm.expect(b.skipped[0].reason).to.be.a('string');
});
"""),
    request('import JSON - a single object is one record', 'import', [('key', '{{jsonKey}}'), ('format', 'json')],
            method='POST', ctype='application/json', body='{"u": "only", "n": 1}', tests=json_ok() + IMPORT_DONE + r"""
pm.test('single object, 1 loaded, with a warning', () => {
  pm.expect(b.shape).to.eql('single object'); pm.expect(b.loaded).to.eql(1);
  pm.expect(b.warnings.join(' ')).to.include('ONE record');
});
"""),
    request('import JSON - non-object items are wrapped as {value}', 'import', [('key', '{{jsonKey}}'), ('format', 'json')],
            method='POST', ctype='application/json', body='[1, "two", null]', tests=json_ok() + IMPORT_DONE + r"""
pm.test('3 loaded', () => pm.expect(b.loaded).to.eql(3));
pm.test('sample {value: 1}', () => pm.expect(b.sample.value).to.eql(1));
pm.test('warns about wrapping', () => pm.expect(b.warnings.join(' ')).to.include('not objects'));
"""),
    request('import JSON - invalid JSON fails (422)', 'import', [('key', '{{jsonKey}}'), ('format', 'json')],
            method='POST', ctype='application/json', body='[{"a": 1},\n {"b": 2\n', tests=error(422) + r"""
pm.test('state failed with a reason', () => { pm.expect(b.state).to.eql('failed'); pm.expect(b.error.length).to.be.above(5); });
"""),
    request('list - failed import left the 3 wrapped records', 'list', [('key', '{{jsonKey}}')], tests=json_ok() + r"""
pm.test('count 3', () => pm.expect(b.count).to.eql(3));
"""),
    request('import by ref - reload the typed JSON', 'import', [('key', '{{jsonKey}}'), ('format', 'json'), ('ref', '{{jsonRef}}')],
            tests=json_ok() + IMPORT_DONE + r"""
pm.test('reload, 2 loaded, ages are numbers', () => {
  pm.expect(b.source).to.eql('reload'); pm.expect(b.loaded).to.eql(2); pm.expect(b.sample.age).to.eql(31);
});
"""),
])

# ================================================================== 09 async import + errors
async_import = folder('09 Import - asynchronous (wait=0) and errors', """`wait` is how many seconds `import` holds the request for the result (0-25,
default 25). `wait=0` returns 202 `queued` at once; poll `import/status` with the `ref` until
`state` is `done` (200) or `failed` (422). Always branch on `state`, not just the HTTP status.""", [
    request('import CSV - wait=0 returns 202 queued', 'import', [('key', '{{asyncKey}}'), ('format', 'csv'), ('wait', '0')],
            method='POST', ctype='text/csv', body='n\n1\n2\n3\n4\n', tests=json_ok(202) + r"""
pm.test('state queued, with a ref', () => {
  pm.expect(b.state).to.eql('queued'); pm.expect(b.ref).to.match(""" + REF_RE + r""");
  pm.expect(b.fileBytes).to.be.above(0);
});
pm.collectionVariables.set('asyncRef', b.ref);
"""),
    request('import/status - poll until done', 'import/status', [('key', '{{asyncKey}}'), ('ref', '{{asyncRef}}')],
            pre="""// wait a second between polls
if (Number(pm.collectionVariables.get('loopCount')) > 0) setTimeout(() => {}, 1000);
""", tests=NEXT + r"""
const b = pm.response.json();
const n = Number(pm.collectionVariables.get('loopCount')) + 1;
pm.test('poll ' + n + ': 200 or 202', () => pm.expect(pm.response.code).to.be.oneOf([200, 202]));
pm.test('poll ' + n + ': state matches the HTTP status', () => {
  if (pm.response.code === 202) pm.expect(b.state).to.be.oneOf(['queued', 'running']);
  else pm.expect(b.state).to.eql('done');
});
if (b.state !== 'done' && b.state !== 'failed' && n < 30) {
  pm.collectionVariables.set('loopCount', String(n)); next(pm.info.requestName);
} else {
  pm.collectionVariables.set('loopCount', '0');
  pm.test('finished: done, 4 loaded', () => { pm.expect(b.state).to.eql('done'); pm.expect(b.loaded).to.eql(4); });
}
"""),
    request('missing format - 400', 'import', [('key', '{{asyncKey}}')], method='POST', ctype='text/csv',
            body='a\n1\n', tests=error(400, 'format') + "pm.test('allowed formats', () => pm.expect(b.allowed).to.eql(['csv', 'json']));\n"),
    request('format=xml - 400', 'import', [('key', '{{asyncKey}}'), ('format', 'xml')], method='POST', ctype='text/csv',
            body='a\n1\n', tests=error(400, 'csv or json')),
    request('delimiter=colon - 400', 'import', [('key', '{{asyncKey}}'), ('format', 'csv'), ('delimiter', 'colon')],
            method='POST', ctype='text/csv', body='a\n1\n', tests=error(400, 'delimiter')),
    request('wait=soon - 400', 'import', [('key', '{{asyncKey}}'), ('format', 'csv'), ('wait', 'soon')],
            method='POST', ctype='text/csv', body='a\n1\n', tests=error(400, 'wait')),
    request('GET with neither body nor ref - 400 with a hint', 'import', [('key', '{{asyncKey}}'), ('format', 'csv')],
            tests=error(400, 'nothing to import') + "pm.test('hint points at the upload page', () => pm.expect(b.hint).to.include('/lists/upload'));\n"),
    request('body AND ref - 400', 'import', [('key', '{{asyncKey}}'), ('format', 'csv'), ('ref', '{{asyncRef}}')],
            method='POST', ctype='text/csv', body='a\n1\n', tests=error(400, 'not both')),
    request('malformed ref - 400', 'import', [('key', '{{asyncKey}}'), ('format', 'csv'), ('ref', 'abc')],
            tests=error(400, 'ref')),
    request('unknown ref - 404', 'import', [('key', '{{asyncKey}}'), ('format', 'csv'), ('ref', '20200101T000000Z-aaaaaa')],
            tests=error(404, 'no csv upload')),
    request('ref with the wrong format - 404', 'import', [('key', '{{asyncKey}}'), ('format', 'json'), ('ref', '{{asyncRef}}')],
            tests=error(404, 'no json upload')),
    request('ref belongs to another key - 404', 'import', [('key', '{{impKey}}'), ('format', 'csv'), ('ref', '{{asyncRef}}')],
            tests=error(404)),
    request('import/status - key with no uploads - 404', 'import/status', [('key', '{{unusedKey}}')],
            tests=error(404, 'no uploads')),
    request('import/status - unknown ref - 404', 'import/status', [('key', '{{asyncKey}}'), ('ref', '20200101T000000Z-aaaaaa')],
            tests=error(404)),
])

# ================================================================== 10 S3 upload
def _with_form(item):
    item['request']['body'] = {'mode': 'formdata', 'formdata': []}
    return item


UPLOAD_PRE = r"""// Build the multipart form from the fields upload/url returned: every field first,
// the file LAST as the part named "file". S3 accepts text content for "file".
const fields = JSON.parse(pm.collectionVariables.get('uploadFields'));
const form = Object.entries(fields).map(([key, value]) => ({ key, value, type: 'text' }));
form.push({ key: 'file', value: pm.collectionVariables.get('uploadCsv'), type: 'text' });
pm.request.body.update({ mode: 'formdata', formdata: form });
"""

upload = folder('10 Large files - upload/url, S3 POST, import by ref', """Files over 4 MB (up to 50 MB) skip API Gateway: `upload/url` returns a presigned
S3 POST (`url` + `fields`), you POST the file straight to S3, then GET the `next` URL to import it.
This folder does the whole round trip with a small CSV built in a script.""", [
    request('upload/url - presigned S3 POST', 'upload/url', [('key', '{{upKey}}'), ('format', 'csv'), ('size', '200')],
            pre=r"""pm.collectionVariables.set('uploadCsv', 'email,plan\nbig1@example.com,gold\nbig2@example.com,silver\nbig3@example.com,bronze\n');
""", tests=json_ok() + r"""
pm.test('POST to an https S3 url', () => { pm.expect(b.method).to.eql('POST'); pm.expect(b.url).to.match(/^https?:\/\//); });
pm.test('signed fields', () => {
  for (const f of ['key', 'policy', 'x-amz-algorithm', 'x-amz-credential', 'x-amz-date', 'x-amz-signature', 'success_action_status'])
    pm.expect(b.fields, f).to.have.property(f);
});
pm.test('object key is imports/<key>/<ref>.csv', () =>
  pm.expect(b.fields.key).to.eql('imports/' + pm.collectionVariables.get('upKey') + '/' + b.ref + '.csv'));
pm.test('limits', () => { pm.expect(b.maxKb).to.eql(51200); pm.expect(b.expiresInSeconds).to.eql(900); });
pm.test('next is the import URL for this ref', () => pm.expect(b.next).to.include('/lists/import?') && pm.expect(b.next).to.include(b.ref));
pm.collectionVariables.set('uploadUrl', b.url);
pm.collectionVariables.set('uploadFields', JSON.stringify(b.fields));
pm.collectionVariables.set('uploadRef', b.ref);
pm.collectionVariables.set('uploadNext', b.next);
"""),
    _with_form(request('S3 - POST the file (fields first, file last)', '', method='POST', pre=UPLOAD_PRE,
               raw_url={'raw': '{{uploadUrl}}', 'host': ['{{uploadUrl}}']},
               desc='The pre-request script fills the form from the saved `fields`; the file part is a small CSV held in a variable.',
               tests=r"""
pm.test('S3 answers 201', () => pm.response.to.have.status(201));
""")),
    request('import/status - uploaded, not imported yet', 'import/status', [('key', '{{upKey}}'), ('ref', '{{uploadRef}}')],
            tests=json_ok() + r"""
pm.test('state uploaded', () => { pm.expect(b.state).to.eql('uploaded'); pm.expect(b.format).to.eql('csv'); });
pm.test('fileBytes is the file size', () => pm.expect(b.fileBytes).to.eql(pm.collectionVariables.get('uploadCsv').length));
pm.test('next given', () => pm.expect(b.next).to.include(pm.collectionVariables.get('uploadRef')));
"""),
    request('import - GET the next URL', '', raw_url={'raw': '{{uploadNext}}', 'host': ['{{uploadNext}}']},
            tests=json_ok() + IMPORT_DONE + r"""
pm.test('source upload, 3 loaded', () => { pm.expect(b.source).to.eql('upload'); pm.expect(b.loaded).to.eql(3); });
pm.test('sample big1', () => pm.expect(b.sample.email).to.eql('big1@example.com'));
"""),
    request('import/status - done', 'import/status', [('key', '{{upKey}}'), ('ref', '{{uploadRef}}')], tests=json_ok() + r"""
pm.test('done, 3 loaded', () => { pm.expect(b.state).to.eql('done'); pm.expect(b.loaded).to.eql(3); });
"""),
    request('pop - from the uploaded file', 'pop', [('key', '{{upKey}}')], tests=json_ok() + r"""
pm.test('big1, gold', () => pm.expect(b.record).to.include({ email: 'big1@example.com', plan: 'gold' }));
"""),
    request('upload/url - over 50 MB - 400', 'upload/url', [('key', '{{upKey}}'), ('format', 'csv'), ('size', '52428801')],
            tests=error(400, 'limit') + "pm.test('maxKb', () => pm.expect(b.maxKb).to.eql(51200));\n"),
    request('upload/url - size not a number - 400', 'upload/url', [('key', '{{upKey}}'), ('format', 'csv'), ('size', '10MB')],
            tests=error(400, 'size')),
    request('upload/url - missing format - 400', 'upload/url', [('key', '{{upKey}}')], tests=error(400, 'format')),
])

# ================================================================== 11 upload page
page = folder('11 Upload page (for humans)', """`lists/upload` is a drag-and-drop page that does the upload/url -> S3 -> import
round trip in the browser. Open it in a browser; here we only check it is served.""", [
    request('upload page - csv (default)', 'upload', tests=r"""
pm.test('HTTP 200', () => pm.response.to.have.status(200));
pm.test('HTML', () => pm.expect(pm.response.headers.get('Content-Type')).to.include('text/html'));
pm.test('the loader page', () => pm.expect(pm.response.text()).to.include('Test-data loader'));
"""),
    request('upload page - format=json', 'upload', [('format', 'json')], tests=r"""
pm.test('HTTP 200 HTML', () => { pm.response.to.have.status(200); pm.expect(pm.response.text()).to.include('<html'); });
"""),
    request('upload page - format=xml - 400', 'upload', [('format', 'xml')], tests=error(400, 'csv or json')),
])

# ================================================================== 12 e2e
e2e = folder('12 Scenario - one load-test iteration end to end', """How the pieces fit together in a real script: load users before the run, give
each iteration its own user, number the orders, hand the order id to another script, register it
for cleanup, and tear down afterwards.""", [
    request('1. setup - import the users', 'import', [('key', '{{e2eKey}}'), ('format', 'csv')], method='POST',
            ctype='text/csv', body='email,password\nshopper1@example.com,Pa55-1\nshopper2@example.com,Pa55-2\n',
            tests=json_ok() + "pm.test('2 users loaded', () => { pm.expect(b.state).to.eql('done'); pm.expect(b.loaded).to.eql(2); });\n"),
    request('2. setup - save the target URL for every script', 'vars/set',
            [('key', '{{e2eKey}}'), ('name', 'shopUrl'), ('value', 'https%3A%2F%2Fshop.verdant-v1.testthis.now')],
            tests=json_ok()),
    request('3. iteration - take a user', 'pop', [('key', '{{e2eKey}}')], tests=json_ok() + r"""
pm.test('got a user', () => pm.expect(b.exhausted).to.eql(false));
pm.collectionVariables.set('e2eUser', b.record.email);
pm.collectionVariables.set('e2ePassword', b.record.password);
"""),
    request('4. iteration - read the target URL', 'vars/get', [('key', '{{e2eKey}}'), ('name', 'shopUrl')],
            tests=json_ok() + "pm.test('shop url', () => pm.expect(b.value).to.eql('https://shop.verdant-v1.testthis.now'));\n"),
    request('5. iteration - next order number', 'counters/add', [('key', '{{e2eKey}}'), ('name', 'orders')],
            tests=json_ok() + r"""
pm.test('order 1', () => pm.expect(b.value).to.eql(1));
pm.collectionVariables.set('e2eOrder', 'ORD-' + String(b.value).padStart(6, '0'));
"""),
    request('6. iteration - (place the order in the app) then register it for cleanup', 'cleanup/push',
            [('key', '{{e2eKey}}'), ('value', '{{e2eOrder}}'), ('note', '{{e2eUser}}')], tests=json_ok() + r"""
pm.test('registered', () => { pm.expect(b.value).to.eql('ORD-000001'); pm.expect(b.note).to.eql(pm.collectionVariables.get('e2eUser')); });
"""),
    request('7. iteration - hand the order id to the checking script', 'vars/set',
            [('key', '{{e2eKey}}'), ('name', 'lastOrder'), ('value', '{{e2eOrder}}')], tests=json_ok()),
    request('8. checking script - read it back', 'vars/get', [('key', '{{e2eKey}}'), ('name', 'lastOrder')],
            tests=json_ok() + "pm.test('ORD-000001', () => pm.expect(b.value).to.eql('ORD-000001'));\n"),
    request('9. teardown - delete what was created', 'cleanup/pop', [('key', '{{e2eKey}}')], tests=NEXT + json_ok() + r"""
const n = Number(pm.collectionVariables.get('loopCount')) + 1;
if (!b.exhausted) pm.test('handed ORD-000001 with its user', () => {
  pm.expect(b.value).to.eql('ORD-000001'); pm.expect(b.note).to.eql(pm.collectionVariables.get('e2eUser'));
});
if (!b.exhausted && n < 20) { pm.collectionVariables.set('loopCount', String(n)); next(pm.info.requestName); }
else pm.collectionVariables.set('loopCount', '0');
"""),
    request('10. teardown - confirm nothing pending', 'cleanup/list', [('key', '{{e2eKey}}'), ('status', 'pending')],
            tests=json_ok() + "pm.test('pending 0', () => pm.expect(b.pending).to.eql(0));\n"),
    request('11. teardown - clear the users', 'clear', [('key', '{{e2eKey}}')],
            tests=json_ok() + "pm.test('cleared', () => pm.expect(b.cleared).to.eql(true));\n"),
])

# ================================================================== 13 teardown
teardown = folder('13 Teardown', """Clears the record lists this run created. Variables, counters and the cleanup
register have no delete action; they are harmless under this run's random keys.""", [
    request('clear ' + k, 'clear', [('key', '{{%s}}' % k)], tests=json_ok() + "pm.test('done', () => pm.expect(b).to.have.property('cleared'));\n")
    for k in ('qKey', 'cycKey', 'impKey', 'jsonKey', 'asyncKey', 'upKey')
])

# ================================================================== collection
COMMON_TEST = r"""// Runs after every request in the collection.
const ct = pm.response.headers.get('Content-Type') || '';
if (ct.includes('application/json')) {
  pm.test('[all] body is JSON', () => pm.response.json());
  const body = pm.response.json();
  if (pm.response.code >= 400) {
    pm.test('[all] error shape {error, status}', () => {
      pm.expect(body.error).to.be.a('string');
      pm.expect(body.status).to.eql(pm.response.code);
    });
  }
}
if (!String(pm.request.url).includes('amazonaws.com') && pm.request.method !== 'OPTIONS' && pm.response.code !== 201) {
  pm.test('[all] CORS header', () => pm.expect(pm.response.headers.get('Access-Control-Allow-Origin')).to.eql('*'));
}
"""

DESC = """Self-checking tests for every **/v1/lists** action of the Test Helper API
(https://testdata.help): queues (`push` `pop` `cyclepop` `random` `list` `reset` `clear` `codes`), variables
(`vars/*`), counters (`counters/*`), the cleanup register (`cleanup/*`), and bulk import (`import`,
`import/status`, `upload/url`, `upload`).

**Run it as a collection** (Collection Runner, or `newman run`). Later requests use variables saved by
earlier ones, and the loops (pop until exhausted, poll an import, teardown) use `setNextRequest`, which
only works in a run. Each run makes its own keys from `lists/codes` (`pm-<rand10>-...`), so it is safe to
run repeatedly and in parallel against the same deployment.

Change `baseUrl` to point at another deployment. Nothing needs authentication.

Conventions checked throughout: parameter names are case-insensitive; responses are camelCase JSON;
"nothing there" is 200 with a flag (`exhausted`, `empty`, `exists:false`), never an error; errors are
`{"error": "...", "status": N}`; an unknown action is 404 listing the valid ones.

Not covered: the 413 for a request body over 4 MB (Postman would have to send it), and concurrency
(use a load-test tool to show that `pop` and `counters/add` never hand the same value to two callers).

Guide: {{baseUrl}}/llms.txt   OpenAPI: {{baseUrl}}/openapi.json"""

coll = {
    'info': {
        '_postman_id': _id('collection'),
        'name': 'testdata.help - lists (v1) - full coverage',
        'description': DESC,
        'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
    },
    'event': [{'listen': 'test', 'script': {'type': 'text/javascript', 'exec': COMMON_TEST.strip().split('\n')}}],
    'variable': [{'key': 'baseUrl', 'value': 'https://api.testdata.help/v1', 'type': 'string'}] + [
        {'key': k, 'value': '', 'type': 'string'} for k in (
            'runKey', 'qKey', 'emptyKey', 'varsKey', 'ctrKey', 'cleanKey', 'impKey', 'jsonKey', 'asyncKey',
            'upKey', 'e2eKey', 'unusedKey', 'cycKey')] + [{'key': 'loopCount', 'value': '0', 'type': 'string'}],
    'item': [setup, empty, queue, cyclepop, validation, vars_, counters, cleanup, csv_import, json_import,
             async_import, upload, page, e2e, teardown],
}


def _ids(items, path):
    for it in items:
        p = path + '/' + it['name']
        it['id'] = _id(p)
        if 'item' in it:
            _ids(it['item'], p)


_ids(coll['item'], '')

if __name__ == '__main__':
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(coll, f, indent=2, ensure_ascii=False)
        f.write('\n')
    n = sum(len(f['item']) for f in coll['item'])
    print('wrote %s (%d folders, %d requests)' % (OUT, len(coll['item']), n))
