// Shared request/response helpers for the Node.js v1 services (lists).
// Mirrors common.py so both runtimes behave identically - see
// docs/CONVENTIONS.md.

// Universal namespace/item key rule. '#' is excluded on purpose: the lists
// storage layer uses it as its shard delimiter.
const KEY_RE = /^[A-Za-z0-9+][A-Za-z0-9._:@+=-]{0,127}$/;
const KEY_RULE = '1-128 characters: letters, digits and . _ - : @ + =, starting with ' +
  'a letter, digit or + (send + as %2B in a query string)';

const TRUE = ['1', 'true', 'yes', 'on', 'y', 't'];
const FALSE = ['0', 'false', 'no', 'off', 'n', 'f', ''];

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS,HEAD',
  'Access-Control-Allow-Headers': '*',
  'Access-Control-Expose-Headers': '*',
};

class ApiError extends Error {
  constructor(status, message, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

function respond(status, obj, headers = {}) {
  return {
    statusCode: status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      ...CORS,
      ...headers,
    },
    body: JSON.stringify(obj, null, 2),
  };
}

function raw(status, body, contentType, headers = {}) {
  return {
    statusCode: status,
    headers: { 'Content-Type': contentType, 'Cache-Control': 'no-store', ...CORS, ...headers },
    body,
  };
}

function error(status, message, extra = {}) {
  return respond(status, { error: message, status, ...extra });
}

// Request wrapper: lower-cased parameter names, typed accessors.
class Req {
  constructor(event, service) {
    this.event = event;
    this.service = service;
    this.method = String(event.httpMethod || 'GET').toUpperCase();
    let proxy = (event.pathParameters && event.pathParameters.proxy) || '';
    if (!proxy) {
      // Static resource (no {proxy+}): take the action from the resource path.
      const res = String(event.resource || '').split('/').filter(Boolean);
      if (res.length > 1 && res[0].toLowerCase() === service && !res[res.length - 1].includes('{')) {
        proxy = res.slice(1).join('/');
      }
    }
    this.action = proxy.toLowerCase().split('/').filter(Boolean).join('/');
    this.qs = {};
    for (const [k, v] of Object.entries(event.queryStringParameters || {})) {
      this.qs[String(k).toLowerCase()] = v;
    }
    this.headers = {};
    for (const [k, v] of Object.entries(event.headers || {})) {
      this.headers[String(k).toLowerCase()] = v;
    }
  }

  text(name, { required = false, dflt = undefined } = {}) {
    const v = this.qs[name];
    if (v === undefined || v === null || String(v).trim() === '') {
      if (required) throw new ApiError(400, `Missing required query parameter '${name}'.`);
      return dflt;
    }
    return String(v).trim();
  }

  key(name = 'key', { required = true } = {}) {
    const v = this.text(name, { required });
    if (v === undefined) return undefined;
    if (!KEY_RE.test(v)) throw new ApiError(400, `'${name}' must be ${KEY_RULE}.`, { received: v });
    return v;
  }

  boolean(name, dflt = false) {
    const v = this.qs[name];
    if (v === undefined || v === null) return dflt;
    const s = String(v).trim().toLowerCase();
    if (TRUE.includes(s)) return true;
    if (FALSE.includes(s)) return s ? false : dflt;
    throw new ApiError(400, `'${name}' must be true or false.`, { received: v });
  }

  integer(name, { dflt = undefined, lo = undefined, hi = undefined } = {}) {
    const v = this.qs[name];
    if (v === undefined || v === null || String(v).trim() === '') return dflt;
    const n = Math.trunc(Number(String(v).trim()));
    if (!Number.isFinite(n)) throw new ApiError(400, `'${name}' must be a whole number.`, { received: v });
    if (lo !== undefined && n < lo) return lo;
    if (hi !== undefined && n > hi) return hi;
    return n;
  }

  bodyBuffer() {
    const b = this.event.body || '';
    return this.event.isBase64Encoded ? Buffer.from(b, 'base64') : Buffer.from(b, 'utf8');
  }
}

// Route an API Gateway proxy event. routes: { 'pop': fn(req), 'vars/get': fn, ... }
async function run(event, service, routes, index = {}) {
  const res = await runRoute(event, service, routes, index);
  contractLog(event, service, res);
  return res;
}

// Test-only: with CONTRACT_LOG set, append each response to that file so
// tests/test_zz_contract.py can check it against the OpenAPI document.
function contractLog(event, service, res) {
  const log = process.env.CONTRACT_LOG;
  if (!log) return;
  const proxy = String((event.pathParameters || {}).proxy || '');
  const path = '/' + [service, ...proxy.toLowerCase().split('/').filter(Boolean)].join('/');
  require('node:fs').appendFileSync(log, JSON.stringify({
    path, method: String(event.httpMethod || 'GET').toUpperCase(),
    status: res.statusCode, headers: res.headers || {},
    body: res.isBase64Encoded ? null : res.body,
  }) + '\n');
}

async function runRoute(event, service, routes, index = {}) {
  try {
    if (String(event.httpMethod || '').toUpperCase() === 'OPTIONS') return raw(204, '', 'text/plain');
    const req = new Req(event, service);
    if (!req.action) {
      return respond(200, { service, actions: Object.keys(routes).sort(), ...index });
    }
    const fn = routes[req.action];
    if (!fn) {
      return error(404, `Unknown action '${req.action}' for service '${service}'.`,
        { actions: Object.keys(routes).sort() });
    }
    return await fn(req);
  } catch (e) {
    if (e instanceof ApiError) return error(e.status, e.message, e.extra);
    console.error(e);
    return error(500, `${e && e.name ? e.name : 'Error'}: ${(e && e.message) || e}`);
  }
}

function baseUrl(event) {
  if (process.env.BASE_URL) return process.env.BASE_URL.replace(/\/+$/, '');
  const h = event.headers || {};
  const host = h.Host || h.host || '';
  return host ? `https://${host}/v1` : '/v1';
}

const nowSeconds = () => Math.floor(Date.now() / 1000);

module.exports = {
  KEY_RE, KEY_RULE, ApiError, Req, respond, raw, error, run, baseUrl, nowSeconds,
};
