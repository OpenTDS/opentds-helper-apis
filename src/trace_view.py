"""trace_view - the trace service: the AWS X-Ray trace of a request to this API, cleaned for the public.

  /v1/trace/get?ref=TRACE_ID               the trace as JSON
  /v1/trace/get?ref=TRACE_ID&format=html   the same as a page: where the time went
  ...&wait=SECONDS                         hold the request open (0-20) while X-Ray
                                           is still receiving the trace

A trace is written as its request finishes and takes a few seconds to arrive, so
a lookup that is too early answers 200 with pending: true (the page refreshes
itself). health/get and health/check return the URL of their own trace.

Public, so what it shows is an allow-list: request paths (never hosts or query
strings), methods, statuses, function and API names, timings and fault flags.
Never account ids, ARNs, request ids, caller IPs or user agents. Only this
deployment's segments are shown (names starting TRACE_PREFIX, the stack name):
a trace from any other application in the account is "not found".
"""
import html
import json
import os
import re
import time
import urllib.parse

import boto3

from common import ApiError, base_url, raw, respond, run

PREFIX = os.environ.get('TRACE_PREFIX', '')
TRACE_RE = re.compile(r'^1-[0-9a-f]{8}-[0-9a-f]{24}$')
PENDING_S = 120          # a trace this young that X-Ray does not have yet is "pending"
SETTLE_S = 30            # after this, every segment of the trace has arrived
WAIT_MAX_S = 20
POLL_S = 2.0
RETENTION_DAYS = 30      # X-Ray keeps traces 30 days

KINDS = {'AWS::ApiGateway::Stage': 'api', 'AWS::Lambda': 'lambda',
         'AWS::Lambda::Function': 'function'}
PHASES = {'Initialization': 'init', 'Invocation': 'invocation', 'Overhead': 'overhead',
          'Restore': 'init'}

_xray = None


def xray():
    global _xray
    if _xray is None:
        _xray = boto3.client('xray')
    return _xray


def started_time(ref):
    """X-Ray trace ids carry their start: 1-<epoch seconds, hex>-<random>."""
    return int(ref.split('-')[1], 16)


# ------------------------------------------------------------------ reading X-Ray
def fetch(ref):
    """-> the trace's segment documents (dicts), or None if X-Ray has none yet."""
    r = xray().batch_get_traces(TraceIds=[ref])
    traces = r.get('Traces') or []
    if not traces:
        return None
    docs = []
    for seg in traces[0].get('Segments') or []:
        try:
            docs.append(json.loads(seg.get('Document') or '{}'))
        except ValueError:
            continue
    return docs or None


def _ours(doc):
    return bool(PREFIX) and str(doc.get('name') or '').startswith(PREFIX)


def _path(url):
    """Path only: never the host (an API id) or the query string (callers' keys)."""
    p = urllib.parse.urlsplit(str(url or '')).path or ''
    if p.startswith('/live/'):          # execute-api URLs carry the stage name
        p = p[len('/live'):]
    if p and not p.startswith('/v1/'):  # the custom domain maps /v1 to the stage root
        p = '/v1' + p
    return p or None


def _http(node):
    h = node.get('http') or {}
    req, res = h.get('request') or {}, h.get('response') or {}
    out = {}
    if req.get('method'):
        out['method'] = str(req['method']).upper()
    if req.get('url'):
        out['path'] = _path(req['url'])
    if isinstance(res.get('status'), int):
        out['status'] = res['status']
    return out


