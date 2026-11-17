// lists bulk loader: upload (HTML page), upload/url (presigned S3 POST),
// import (GET with ref / POST with body), import/status - plus the
// background worker (handler 'lists_import.worker') that runs the 'import'
// and 'cleanup' jobs the API function hands it by asynchronous invoke.
//
// Flow:  file -> S3 (imports/<KEY>/<REF>.<csv|json>) -> worker parses it and
//        writes a fresh generation of sharded rows -> one metadata write swaps
//        the key over (count, nextPop=1, gen, shards) -> the previous
//        generation is deleted in the background.
//
// LOADING ALWAYS REPLACES. The new records only become visible at the swap,
// so a pop during a reload sees the old set or the new set, never a mix, and
// a failed import leaves the old set untouched.
const crypto = require('crypto');
const { respond, raw, ApiError, baseUrl, KEY_RE, nowSeconds } = require('./common');
const {
  dynamo, s3, TABLES, RESERVED_FIELDS, sleep, invokeWorker,
} = require('./lists_store');
const { renderPage } = require('./lists_page');

const env = (name) => process.env[name];
const BUCKET = () => env('IMPORT_BUCKET');
const REGION = () => env('AWS_REGION') || env('AWS_DEFAULT_REGION');

// ---- limits ----------------------------------------------------------------
// MAX_FILE_KB: the presigned-POST route (browser or script -> S3 directly).
//   Parsing holds the whole file in memory, so this is a memory/CPU policy
//   number, not a platform one. 50 MB is ~100K records of 500 bytes.
// MAX_BODY_KB: the one-call route (POST the file as the request body). The
//   body crosses API Gateway and a synchronous Lambda invoke (6 MB hard, and
//   a text/csv body arrives base64 = x4/3), so ~4 MB raw is the real ceiling.
// MAX_RECORDS: a sanity cap; the worker has 15 minutes, this needs ~2.
// MAX_RECORD_BYTES: DynamoDB items stop at 400 KB; leave room for overhead.
const MAX_FILE_KB = 51200;
const MAX_BODY_KB = 4096;
const MAX_RECORDS = 1000000;
const MAX_RECORD_BYTES = 350 * 1024;
const MAX_SKIPPED_LISTED = 100;
const PRESIGN_TTL = 900;
const MAX_WAIT_S = 25;             // under API Gateway's 29 s integration timeout
const RECORDS_PER_SHARD = 12500;   // 100K -> 8 shards, capped at 16
const MAX_SHARDS = 16;
const MAX_INFLIGHT = 64;
const STALE_MS = 16 * 60 * 1000;   // worker ceiling is 15 min; progress every 2 s
const LIMITS = { MAX_FILE_KB, MAX_BODY_KB, MAX_RECORDS, MAX_RECORD_BYTES };

