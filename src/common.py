"""Shared request/response helpers for every Python v1 service.

Every service Lambda sits behind an API Gateway proxy resource
(/v1/<service>/{proxy+}), so the action arrives as pathParameters.proxy.
Handlers call parse(event) once and route on req.action.
"""
import base64
import json
import re
from decimal import Decimal

# Universal namespace/item key rule (docs/CONVENTIONS.md section 3).
# '#' is deliberately excluded: storage layers use it as an internal delimiter.
KEY_RE = re.compile(r'^[A-Za-z0-9+][A-Za-z0-9._:@+=-]{0,127}$')
KEY_RULE = ("1-128 characters: letters, digits and . _ - : @ + =, starting with "
            "a letter, digit or + (send + as %2B in a query string)")

TRUE = ('1', 'true', 'yes', 'on', 'y', 't')
FALSE = ('0', 'false', 'no', 'off', 'n', 'f', '')

CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS,HEAD',
    'Access-Control-Allow-Headers': '*',
    'Access-Control-Expose-Headers': '*',
}


# ------------------------------------------------------------------ build stamp
# deploy.ps1 (or Terraform) stamps every function with the same BUILD_ID,
# BUILD_COMMIT and DEPLOYED_TIME, so any response can be tied to the exact
# deployment that produced it (X-Build header) and health/check can spot a
# function left on an older build. The contract version lives in version.json.
_build = None


def build_info():
    """{'version', 'build', 'commit', 'deployedTime'} of this function's deployment."""
    global _build
    if _build is None:
        import os
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            version = json.load(open(os.path.join(here, 'version.json'), encoding='utf-8'))['version']
        except Exception:     # noqa: BLE001 - a missing file must not break every response
            version = None
        try:
            deployed = int(os.environ.get('DEPLOYED_TIME') or 0) or None
        except ValueError:
            deployed = None
        _build = {'version': version,
                  'build': os.environ.get('BUILD_ID') or 'dev',
                  'commit': os.environ.get('BUILD_COMMIT') or None,
                  'deployedTime': deployed}
    return dict(_build)


def build_headers():
    return {'X-Build': build_info()['build']}