def model(ref, docs, base):
    """The public view of a trace: spans (a tree, in start order), requests, clues."""
    docs = [d for d in docs if _ours(d)]
    if not docs:
        return None
    # Subsegments can start before their segment (a cold start's Initialization).
    times = []

    def span_times(n):
        if n.get('start_time') is not None:
            times.append((float(n['start_time']), float(n.get('end_time') or n['start_time'])))
        for sub in n.get('subsegments') or []:
            span_times(sub)
    for d in docs:
        span_times(d)
    t0 = min(a for a, _ in times)
    t1 = max(b for _, b in times)
    nodes, children, roots = {}, {}, []

    def add(node, parent_id, top):
        nid = node.get('id')
        start = float(node.get('start_time') or t0)
        end = node.get('end_time')
        entry = {'id': nid, 'name': str(node.get('name') or ''), 'startMs': round((start - t0) * 1000, 1),
                 'durationMs': round((float(end) - start) * 1000, 1) if end is not None else None}
        if top:
            entry['kind'] = KINDS.get(node.get('origin'), 'segment')
        else:
            entry['kind'] = PHASES.get(entry['name'], 'subsegment')
            if node.get('namespace') == 'aws' and (node.get('aws') or {}).get('operation'):
                entry['operation'] = str(node['aws']['operation'])
        entry.update(_http(node))
        for f in ('error', 'fault', 'throttle'):
            if node.get(f):
                entry[f] = True
        if node.get('in_progress'):
            entry['inProgress'] = True
        nodes[nid] = entry
        if parent_id:
            children.setdefault(parent_id, []).append(nid)
        else:
            roots.append(nid)
        for sub in node.get('subsegments') or []:
            add(sub, nid, False)

    known = set()

    def collect(n):
        known.add(n.get('id'))
        for sub in n.get('subsegments') or []:
            collect(sub)
    for d in docs:
        collect(d)
    for d in docs:
        parent = d.get('parent_id')
        # A parent that is not one of ours (another application's segment, or one
        # that has not arrived yet) makes this segment a root.
        add(d, parent if parent in known else None, True)

    # Depth-first, children in start order: the order the page draws them.
    spans = []

    def walk(nid, depth, parent_index):
        e = nodes[nid]
        e = dict(e, depth=depth, parent=parent_index, index=len(spans))
        e.pop('id', None)
        spans.append(e)
        me = e['index']
        for c in sorted(children.get(nid, []), key=lambda k: nodes[k]['startMs']):
            walk(c, depth + 1, me)
    for r in sorted(roots, key=lambda k: nodes[k]['startMs']):
        walk(r, 0, None)

    requests = _requests(spans)
    age = int(time.time() - t1)
    return {
        'traceId': ref,
        'startedTime': int(t0),
        'durationMs': round((t1 - t0) * 1000, 1),
        'ageSeconds': max(0, age),
        'settled': age >= SETTLE_S,
        'pending': False,
        'requestCount': len(requests),
        'spanCount': len(spans),
        'requests': requests,
        'clues': _clues(requests, spans),
        'spans': spans,
        'links': _links(base, ref),
    }


def _requests(spans):
    """One row per API request: the API Gateway span and the function behind it."""
    out = []
    for i, s in enumerate(spans):
        if s['kind'] != 'api':
            continue
        row = {'index': len(out) + 1, 'span': i, 'method': s.get('method'), 'path': s.get('path'),
               'status': s.get('status'), 'startMs': s['startMs'], 'durationMs': s['durationMs']}
        # The function under this request: the first 'function' span below it.
        for j in range(i + 1, len(spans)):
            t = spans[j]
            if t['depth'] <= s['depth']:
                break
            if t['kind'] == 'function' and 'function' not in row:
                row['function'] = t['name']
                row['functionMs'] = t['durationMs']
                for k in range(j + 1, len(spans)):
                    u = spans[k]
                    if u['depth'] <= t['depth']:
                        break
                    if u['parent'] == j and u['kind'] in ('init', 'invocation', 'overhead'):
                        row[u['kind'] + 'Ms'] = u['durationMs']
        row['coldStart'] = 'initMs' in row
        # Failed on its own account: its API span, or the function behind it, faulted
        # (not a request it made itself - that one has its own row).
        own = [i] + [k for k in _subtree(spans, i) if k != i and spans[k]['kind'] != 'api'
                     and not _under_api(spans, k, i)]
        if (s.get('status') or 0) >= 500 or any(spans[k].get('fault') or spans[k].get('error')
                                                for k in own):
            row['failed'] = True
        out.append(row)
    return out


def _under_api(spans, k, top):
    """Is span k inside another API request nested below span top?"""
    p = spans[k]['parent']
    while p is not None and p != top:
        if spans[p]['kind'] == 'api':
            return True
        p = spans[p]['parent']
    return False


def _subtree(spans, i):
    yield i
    for j in range(i + 1, len(spans)):
        if spans[j]['depth'] <= spans[i]['depth']:
            return
        yield j