const REF_RE = /^[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$/;  // 20260922T031500Z-k3x9qa
const FORMATS = { csv: { ext: 'csv', type: 'text/csv' }, json: { ext: 'json', type: 'application/json' } };
const DELIMITERS = { comma: ',', ',': ',', semicolon: ';', ';': ';', tab: '\t', '\\t': '\t', pipe: '|', '|': '|' };

const objKey = (key, ref, format) => `imports/${key}/${ref}.${FORMATS[format].ext}`;
const statusKey = (key, ref) => `imports/${key}/${ref}.status.json`;

function newRef() {
  const t = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
  const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let r = '';
  for (const b of crypto.randomBytes(6)) r += alphabet[b % 36];
  return `${t}-${r}`;
}

const q = encodeURIComponent;

function formatParam(req, { required = true } = {}) {
  const f = req.text('format');
  if (f === undefined) {
    if (required) throw new ApiError(400, "Missing required query parameter 'format' (csv or json).", { allowed: ['csv', 'json'] });
    return undefined;
  }
  const format = f.toLowerCase();
  if (!FORMATS[format]) throw new ApiError(400, "'format' must be csv or json.", { allowed: ['csv', 'json'], received: f });
  return format;
}

function refParam(req, { required = false } = {}) {
  const ref = req.text('ref', { required });
  if (ref !== undefined && !REF_RE.test(ref)) {
    throw new ApiError(400, "'ref' is not a valid import reference.", { received: ref, hint: 'e.g. 20260922T031500Z-k3x9qa' });
  }
  return ref;
}

// wait: seconds 0-25 (clamped), default 25. The booleans are accepted too:
// true/yes/on = 25, false/no/off = 0. A bare number is always seconds, so
// wait=1 means one second.
function waitParam(req) {
  const v = req.qs.wait;
  if (v === undefined || v === null || String(v).trim() === '') return MAX_WAIT_S;
  const s = String(v).trim().toLowerCase();
  if (/^[+-]?\d+(\.\d+)?$/.test(s)) return Math.max(0, Math.min(MAX_WAIT_S, Math.trunc(Number(s))));
  if (['true', 'yes', 'on', 'y', 't'].includes(s)) return MAX_WAIT_S;
  if (['false', 'no', 'off', 'n', 'f'].includes(s)) return 0;
  throw new ApiError(400, "'wait' must be a number of seconds (0-25) or true/false.", { received: v });
}

// ============================================================================
//  Presigned POST (SigV4 POST policy), signed by hand
// ----------------------------------------------------------------------------
//  @aws-sdk/s3-presigned-post is not part of the Lambda runtime's bundled SDK
//  and this code takes no third-party packages, so the policy is signed here
//  with the function's own credentials (its role needs s3:PutObject on
//  imports/*). The URL is the REGIONAL virtual-hosted endpoint: the global one
//  answers a non-us-east-1 bucket with a 307, and browsers will not follow a
//  cross-origin redirect on an upload POST.
// ============================================================================
const hmac = (k, s) => crypto.createHmac('sha256', k).update(s, 'utf8').digest();

function presignPost(objectKey, maxBytes, now = new Date()) {
  const accessKey = env('AWS_ACCESS_KEY_ID');
  const secret = env('AWS_SECRET_ACCESS_KEY');
  const token = env('AWS_SESSION_TOKEN');
  const region = REGION();
  const bucket = BUCKET();
  const amzDate = now.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
  const day = amzDate.slice(0, 8);
  const fields = {
    key: objectKey,
    success_action_status: '201',
    'x-amz-algorithm': 'AWS4-HMAC-SHA256',
    'x-amz-credential': `${accessKey}/${day}/${region}/s3/aws4_request`,
    'x-amz-date': amzDate,
  };
  if (token) fields['x-amz-security-token'] = token;
  const policy = {
    expiration: new Date(now.getTime() + PRESIGN_TTL * 1000).toISOString(),
    conditions: [
      { bucket },
      ...Object.entries(fields).map(([k, v]) => ({ [k]: v })),
      ['content-length-range', 1, maxBytes],
    ],
  };
  const b64 = Buffer.from(JSON.stringify(policy), 'utf8').toString('base64');
  let k = hmac('AWS4' + secret, day);
  k = hmac(k, region);
  k = hmac(k, 's3');
  k = hmac(k, 'aws4_request');
  fields.policy = b64;
  fields['x-amz-signature'] = crypto.createHmac('sha256', k).update(b64, 'utf8').digest('hex');
  return { url: `https://${bucket}.s3.${region}.amazonaws.com/`, fields };
}

// ============================================================================
//  API: upload (page), upload/url
// ============================================================================
function doUploadPage(req) {
  const format = formatParam(req, { required: false }) || 'csv';
  return raw(200, renderPage({ format, base: baseUrl(req.event), limits: LIMITS }), 'text/html; charset=utf-8');
}

function doUploadUrl(req) {
  const key = req.key();
  const format = formatParam(req);
  let declared = 0;
  const size = req.text('size');
  if (size !== undefined) {
    if (!/^\d+$/.test(size)) throw new ApiError(400, "'size' must be a whole number of bytes.", { received: size });
    declared = Number(size);
  }
  if (declared > MAX_FILE_KB * 1024) {
    throw new ApiError(400, `The file is larger than the ${MAX_FILE_KB} KB limit.`, { maxKb: MAX_FILE_KB, bytes: declared });
  }
  const ref = newRef();
  const ps = presignPost(objKey(key, ref, format), MAX_FILE_KB * 1024);
  const base = baseUrl(req.event);
  return respond(200, {
    key, ref, format, method: 'POST', url: ps.url, fields: ps.fields,
    maxKb: MAX_FILE_KB, expiresInSeconds: PRESIGN_TTL, expiresTime: nowSeconds() + PRESIGN_TTL,
    next: `${base}/lists/import?key=${q(key)}&format=${format}&ref=${ref}`,
    note: 'POST multipart/form-data to url: every entry of fields first, the file LAST as the part named "file". ' +
      'S3 answers 201; then GET next.',
  });
}

// ============================================================================
//  Status (a small JSON object next to the file in S3)
// ============================================================================
async function readStatus(key, ref) {
  try {
    const o = await s3.getObject({ Bucket: BUCKET(), Key: statusKey(key, ref) });
    const st = JSON.parse(await o.Body.transformToString());
    // A worker that ran out of time or memory never writes 'failed'. Progress
    // is written every 2 s, so a status silent for longer than the 15-minute
    // Lambda ceiling is dead - report it rather than 'running' forever.
    if ((st.state === 'queued' || st.state === 'running') &&
        Date.now() - (st.updatedTime || 0) * 1000 > STALE_MS) {
      st.state = 'failed';
      st.error = 'The import worker stopped without finishing (timeout or out of memory). Try a smaller file.';
      st.message = 'Nothing was changed: the key still holds whatever it held before this import.';
    }
    return st;
  } catch (e) {
    if (e.name === 'NoSuchKey' || e.$metadata?.httpStatusCode === 404) return null;
    throw e;
  }
}

async function writeStatus(st) {
  st.updatedTime = nowSeconds();
  await s3.putObject({
    Bucket: BUCKET(), Key: statusKey(st.key, st.ref),
    Body: JSON.stringify(st), ContentType: 'application/json',
  });
}

async function objectExists(k) {
  try {
    const h = await s3.headObject({ Bucket: BUCKET(), Key: k });
    return { bytes: h.ContentLength };
  } catch (e) {
    if (e.name === 'NotFound' || e.name === 'NoSuchKey' || e.$metadata?.httpStatusCode === 404) return null;
    throw e;
  }
}

// queued/running -> 202, done -> 200, failed -> 422 in the standard error
// shape (error + numeric status) with the whole import state alongside.
function statusResponse(st) {
  if (st.state === 'failed') return respond(422, { error: st.error || 'Import failed.', status: 422, ...st });
  return respond(st.state === 'queued' || st.state === 'running' ? 202 : 200, st);
}

// ============================================================================
//  API: import (body upload or a ref already in S3)
// ============================================================================
async function doImport(req) {
  const key = req.key();
  const format = formatParam(req);
  const wait = waitParam(req);

  const options = {};
  if (format === 'csv') {
    const dl = req.text('delimiter');
    if (dl !== undefined) {
      const d = DELIMITERS[dl.toLowerCase()];
      if (!d) {
        throw new ApiError(400, "'delimiter' must be comma, semicolon, tab or pipe (or leave it out to auto-detect).",
          { allowed: ['comma', 'semicolon', 'tab', 'pipe'], received: dl });
      }
      options.delimiter = d;
    }
  }

  let ref = refParam(req);
  let bytes;
  let source;
  const base = baseUrl(req.event);

  if (req.method === 'POST' && req.event.body) {
    if (ref) throw new ApiError(400, 'Send the file as the request body OR pass ref, not both.');
    const buf = req.bodyBuffer();
    if (buf.length > MAX_BODY_KB * 1024) {
      throw new ApiError(413,
        `Body is ${Math.ceil(buf.length / 1024)} KB; the one-call route takes up to ${MAX_BODY_KB} KB. ` +
        `For bigger files GET ${base}/lists/upload/url?key=${q(key)}&format=${format}, POST the file to S3, then ` +
        `GET ${base}/lists/import?key=${q(key)}&format=${format}&ref=REF. The upload page does all of this for you.`,
        { maxKb: MAX_BODY_KB });
    }
    ref = newRef();
    await s3.putObject({
      Bucket: BUCKET(), Key: objKey(key, ref, format), Body: buf, ContentType: FORMATS[format].type,
    });
    bytes = buf.length;
    source = 'body';
  } else if (ref) {
    const ex = await objectExists(objKey(key, ref, format));
    if (!ex) {
      throw new ApiError(404, `No ${format} upload '${ref}' for key '${key}'. ` +
        'Refs belong to one key and one format, and uploaded files expire after 90 days.', { key, ref, format });
    }
    const prev = await readStatus(key, ref);
    if (prev && (prev.state === 'queued' || prev.state === 'running')) {
      throw new ApiError(409, 'This upload is already being imported; poll lists/import/status.', prev);
    }
    bytes = ex.bytes;
    source = prev ? 'reload' : 'upload';
  } else {
    throw new ApiError(400, 'Nothing to import. POST the file as the request body, or pass ref from lists/upload/url.',
      { hint: `Humans: open ${base}/lists/upload?format=${format} in a browser.` });
  }

  const st = {
    key, ref, format, state: 'queued', source, fileBytes: bytes, options, queuedTime: nowSeconds(),
  };
  await writeStatus(st);
  await invokeWorker({ job: 'import', key, ref, format, options, base });

  if (!wait) return statusResponse(st);
  const deadline = Date.now() + wait * 1000;
  let cur = st;
  while (Date.now() < deadline) {
    await sleep(Math.min(500, Math.max(0, deadline - Date.now())));
    cur = (await readStatus(key, ref)) || cur;
    if (cur.state === 'done' || cur.state === 'failed') break;
  }
  return statusResponse(cur);
}

async function doImportStatus(req) {
  const key = req.key();
  let ref = refParam(req);
  const recent = [];
  if (!ref) {
    // No ref: report the newest import for the key, and list recent refs
    // (handy for reloading an earlier file after a lists/clear).
    let token;
    const refs = new Set();
    do {
      const page = await s3.listObjectsV2({ Bucket: BUCKET(), Prefix: `imports/${key}/`, ContinuationToken: token });
      for (const o of page.Contents || []) {
        const m = /\/([0-9]{8}T[0-9]{6}Z-[a-z0-9]{6})\.(csv|json)$/.exec(o.Key);
        if (m) refs.add(`${m[1]}|${m[2]}|${o.Size}`);
      }
      token = page.NextContinuationToken;
    } while (token);
    const sorted = [...refs].sort().reverse();
    if (!sorted.length) throw new ApiError(404, 'No uploads for this key.', { key });
    for (const r of sorted.slice(0, 20)) {
      const [rref, fmt, size] = r.split('|');
      recent.push({ ref: rref, format: fmt, fileBytes: Number(size) });
    }
    ref = recent[0].ref;
  }
  const st = await readStatus(key, ref);
  if (!st) {
    let r = recent.find((x) => x.ref === ref);
    if (!r) {
      for (const fmt of Object.keys(FORMATS)) {
        const ex = await objectExists(objKey(key, ref, fmt));
        if (ex) { r = { ref, format: fmt, fileBytes: ex.bytes }; break; }
      }
    }
    if (!r) throw new ApiError(404, `No upload '${ref}' for key '${key}'.`, { key, ref });
    const out = {
      key, ref, state: 'uploaded', format: r.format, fileBytes: r.fileBytes,
      message: 'Uploaded but not imported yet.',
      next: `${baseUrl(req.event)}/lists/import?key=${q(key)}&format=${r.format}&ref=${ref}`,
    };
    if (recent.length) out.recent = recent;
    return respond(200, out);
  }
  if (recent.length) st.recent = recent;
  return statusResponse(st);
}

// ============================================================================
//  Decoding
// ============================================================================
// Windows-1252 differs from Latin-1 only in 0x80-0x9F (smart quotes, euro,
// dashes...). Excel's plain "CSV" save on Windows writes this, not UTF-8.
const CP1252 = {
  0x80: '€', 0x82: '‚', 0x83: 'ƒ', 0x84: '„', 0x85: '…', 0x86: '†',
  0x87: '‡', 0x88: 'ˆ', 0x89: '‰', 0x8A: 'Š', 0x8B: '‹', 0x8C: 'Œ',
  0x8E: 'Ž', 0x91: '‘', 0x92: '’', 0x93: '“', 0x94: '”', 0x95: '•',
  0x96: '–', 0x97: '—', 0x98: '˜', 0x99: '™', 0x9A: 'š', 0x9B: '›',
  0x9C: 'œ', 0x9E: 'ž', 0x9F: 'Ÿ',
};

class ImportError extends Error {}

function decode(buf, allowLegacy) {
  if (buf[0] === 0xEF && buf[1] === 0xBB && buf[2] === 0xBF) {
    return { text: buf.subarray(3).toString('utf8'), encoding: 'utf-8 (BOM)' };
  }
  // UTF-16 with BOM: Windows PowerShell 5.1's Out-File / > default, and
  // Excel's "Unicode Text" save.
  if (buf[0] === 0xFF && buf[1] === 0xFE) {
    return { text: buf.subarray(2).toString('utf16le'), encoding: 'utf-16le' };
  }
  if (buf[0] === 0xFE && buf[1] === 0xFF) {
    const b = Buffer.from(buf.subarray(2, 2 + ((buf.length - 2) & ~1)));
    return { text: b.swap16().toString('utf16le'), encoding: 'utf-16be' };
  }
  try {
    return { text: new TextDecoder('utf-8', { fatal: true }).decode(buf), encoding: 'utf-8' };
  } catch (_) {
    if (!allowLegacy) {
      throw new ImportError('The file is not valid UTF-8. JSON must be UTF-8 (or UTF-16 with a byte-order mark).');
    }
    const text = buf.toString('latin1').replace(/[\x80-\x9F]/g, (c) => CP1252[c.charCodeAt(0)] || c);
    return { text, encoding: 'windows-1252' };
  }
}

// A free variant of 'base' not in 'taken': base_2, base_3, ...
function freeName(base, taken) {
  let n = 2;
  while (taken.has(`${base}_${n}`)) n++;
  return `${base}_${n}`;
}

// ============================================================================
//  CSV
// ----------------------------------------------------------------------------
//  RFC 4180 plus the usual real-world deviations: CRLF / LF / CR line ends,
//  quoted fields holding delimiters, "" escapes and line breaks, a stray
//  character after a closing quote (kept, not fatal), a trailing delimiter.
//  An unterminated quote IS fatal - everything after it would be swallowed
//  into one field, and loading that silently is worse than refusing.
// ============================================================================
function detectDelimiter(text) {
  // Count candidates on the header line, outside quotes.
  const counts = { ',': 0, ';': 0, '\t': 0, '|': 0 };
  let inQ = false;
  for (let i = 0; i < text.length && i < 65536; i++) {
    const c = text[i];
    if (c === '"') inQ = !inQ;
    else if (!inQ && (c === '\n' || c === '\r')) break;
    else if (!inQ && c in counts) counts[c]++;
  }
  let best = ',';
  for (const d of [';', '\t', '|']) if (counts[d] > counts[best]) best = d;
  return best;
}

function* csvRows(text, delim) {
  const D = delim.charCodeAt(0);
  const n = text.length;
  let i = 0;
  let line = 1;
  while (i < n) {
    const startLine = line;
    const fields = [];
    let stray = false;
    for (;;) {
      let field;
      if (text.charCodeAt(i) === 34) {                    // quoted field
        i++;
        let buf = '';
        for (;;) {
          const qi = text.indexOf('"', i);
          if (qi === -1) {
            throw new ImportError(`Line ${startLine}: a quoted field is never closed. Every opening " needs a closing ", ` +
              'and a " inside a quoted value must be doubled ("").');
          }
          const chunk = text.slice(i, qi);
          for (let j = chunk.indexOf('\n'); j !== -1; j = chunk.indexOf('\n', j + 1)) line++;
          buf += chunk;
          if (text.charCodeAt(qi + 1) === 34) { buf += '"'; i = qi + 2; continue; }
          i = qi + 1;
          break;
        }
        const s = i;
        while (i < n) { const c = text.charCodeAt(i); if (c === D || c === 10 || c === 13) break; i++; }
        if (i > s) { buf += text.slice(s, i); stray = true; }
        field = buf;
      } else {
        const s = i;
        while (i < n) { const c = text.charCodeAt(i); if (c === D || c === 10 || c === 13) break; i++; }
        field = text.slice(s, i);
      }
      fields.push(field);
      if (i >= n) break;
      const c = text.charCodeAt(i);
      if (c === D) {
        i++;
        if (i >= n) { fields.push(''); break; }
        continue;
      }
      if (c === 13) { i++; if (text.charCodeAt(i) === 10) i++; } else { i++; }
      line++;
      break;
    }
    yield { fields, line: startLine, stray };
  }
}

function dedupeHeader(fields) {
  const out = [];
  const seen = new Map();
  out.renamed = [];
  fields.forEach((rawName, idx) => {
    let name = rawName.trim();
    if (!name) { name = `col${idx + 1}`; out.renamed.push(`column ${idx + 1} (blank) -> ${name}`); }
    const base = name;
    let n = seen.get(base) || 0;
    if (n) {
      do { n++; name = `${base}_${n}`; } while (seen.has(name));
      out.renamed.push(`duplicate "${base}" -> ${name}`);
    }
    seen.set(base, n || 1);
    seen.set(name, 1);
    out.push(name);
  });
  // key, index and createdTime are added to every record the API returns, so
  // a column of the same name would be hidden: rename it.
  const taken = new Set(out);
  out.forEach((name, i) => {
    if (RESERVED_FIELDS.includes(name)) {
      const nn = freeName(name, taken);
      taken.add(nn);
      out[i] = nn;
      out.renamed.push(`"${name}" -> ${nn} (${name} is reserved: every record already has it)`);
    }
  });
  return out;
}

function parseCsv(buf, opts) {
  const { text, encoding } = decode(buf, true);
  const delimiter = opts.delimiter || detectDelimiter(text);
  const records = [];
  const skipped = [];
  const warnings = [];
  let header = null;
  let blank = 0;
  let padded = 0;
  let strayRows = 0;
  let skippedTotal = 0;
  const skip = (line, reason) => {
    skippedTotal++;
    if (skipped.length < MAX_SKIPPED_LISTED) skipped.push({ line, reason });
  };

  for (const row of csvRows(text, delimiter)) {
    const f = row.fields;
    const empty = f.every((v) => v === '');
    if (!header) {
      if (empty) continue;
      header = dedupeHeader(f);
      continue;
    }
    if (empty) { blank++; continue; }
    if (f.length > header.length) {
      if (f.slice(header.length).some((v) => v !== '')) {
        skip(row.line, `${f.length} fields but the header has ${header.length}; the extra values would be lost ` +
          '(an unquoted delimiter inside a value?)');
        continue;
      }
    } else if (f.length < header.length) {
      padded++;
    }
    if (row.stray) strayRows++;
    const rec = {};
    for (let c = 0; c < header.length; c++) rec[header[c]] = c < f.length ? f[c] : '';
    const bad = checkRecord(rec);
    if (bad) { skip(row.line, bad); continue; }
    records.push(rec);
    if (records.length > MAX_RECORDS) throw new ImportError(`More than ${MAX_RECORDS} records; split the file.`);
  }
  if (!header) throw new ImportError('The file is empty: no header row found.');
  if (padded) warnings.push(`${padded} row(s) had fewer fields than the header; the missing trailing values were loaded as empty strings.`);
  if (strayRows) warnings.push(`${strayRows} row(s) had text straight after a closing quote (e.g. "abc"def); it was kept as part of the value.`);
  if (header.renamed.length) warnings.push(`Header names changed: ${header.renamed.join('; ')}.`);
  if (delimiter !== ',' && !opts.delimiter) warnings.push(`Delimiter auto-detected as ${JSON.stringify(delimiter)}.`);
  if (header.length === 1 && records.length) warnings.push('Only one column was found. If the file uses a different delimiter, pass delimiter=semicolon|tab|pipe.');
  return {
    records, skipped, skippedTotal, warnings, blankRows: blank,
    info: { encoding, delimiter: delimiter === '\t' ? 'tab' : delimiter, columns: [...header] },
  };
}

// ============================================================================
//  JSON
// ----------------------------------------------------------------------------
//  Accepts: an array of objects; JSON Lines (one object per line); an object
//  with exactly one array-of-objects property ({"records":[...]} API dumps);
//  or a single object (PowerShell's ConvertTo-Json emits one of those for a
//  one-element array). Scalars become {"value": x}.
//
//  Integers too big for a JS number (19-digit IDs) are kept EXACTLY, as
//  strings, using JSON.parse's source-text access - otherwise the last digits
//  would be silently rounded before anything could check them.
// ============================================================================
let bigInts = 0;
function reviver(k, v, ctx) {
  if (typeof v === 'number' && !Number.isSafeInteger(v) && ctx && typeof ctx.source === 'string' &&
      /^-?\d+$/.test(ctx.source)) {
    bigInts++;
    return ctx.source;
  }
  return v;
}

function parseJsonText(t) {
  return JSON.parse(t, reviver);
}

function jsonErr(e, text, offsetLine = 0) {
  const m = /position (\d+)/.exec(e.message) || [];
  let where = '';
  if (m[1] !== undefined) {
    const pos = Number(m[1]);
    const before = text.slice(0, pos);
    const ln = (before.match(/\n/g) || []).length + 1 + offsetLine;
    const col = pos - before.lastIndexOf('\n');
    where = ` at line ${ln}, column ${col}`;
  }
  return new ImportError(`Invalid JSON${where}: ${e.message.replace(/ in JSON at position \d+.*$/, '')}`);
}

function parseJson(buf) {
  const { text, encoding } = decode(buf, false);
  bigInts = 0;
  const trimmed = text.replace(/^\s+/, '');
  if (!trimmed) throw new ImportError('The file is empty.');
  let items;
  let shape;
  const warnings = [];
  const skipped = [];
  let skippedTotal = 0;
  const skip = (at, reason) => {
    skippedTotal++;
    if (skipped.length < MAX_SKIPPED_LISTED) skipped.push({ ...at, reason });
  };

  if (trimmed[0] === '[') {
    try { items = parseJsonText(text); } catch (e) { throw jsonErr(e, text); }
    shape = 'array';
  } else if (trimmed[0] === '{') {
    let whole;
    let ok = true;
    try { whole = parseJsonText(text); } catch (_) { ok = false; }
    if (ok) {
      const arrays = Object.keys(whole).filter((k) => Array.isArray(whole[k]) &&
        whole[k].length && whole[k].every((x) => x && typeof x === 'object' && !Array.isArray(x)));
      if (arrays.length === 1) {
        items = whole[arrays[0]];
        shape = `object.${arrays[0]}`;
        const others = Object.keys(whole).filter((k) => k !== arrays[0]);
        if (others.length) warnings.push(`Loaded the "${arrays[0]}" array; other top-level properties were ignored: ${others.slice(0, 10).join(', ')}.`);
      } else {
        items = [whole];
        shape = 'single object';
        warnings.push('The file is a single JSON object, so it was loaded as ONE record. For many records use an array [ {...}, {...} ] or JSON Lines.');
      }
    } else {
      // JSON Lines: one value per line.
      items = [];
      shape = 'json lines';
      const lines = text.split(/\r?\n/);
      for (let i = 0; i < lines.length; i++) {
        const l = lines[i].trim();
        if (!l) continue;
        try { items.push({ __line: i + 1, v: parseJsonText(l) }); } catch (e) {
          if (items.length === 0 && i < 3) {
            // Not JSON Lines after all: report the whole-document error.
            try { parseJsonText(text); } catch (e2) { throw jsonErr(e2, text); }
          }
          skip({ line: i + 1 }, jsonErr(e, l, i).message);
        }
        if (items.length > MAX_RECORDS) throw new ImportError(`More than ${MAX_RECORDS} records; split the file.`);
      }
    }
  } else {
    throw new ImportError('A JSON file must start with [ (an array of records) or { (an object, or JSON Lines).');
  }
  if (!Array.isArray(items)) throw new ImportError('Expected an array of records.');
  if (items.length > MAX_RECORDS) throw new ImportError(`More than ${MAX_RECORDS} records; split the file.`);

  const records = [];
  let wrapped = 0;
  const renamed = new Map();   // "key -> key_2" -> number of records
  const columns = new Set();
  items.forEach((rawItem, idx) => {
    let at = { item: idx + 1 };
    let v = rawItem;
    if (shape === 'json lines') { at = { line: rawItem.__line }; v = rawItem.v; }
    if (v === null || typeof v !== 'object' || Array.isArray(v)) { v = { value: v }; wrapped++; }
    // key, index and createdTime are added to every record the API returns,
    // so a top-level property of the same name would be hidden: rename it.
    if (RESERVED_FIELDS.some((r) => Object.prototype.hasOwnProperty.call(v, r))) {
      const taken = new Set(Object.keys(v));
      const out = {};
      for (const [k, val] of Object.entries(v)) {
        if (RESERVED_FIELDS.includes(k)) {
          const nn = freeName(k, taken);
          taken.add(nn);
          out[nn] = val;
          const label = `"${k}" -> ${nn}`;
          renamed.set(label, (renamed.get(label) || 0) + 1);
        } else {
          out[k] = val;
        }
      }
      v = out;
    }
    const bad = checkRecord(v);
    if (bad) { skip(at, bad); return; }
    if (columns.size < 200) for (const k of Object.keys(v)) columns.add(k);
    records.push(v);
  });
  if (wrapped) warnings.push(`${wrapped} item(s) were not objects and were stored as {"value": ...}.`);
  if (bigInts) warnings.push(`${bigInts} integer(s) too large for a JavaScript number were kept exactly as strings.`);
  if (renamed.size) {
    warnings.push(`Reserved property names renamed (every record already has ${RESERVED_FIELDS.join(', ')}): ` +
      [...renamed].map(([l, n]) => `${l} in ${n} record(s)`).join('; ') + '.');
  }
  return { records, skipped, skippedTotal, warnings, info: { encoding, shape, columns: [...columns] } };
}

// ============================================================================
//  Per-record checks: what DynamoDB would reject, caught before writing, so a
//  bad record is skipped (and reported) instead of failing a whole batch.
// ============================================================================
function checkRecord(rec) {
  const bytes = Buffer.byteLength(JSON.stringify(rec), 'utf8');
  if (bytes > MAX_RECORD_BYTES) return `record is ${Math.ceil(bytes / 1024)} KB; the limit is ${MAX_RECORD_BYTES / 1024} KB`;
  return walk(rec, 1);
}

function walk(v, depth) {
  if (depth > 30) return 'nested more than 30 levels deep (DynamoDB allows 32)';
  if (typeof v === 'number') {
    const a = Math.abs(v);
    if (a !== 0 && (a >= 1e126 || a < 1e-130)) return `number ${v} is outside the range DynamoDB can store (1e-130 to 1e125) - quote it as a string`;
    return null;
  }
  if (Array.isArray(v)) {
    for (const x of v) { const r = walk(x, depth + 1); if (r) return r; }
    return null;
  }
  if (v && typeof v === 'object') {
    for (const k of Object.keys(v)) {
      if (k === '') return 'has a property with an empty name, which DynamoDB cannot store';
      const r = walk(v[k], depth + 1);
      if (r) return r;
    }
  }
  return null;
}

// ============================================================================
//  Worker entry point (handler 'lists_import.worker')
// ============================================================================
exports.worker = async (event) => {
  if (!event || typeof event !== 'object') return;
  if (event.job === 'import') return runImport(event);
  if (event.job === 'cleanup') return runCleanup(event);
  console.error('lists worker: unknown job', JSON.stringify(event).slice(0, 500));
};

async function runImport(ev) {
  const { key, ref, format } = ev;
  if (!KEY_RE.test(key || '') || !REF_RE.test(ref || '') || !FORMATS[format]) return;
  const options = ev.options || {};
  const base = ev.base || '';
  const t0 = Date.now();
  let st = (await readStatus(key, ref)) || { key, ref, format };
  if (st.state === 'done' || st.state === 'running') return;   // async-invoke retry: already handled
  st = { ...st, state: 'running', startedTime: nowSeconds(), phase: 'parsing' };
  delete st.error; delete st.message;
  await writeStatus(st);

  const gen = Date.now().toString(36) + crypto.randomBytes(3).toString('hex');
  let shards = 0;
  let written = 0;
  try {
    const obj = await s3.getObject({ Bucket: BUCKET(), Key: objKey(key, ref, format) });
    const buf = Buffer.from(await obj.Body.transformToByteArray());
    const parsed = format === 'csv' ? parseCsv(buf, options) : parseJson(buf);
    const recs = parsed.records;
    if (!recs.length) {
      throw new ImportError(parsed.skippedTotal
        ? `No loadable records: all ${parsed.skippedTotal} were skipped (see skipped). The key was left unchanged.`
        : 'No records found. The key was left unchanged.');
    }
    shards = Math.min(MAX_SHARDS, Math.max(1, Math.ceil(recs.length / RECORDS_PER_SHARD)));
    st = { ...st, phase: 'writing', total: recs.length, loaded: 0, shards };
    await writeStatus(st);

    const createdTime = nowSeconds();
    written = await writeAll(key, gen, shards, recs, createdTime, async (n) => {
      st.loaded = n;
      await writeStatus(st);
    });

    // The swap: one write makes the new generation live and rewinds the pointer.
    const meta = {
      key, index: 0, count: recs.length, nextPop: 1, gen, shards,
      importRef: ref, importFormat: format, importTime: createdTime, createdTime,
    };
    const put = await dynamo.put({ TableName: TABLES.records, Item: meta, ReturnValues: 'ALL_OLD' });
    const old = put.Attributes;

    const kq = q(key);
    st = {
      ...st,
      state: 'done', phase: 'done', finishedTime: nowSeconds(),
      durationMs: Date.now() - t0,
      loaded: recs.length, firstIndex: 1, lastIndex: recs.length,
      skippedCount: parsed.skippedTotal, skipped: parsed.skipped,
      warnings: parsed.warnings,
      ...parsed.info,
      blankRows: parsed.blankRows ?? 0,
      replaced: old ? { count: Number(old.count ?? 0), importRef: old.importRef || null } : null,
      // Record 1 exactly as lists/pop will return it.
      sample: { ...recs[0], key, index: 1, createdTime },
      next: {
        pop: `${base}/lists/pop?key=${kq}`,
        random: `${base}/lists/random?key=${kq}`,
        reset: `${base}/lists/reset?key=${kq}`,
        list: `${base}/lists/list?key=${kq}&limit=10`,
        reload: `${base}/lists/import?key=${kq}&format=${format}&ref=${ref}`,
      },
    };
    if (parsed.skippedTotal > parsed.skipped.length) {
      st.skippedNote = `Only the first ${parsed.skipped.length} of ${parsed.skippedTotal} skipped records are listed.`;
    }
    await writeStatus(st);

    // Out with the old - after 'done' is published, so waiting callers are not held up.
    if (old && old.shards) await deleteGeneration(key, old.gen, old.shards);
    else if (old && Number(old.count) > 0) await deletePlainRows(key);
  } catch (e) {
    if (!(e instanceof ImportError)) console.error(e);
    st = {
      ...st, state: 'failed', phase: 'failed', finishedTime: nowSeconds(),
      durationMs: Date.now() - t0,
      error: e instanceof ImportError ? e.message : `Internal error: ${e.message || e}`,
      message: 'Nothing was changed: the key still holds whatever it held before this import.',
    };
    await writeStatus(st).catch(() => {});
    if (written && shards) await deleteGeneration(key, gen, shards, 0).catch(() => {});
  }
}

// Writes every record under its shard key. Up to MAX_INFLIGHT BatchWriteItem
// calls (25 items each - the API's hard cap) run at once. Consecutive indexes
// fall in different shards, so every batch already spreads its writes across
// partitions. Throttled items come back as UnprocessedItems and are retried
// with jittered exponential backoff; throttling exceptions are retried by the
// SDK itself (maxAttempts 10).
async function writeAll(key, gen, shards, recs, createdTime, onProgress) {
  const total = recs.length;
  let next = 0;
  let done = 0;
  let lastReport = Date.now();
  const inflight = Math.min(MAX_INFLIGHT, Math.max(4, shards * 4));

  async function lane() {
    for (;;) {
      const from = next;
      if (from >= total) return;
      next = Math.min(total, from + 25);
      let reqs = [];
      for (let i = from; i < next; i++) {
        const index = i + 1;
        reqs.push({ PutRequest: { Item: { key: `${key}#${gen}#${index % shards}`, index, data: recs[i], createdTime } } });
      }
      const n = reqs.length;
      let attempt = 0;
      while (reqs.length) {
        const res = await dynamo.batchWrite({ RequestItems: { [TABLES.records]: reqs } });
        reqs = res.UnprocessedItems?.[TABLES.records] ?? [];
        if (reqs.length) {
          attempt++;
          if (attempt > 12) throw new Error('DynamoDB kept throttling this import; try again in a minute.');
          await sleep(Math.random() * Math.min(5000, 50 * 2 ** attempt));
        }
      }
      done += n;
      if (Date.now() - lastReport > 2000) {
        lastReport = Date.now();
        onProgress(done).catch(() => {});
      }
    }
  }
  await Promise.all(Array.from({ length: inflight }, lane));
  return done;
}

// ============================================================================
//  Worker: cleanup (a replaced generation, or the shards of a cleared key)
// ============================================================================
async function runCleanup(ev) {
  if (!KEY_RE.test(ev.key || '')) return;
  if (ev.gen && ev.shards) await deleteGeneration(ev.key, ev.gen, ev.shards);
}

async function deleteKeys(keys) {
  let i = 0;
  async function lane() {
    for (;;) {
      const from = i;
      if (from >= keys.length) return;
      i = Math.min(keys.length, from + 25);
      let reqs = keys.slice(from, i).map((k) => ({ DeleteRequest: { Key: k } }));
      let attempt = 0;
      while (reqs.length) {
        const res = await dynamo.batchWrite({ RequestItems: { [TABLES.records]: reqs } });
        reqs = res.UnprocessedItems?.[TABLES.records] ?? [];
        if (reqs.length) await sleep(Math.random() * Math.min(5000, 50 * 2 ** ++attempt));
      }
    }
  }
  await Promise.all(Array.from({ length: 32 }, lane));
}

async function queryKeys(pk, fromIndex) {
  const keys = [];
  let lastKey;
  do {
    const page = await dynamo.query({
      TableName: TABLES.records,
      KeyConditionExpression: '#k = :k AND #i >= :lo',
      ProjectionExpression: '#k, #i',
      ExpressionAttributeNames: { '#k': 'key', '#i': 'index' },
      ExpressionAttributeValues: { ':k': pk, ':lo': fromIndex },
      ExclusiveStartKey: lastKey,
    });
    for (const it of page.Items || []) keys.push({ key: it.key, index: it.index });
    lastKey = page.LastEvaluatedKey;
  } while (lastKey);
  return keys;
}

// A pop that read the old metadata just before the swap is still about to
// fetch its row from the old generation - give it time to land.
// CLEANUP_GRACE_MS overrides the 5 s default (tests set it to 0).
function graceMs() {
  const v = env('CLEANUP_GRACE_MS');
  return v !== undefined && v !== '' && Number(v) >= 0 ? Number(v) : 5000;
}

async function deleteGeneration(key, gen, shards, grace = graceMs()) {
  if (grace) await sleep(grace);
  const parts = await Promise.all(
    Array.from({ length: shards }, (_, s) => queryKeys(`${key}#${gen}#${s}`, 0)));
  await deleteKeys(parts.flat());
}

// A key filled by lists/push keeps its rows under the plain key. After an
// import replaces it, those rows (index >= 1) are orphans; the metadata row
// (index 0) is the live one and must stay.
async function deletePlainRows(key) {
  const grace = graceMs();
  if (grace) await sleep(grace);
  await deleteKeys(await queryKeys(key, 1));
}

exports.routes = {
  upload: doUploadPage,
  'upload/url': doUploadUrl,
  import: doImport,
  'import/status': doImportStatus,
};

// exported for tests
exports._t = { parseCsv, parseJson, presignPost, csvRows, detectDelimiter, decode, waitParam, LIMITS };