class ApiError(Exception):
    """Raise anywhere inside a handler to return a uniform error body."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class Req:
    __slots__ = ('event', 'method', 'service', 'action', 'qs', 'headers',
                 'body', 'is_base64', 'source_ip', 'path')

    def get(self, name, default=None):
        v = self.qs.get(name)
        return default if v is None else v

    def text(self, name, default=None, required=False):
        """A trimmed string parameter."""
        v = self.qs.get(name)
        if v is None or str(v).strip() == '':
            if required:
                raise ApiError(400, f"Missing required query parameter '{name}'.")
            return default
        return str(v).strip()

    def text_or64(self, name, required=False):
        """name=... or name64=<base64>. Returns (value, used_b64) or (None, False)."""
        raw64 = self.qs.get(name + '64')
        if raw64 not in (None, ''):
            try:
                return b64decode(raw64).decode('utf-8'), True
            except UnicodeDecodeError:
                raise ApiError(400, f"'{name}64' does not decode to UTF-8 text.")
        v = self.qs.get(name)
        if v not in (None, ''):
            return v, False
        if required:
            raise ApiError(400, f"Missing required query parameter '{name}' (or '{name}64').")
        return None, False

    def key(self, name='key', required=True):
        v = self.text(name, required=required)
        if v is None:
            return None
        if not KEY_RE.match(v):
            raise ApiError(400, f"'{name}' must be {KEY_RULE}.", received=v)
        return v

    def boolean(self, name, default=False):
        v = self.qs.get(name)
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in TRUE:
            return True
        if s in FALSE:
            return False if s else default
        raise ApiError(400, f"'{name}' must be true or false.", received=v)

    def integer(self, name, default=None, lo=None, hi=None, clamp=True):
        v = self.qs.get(name)
        if v is None or str(v).strip() == '':
            return default
        try:
            n = int(float(str(v).strip()))
        except ValueError:
            raise ApiError(400, f"'{name}' must be a whole number.", received=v)
        if lo is not None and n < lo:
            if not clamp:
                raise ApiError(400, f"'{name}' must be at least {lo}.", received=v)
            n = lo
        if hi is not None and n > hi:
            if not clamp:
                raise ApiError(400, f"'{name}' must be at most {hi}.", received=v)
            n = hi
        return n

    def number(self, name, default=None):
        v = self.qs.get(name)
        if v is None or str(v).strip() == '':
            return default
        try:
            return float(str(v).strip())
        except ValueError:
            raise ApiError(400, f"'{name}' must be a number.", received=v)

    def body_bytes(self):
        b = self.body or ''
        if self.is_base64:
            return base64.b64decode(b)
        return b.encode('utf-8') if isinstance(b, str) else (b or b'')


def parse(event, service):
    r = Req()
    r.event = event
    r.service = service
    r.method = (event.get('httpMethod') or 'GET').upper()
    proxy = ((event.get('pathParameters') or {}).get('proxy') or '')
    if not proxy:
        # A static resource (e.g. /mail/pop routed to its own small function)
        # carries no proxy parameter: take the action from the resource path.
        res = (event.get('resource') or '').strip('/').split('/')
        if len(res) > 1 and res[0].lower() == service and '{' not in res[-1]:
            proxy = '/'.join(res[1:])
    r.action = '/'.join(p for p in proxy.lower().split('/') if p)
    r.path = event.get('path') or ''
    r.qs = {}
    # Parameter NAMES are case-insensitive on every service (CONVENTIONS 3).
    for k, v in (event.get('queryStringParameters') or {}).items():
        r.qs[str(k).lower()] = v
    r.headers = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
    r.body = event.get('body')
    r.is_base64 = bool(event.get('isBase64Encoded'))
    r.source_ip = ((event.get('requestContext') or {}).get('identity') or {}).get('sourceIp')
    return r


def b64decode(s):
    """Standard or URL-safe base64, padding optional."""
    s = str(s).strip().replace(' ', '+')   # '+' decoded to space by a query string
    s += '=' * ((4 - len(s) % 4) % 4)
    try:
        if '-' in s or '_' in s:
            return base64.urlsafe_b64decode(s)
        return base64.b64decode(s, validate=False)
    except Exception:
        raise ApiError(400, 'Value is not valid base64.')


def _default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, bytes):
        return base64.b64encode(o).decode('ascii')
    return str(o)


def dumps(obj, pretty=True):
    return json.dumps(obj, default=_default, indent=2 if pretty else None,
                      ensure_ascii=False)


def respond(status, obj, headers=None):
    h = {'Content-Type': 'application/json; charset=utf-8',
         'Cache-Control': 'no-store'}
    h.update(CORS)
    h.update(build_headers())
    if headers:
        h.update(headers)
    return {'statusCode': status, 'headers': h, 'body': dumps(obj)}


def raw(status, body, content_type, headers=None, base64_body=False):
    h = {'Content-Type': content_type, 'Cache-Control': 'no-store'}
    h.update(CORS)
    h.update(build_headers())
    if headers:
        h.update(headers)
    out = {'statusCode': status, 'headers': h, 'body': body}
    if base64_body:
        out['isBase64Encoded'] = True
    return out


def error(status, message, **extra):
    body = {'error': message, 'status': status}
    body.update(extra)
    return respond(status, body)


def base_url(event):
    """https://host/v1 as the caller addressed it (for links in responses)."""
    import os
    env = os.environ.get('BASE_URL')
    if env:
        return env.rstrip('/')
    host = ((event.get('headers') or {}).get('Host')
            or (event.get('headers') or {}).get('host') or '')
    return f'https://{host}/v1' if host else '/v1'


def _contract_log(event, service, res):
    """Test-only: with CONTRACT_LOG set, append each response to that file so
    tests/test_zz_contract.py can check it against the OpenAPI document."""
    import os
    log = os.environ.get('CONTRACT_LOG')
    if not log:
        return
    proxy = ((event.get('pathParameters') or {}).get('proxy') or '')
    if not proxy:
        parts = (event.get('resource') or '').strip('/').split('/')
        proxy = '/'.join(parts[1:]) if len(parts) > 1 and '{' not in parts[-1] else ''
    with open(log, 'a', encoding='utf-8') as f:
        f.write(json.dumps({
            'path': '/' + '/'.join([service] + [p for p in proxy.lower().split('/') if p]),
            'method': (event.get('httpMethod') or 'GET').upper(),
            'status': res.get('statusCode'), 'headers': res.get('headers') or {},
            'body': None if res.get('isBase64Encoded') else res.get('body')}) + '\n')


def run(event, service, routes, index=None):
    """Route an API Gateway proxy event to routes[action](req).

    routes: {'pop': fn, 'vars/get': fn, ...}. index: optional dict returned
    for GET /v1/<service> (no action). Unknown actions list the valid ones.
    """
    res = _run(event, service, routes, index)
    _contract_log(event, service, res)
    return res


def _run(event, service, routes, index=None):
    try:
        if (event.get('httpMethod') or '').upper() == 'OPTIONS':
            return raw(204, '', 'text/plain')
        req = parse(event, service)
        if not req.action:
            body = {'service': service, 'actions': sorted(routes)}
            if index:
                body.update(index)
            return respond(200, body)
        fn = routes.get(req.action)
        if fn is None:
            return error(404, f"Unknown action '{req.action}' for service '{service}'.",
                         actions=sorted(routes))
        return fn(req)
    except ApiError as e:
        return error(e.status, e.message, **e.extra)
    except Exception as e:     # noqa: BLE001 - last-resort 500 with a message
        import traceback
        traceback.print_exc()
        return error(500, f'{type(e).__name__}: {e}')