def _clues(requests, spans):
    """A few plain sentences about where the time went."""
    out = []
    timed = [r for r in requests if r.get('durationMs') is not None]
    if not timed:
        return out
    slow = sorted(timed, key=lambda r: r['durationMs'], reverse=True)
    top = slow[0]
    out.append('Slowest request: %s %s, %.0f ms%s.' % (
        top.get('method') or '', top.get('path') or '?', top['durationMs'],
        ' (cold start: %.0f ms initialising)' % top['initMs'] if top.get('coldStart') else ''))
    cold = [r for r in timed if r.get('coldStart')]
    if cold:
        out.append('%d of %d requests started a new Lambda instance (cold start), %.0f ms of '
                   'initialising in all.' % (len(cold), len(timed), sum(r['initMs'] or 0 for r in cold)))
    failed = [r for r in requests if r.get('failed') or (r.get('status') or 0) >= 500]
    if failed:
        out.append('%d request(s) failed: %s.' % (len(failed), ', '.join(
            '%s %s -> %s' % (r.get('method') or '', r.get('path') or '?', r.get('status') or 'fault')
            for r in failed[:5])))
    outside = []
    for r in timed:
        if r.get('functionMs') is not None and r['durationMs']:
            outside.append(r['durationMs'] - r['functionMs'])
    if outside:
        out.append('API Gateway and Lambda start-up outside the functions: %.0f ms median per '
                   'request.' % sorted(outside)[len(outside) // 2])
    root = spans[0]
    inner = [r for r in timed if r['span'] != 0] if root['kind'] == 'api' else []
    if inner and root.get('durationMs'):
        busy = sum(r['durationMs'] for r in inner)
        what = '%s %s' % (root.get('method') or '', root.get('path') or '')
        n = len(inner)
        line = '%d request%s ran inside %s' % (n, '' if n == 1 else 's', what.strip())
        if n > 1 and busy > root['durationMs']:
            line += ('; together they took %.0f ms of a %.0f ms request, so they ran in parallel'
                     % (busy, root['durationMs']))
        out.append(line + '.')
    return out


def _links(base, ref):
    return {'page': '%s/trace/get?ref=%s&format=html' % (base, ref),
            'data': '%s/trace/get?ref=%s' % (base, ref)}


# ------------------------------------------------------------------ the action
def _ref(req):
    ref = (req.text('ref', required=True) or '').lower()
    if not TRACE_RE.match(ref):
        raise ApiError(400, "'ref' must be an X-Ray trace id: 1-<8 hex>-<24 hex>.", received=ref,
                       hint='the traceId field of a health response, e.g. '
                            '1-673d9a1b-5f2a8c3e9d1b4a6f7e8c9d0a')
    return ref


def get(req):
    ref = _ref(req)
    fmt = (req.text('format') or 'json').lower()
    if fmt not in ('json', 'html'):
        raise ApiError(400, "'format' must be json or html.", received=fmt)
    wait = req.integer('wait', default=0, lo=0, hi=WAIT_MAX_S)
    base = base_url(req.event)
    started = time.monotonic()
    while True:
        docs = fetch(ref)
        if docs is not None or time.monotonic() - started + POLL_S > wait:
            break
        time.sleep(POLL_S)
    waited = int((time.monotonic() - started) * 1000)
    age = int(time.time()) - started_time(ref)

    out = model(ref, docs, base) if docs else None
    if out is None:
        if docs is None and age < PENDING_S:
            # Written as the request finished; X-Ray takes a few seconds to have it.
            out = {'traceId': ref, 'pending': True, 'ageSeconds': max(0, age), 'waitedMs': waited,
                   'retryAfterSeconds': 5, 'links': _links(base, ref),
                   'comment': 'X-Ray is still receiving this trace: ask again in a few seconds '
                              '(or pass wait=10)'}
            return _page(out) if fmt == 'html' else respond(200, out)
        msg = ('No trace with that id from this deployment: it may be older than %d days, from a '
               'request that was not traced, or from another application.' % RETENTION_DAYS)
        if fmt == 'html':
            return raw(404, _shell('Trace not found', '<p>%s</p>' % html.escape(msg)),
                       'text/html; charset=utf-8')
        raise ApiError(404, msg, traceId=ref)
    out['waitedMs'] = waited
    return _page(out) if fmt == 'html' else respond(200, out)


# ------------------------------------------------------------------ the page
CSS = """
body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;color:#1d2433;background:#f6f7f9}
main{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:24px 0 8px}
.id{font-family:ui-monospace,Menlo,Consolas,monospace;color:#5b6474}
.stats{display:flex;gap:28px;margin:16px 0;flex-wrap:wrap}
.stats div b{display:block;font-size:11px;letter-spacing:.06em;color:#5b6474;text-transform:uppercase}
.stats div span{font-size:20px;font-weight:600}
.card{background:#fff;border:1px solid #e2e5ea;border-radius:8px;padding:14px 16px}
ul.clues{margin:0;padding-left:18px} ul.clues li{margin:3px 0}
table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #eef0f3;font-size:13px}
th{color:#5b6474;font-weight:600} td.n{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:#b42318;font-weight:600} .cold{color:#9a6700}
.wf{position:relative} .row{display:flex;align-items:center;height:22px;font-size:12px}
.lab{width:330px;flex:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lane{position:relative;flex:1;height:14px;background:#f1f3f6;border-radius:3px}
.bar{position:absolute;top:0;height:14px;border-radius:3px;min-width:2px}
.k-api{background:#3b6fd8}.k-lambda{background:#8aa6e8}.k-function{background:#2e9e6b}
.k-init{background:#d99a1e}.k-invocation{background:#58c08e}.k-overhead{background:#b9c2cf}
.k-subsegment,.k-segment{background:#9b7bd4}.f{background:#d92d20!important}
.ms{width:70px;flex:none;text-align:right;color:#5b6474;font-variant-numeric:tabular-nums}
.muted{color:#5b6474} a{color:#2856c6}
"""


def _shell(title, body, refresh=None):
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ''
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">%s'
            '<title>%s</title><style>%s</style></head><body><main>%s</main></body></html>'
            % (meta, html.escape(title), CSS, body))


def _ms(v):
    if v is None:
        return '—'
    return '%.2f s' % (v / 1000.0) if v >= 1000 else '%.0f ms' % v if v >= 10 else '%.1f ms' % v


def _page(t):
    e = html.escape
    if t.get('pending'):
        body = ('<h1>Trace</h1><p class="id">%s</p><div class="card"><p>X-Ray is still receiving '
                'this trace (it is written as the request finishes). This page refreshes every few '
                'seconds until it arrives.</p></div>' % e(t['traceId']))
        return raw(200, _shell('Trace (waiting)', body, refresh=4), 'text/html; charset=utf-8')
    total = t['durationMs'] or 1
    root = t['spans'][0] if t['spans'] else {}
    title = ' '.join(x for x in (root.get('method'), root.get('path')) if x) or 'Trace'
    when = time.strftime('%a %d %b %Y, %H:%M:%S UTC', time.gmtime(t['startedTime']))
    parts = ['<h1>%s</h1><p class="id">%s</p>' % (e(title), e(t['traceId'])),
             '<div class="stats"><div><b>Started</b><span>%s</span></div>'
             '<div><b>Total</b><span>%s</span></div><div><b>Requests</b><span>%d</span></div>'
             '<div><b>Spans</b><span>%d</span></div></div>'
             % (e(when), _ms(t['durationMs']), t['requestCount'], t['spanCount'])]
    if not t.get('settled'):
        parts.append('<p class="muted">Segments may still be arriving: this page refreshes until '
                     'the trace is %d s old.</p>' % SETTLE_S)
    if t['clues']:
        parts.append('<h2>What stands out</h2><div class="card"><ul class="clues">%s</ul></div>'
                     % ''.join('<li>%s</li>' % e(c) for c in t['clues']))
    if t['requests']:
        rows = []
        for r in t['requests']:
            status = r.get('status')
            cls = ' class="bad"' if (r.get('failed') or (status or 0) >= 500) else ''
            rows.append('<tr><td class="n">%d</td><td>%s</td><td>%s</td><td%s>%s</td>'
                        '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td><td>%s</td></tr>' % (
                            r['index'], e(r.get('method') or ''), e(r.get('path') or ''), cls,
                            e(str(status if status is not None else '—')), _ms(r['startMs']),
                            _ms(r['durationMs']), _ms(r.get('functionMs')),
                            '<span class="cold">cold start %s</span>' % _ms(r['initMs'])
                            if r.get('coldStart') else ''))
        parts.append('<h2>Requests</h2><div class="card"><table><tr><th>#</th><th>Method</th>'
                     '<th>Path</th><th>Status</th><th>Starts at</th><th>Took</th>'
                     '<th>In function</th><th></th></tr>%s</table></div>' % ''.join(rows))
    bars = []
    for s in t['spans']:
        left = 100.0 * s['startMs'] / total
        width = 100.0 * (s['durationMs'] or 0) / total
        label = s['name'] if s['kind'] != 'api' else '%s %s' % (s.get('method') or '', s.get('path') or s['name'])
        if s.get('operation'):
            label += ' · ' + s['operation']
        bad = ' f' if s.get('fault') or s.get('error') else ''
        bars.append('<div class="row"><div class="lab" style="padding-left:%dpx" title="%s">%s</div>'
                    '<div class="lane"><div class="bar k-%s%s" style="left:%.3f%%;width:%.3f%%"></div>'
                    '</div><div class="ms">%s</div></div>' % (
                        s['depth'] * 14, e(label), e(label), e(s['kind']), bad,
                        min(left, 100), min(width, 100 - min(left, 100)), _ms(s['durationMs'])))
    parts.append('<h2>Every step</h2><div class="card wf">%s</div>' % ''.join(bars))
    parts.append('<p class="muted">Blue: API Gateway · green: the function (light green its '
                 'handler, amber initialising a new instance, grey after the response) · red: a '
                 'fault. Only this deployment\'s segments are shown, and only paths, methods, '
                 'statuses, names and timings. <a href="%s">This trace as JSON</a>.</p>'
                 % e(t['links']['data']))
    return raw(200, _shell(title + ' — trace', ''.join(parts),
                           refresh=None if t.get('settled') else 5), 'text/html; charset=utf-8')


ROUTES = {'get': get}


def handler(event, context):
    return run(event, 'trace', ROUTES, index={
        'description': 'The AWS X-Ray trace of a request to this API, cleaned for the public: '
                       'where its time went, as JSON or a page.'})
