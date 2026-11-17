// lists - keyed test-data queues, run variables, counters, cleanup register,
// and bulk CSV/JSON import. API entry point (handler 'lists.handler').
//
// Queue actions live here (push, pop, cyclepop, random, list, reset, clear, codes);
// vars/* in lists_vars.js, counters/* and cleanup/* in lists_register.js,
// upload/import in lists_import.js (which also exports the background
// worker, handler 'lists_import.worker').
const crypto = require('crypto');
const { run, respond, ApiError, nowSeconds } = require('./common');
const {
  dynamo, TABLES, pkFor, present, RESERVED_FIELDS, invokeWorker,
} = require('./lists_store');
const vars = require('./lists_vars');
const register = require('./lists_register');
const imp = require('./lists_import');

const routes = {
  push: doPush,
  pop: doPop,
  cyclepop: doCyclePop,
  random: doRandom,
  list: doList,
  reset: doReset,
  clear: doClear,
  codes: doCodes,
  ...vars.routes,
  ...register.routes,
  ...imp.routes,
};

exports.handler = async (event) => run(event, 'lists', routes, {
  description: 'Keyed test-data queues (push/pop/random), run variables, atomic counters, ' +
    'a cleanup register, and bulk CSV/JSON import.',
});

// --- codes: stateless random code generator -----------------------------
// Uppercase alphanumeric (36-char alphabet). Rejection sampling on
// crypto.randomBytes keeps every character exactly uniform, so codes spread
// evenly across database indexes with no skew.
const CODE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
function randString(len) {
  let out = '';
  while (out.length < len) {
    for (const b of crypto.randomBytes(len + 8)) {
      if (b < 252) {                    // 252 = 7 * 36; reject 252-255 to avoid modulo bias
        out += CODE_CHARS[b % 36];
        if (out.length === len) break;
      }
    }
  }
  return out;
}

function doCodes() {
  return respond(200, {
    rand3: randString(3),
    rand4: randString(4),
    rand6: randString(6),
    rand8: randString(8),
    rand10: randString(10),
  });
}

// --- push: append a record ----------------------------------------------
// Every query parameter other than 'key' becomes a field of the record. Field
// names are taken from the raw query string so their case is kept exactly
// (user data is returned untouched); only 'key' is matched case-insensitively.
async function doPush(req) {
  const key = req.key();
  const data = {};
  for (const [k, v] of Object.entries(req.event.queryStringParameters || {})) {
    const lk = String(k).toLowerCase();
    if (lk === 'key') continue;
    if (RESERVED_FIELDS.some((r) => r.toLowerCase() === lk)) {
      throw new ApiError(400, `'${k}' cannot be a record field: every record already has ${RESERVED_FIELDS.join(', ')}.`,
        { reserved: RESERVED_FIELDS, received: k });
    }
    data[k] = v === null || v === undefined ? '' : String(v);
  }

  // Atomically bump count on the metadata row (index 0); auto-creates the row
  // on first use and initialises nextPop to 1. ALL_NEW so an append onto an
  // imported (sharded) key lands in the right shard of the current generation.
  const upd = await dynamo.update({
    TableName: TABLES.records,
    Key: { key, index: 0 },
    UpdateExpression: 'ADD #c :one SET #n = if_not_exists(#n, :one)',
    ExpressionAttributeNames: { '#c': 'count', '#n': 'nextPop' },
    ExpressionAttributeValues: { ':one': 1 },
    ReturnValues: 'ALL_NEW',
  });
  const meta = upd.Attributes;
  const index = Number(meta.count);
  const createdTime = nowSeconds();
  const row = { key: pkFor(key, meta, index), index, data, createdTime };
  await dynamo.put({ TableName: TABLES.records, Item: row });

  return respond(200, { key, index, createdTime, count: index, record: present(key, row) });
}

// --- pop: next record, advance pointer ----------------------------------
async function doPop(req) {
  const key = req.key();
  let meta;
  try {
    const upd = await dynamo.update({
      TableName: TABLES.records,
      Key: { key, index: 0 },
      UpdateExpression: 'ADD #n :one',
      ConditionExpression: 'attribute_exists(#n) AND #n <= #c',
      ExpressionAttributeNames: { '#n': 'nextPop', '#c': 'count' },
      ExpressionAttributeValues: { ':one': 1 },
      // ALL_OLD (not UPDATED_OLD) so gen/shards come back in the same call.
      ReturnValues: 'ALL_OLD',
    });
    meta = upd.Attributes;
  } catch (e) {
    if (e.name === 'ConditionalCheckFailedException') {
      return respond(200, {
        key, exhausted: true, index: null, record: null,
        message: 'No more records to pop. Use lists/reset to start over.',
      });
    }
    throw e;
  }
  const index = Number(meta.nextPop);
  const count = Number(meta.count);
  const item = await getRecord(key, meta, index);
  const out = { key, exhausted: false, index, count, remaining: count - index, record: present(key, item) };
  if (!item) {
    out.missing = true;
    out.message = 'Record ' + index + ' was claimed but never written (its push failed). Call pop again.';
  }
  return respond(200, out);
}

// --- cyclepop: pop that starts again at record 1 instead of running out --
// Shares pop's pointer (nextPop), so reset, import, list and pop all see the
// same position. The pointer keeps counting past the end and the record
// handed out is ((n - 1) mod count) + 1, so one atomic ADD is the whole
// claim: concurrent callers always get consecutive, different records, and
// a record comes round again only after every other record has been handed out.
async function doCyclePop(req) {
  const key = req.key();
  let meta;
  try {
    const upd = await dynamo.update({
      TableName: TABLES.records,
      Key: { key, index: 0 },
      UpdateExpression: 'ADD #n :one',
      ConditionExpression: 'attribute_exists(#n) AND #c > :zero',
      ExpressionAttributeNames: { '#n': 'nextPop', '#c': 'count' },
      ExpressionAttributeValues: { ':one': 1, ':zero': 0 },
      ReturnValues: 'ALL_OLD',
    });
    meta = upd.Attributes;
  } catch (e) {
    if (e.name === 'ConditionalCheckFailedException') {
      return respond(200, {
        key, exhausted: true, empty: true, index: null, count: 0, pass: null, record: null,
        message: 'No records for this key. Push or import some first; cyclepop never runs out after that.',
      });
    }
    throw e;
  }
  const n = Math.max(1, Number(meta.nextPop));
  const count = Number(meta.count);
  const index = ((n - 1) % count) + 1;
  const pass = Math.floor((n - 1) / count) + 1;
  const item = await getRecord(key, meta, index);
  const out = {
    key, exhausted: false, index, count, pass, wrapped: pass > 1 && index === 1,
    remaining: count - index, record: present(key, item),
  };
  if (!item) {
    out.missing = true;
    out.message = 'Record ' + index + ' was claimed but never written (its push failed). Call cyclepop again.';
  }
  return respond(200, out);
}

// A push bumps the count BEFORE it writes the row, so a pop or random that
// lands between the two sees an index with no row yet. Strongly consistent
// reads, retried for a few seconds, ride that out instead of handing back an
// empty record for an index that is then consumed.
async function getRecord(key, meta, index) {
  for (let attempt = 0; attempt < 8; attempt++) {
    if (attempt) await new Promise((r) => setTimeout(r, 100 * attempt));
    const rec = await dynamo.get({
      TableName: TABLES.records, Key: { key: pkFor(key, meta, index), index }, ConsistentRead: true,
    });
    if (rec.Item) return rec.Item;
  }
  return null;
}

// --- random: a random record, pointer untouched -------------------------
async function doRandom(req) {
  const key = req.key();
  const res = await dynamo.get({ TableName: TABLES.records, Key: { key, index: 0 } });
  const meta = res.Item;
  const count = Number(meta?.count ?? 0);
  if (count < 1) {
    return respond(200, { key, empty: true, count: 0, index: null, record: null, message: 'No records for this key.' });
  }
  const index = crypto.randomInt(1, count + 1); // 1..count
  const item = await getRecord(key, meta, index);
  return respond(200, { key, empty: false, count, index, record: present(key, item) });
}

// Every item for one partition key with index in [lo, hi], all pages.
async function queryRange(pk, lo, hi) {
  const items = [];
  let lastKey;
  do {
    const page = await dynamo.query({
      TableName: TABLES.records,
      KeyConditionExpression: '#k = :k AND #i BETWEEN :lo AND :hi',
      ExpressionAttributeNames: { '#k': 'key', '#i': 'index' },
      ExpressionAttributeValues: { ':k': pk, ':lo': lo, ':hi': hi },
      ExclusiveStartKey: lastKey,
    });
    items.push(...(page.Items || []));
    lastKey = page.LastEvaluatedKey;
  } while (lastKey);
  return items;
}

// --- list: the records for the key ---------------------------------------
// A Lambda proxy response is capped at 6 MB, and an imported key can hold
// 100K+ records, so list stops at a byte budget and says where to resume:
// truncated:true + nextStart. start=N (first index, default 1) and limit=N
// (max records). A small key returns everything.
const LIST_BUDGET = 4 * 1024 * 1024;   // pretty-printed bytes; leaves headroom under 6 MB
const LIST_WINDOW = 1000;              // indexes fetched per round

async function doList(req) {
  const key = req.key();
  const start = req.integer('start', { dflt: 1, lo: 1 });
  const limit = req.integer('limit', { dflt: Infinity, lo: 1 });

  const metaRes = await dynamo.get({ TableName: TABLES.records, Key: { key, index: 0 } });
  const meta = metaRes.Item;
  const count = Number(meta?.count ?? 0);
  const nextPop = meta ? Number(meta.nextPop ?? 1) : null;
  const last = Math.min(count, start + limit - 1);

  const records = [];
  let used = 0;
  let truncated = false;
  let nextStart = null;

  for (let lo = start; lo <= last && !truncated; lo += LIST_WINDOW) {
    const hi = Math.min(last, lo + LIST_WINDOW - 1);
    let items;
    if (meta && meta.shards) {
      const parts = await Promise.all(
        Array.from({ length: meta.shards }, (_, s) => queryRange(`${key}#${meta.gen}#${s}`, lo, hi)));
      items = parts.flat().sort((a, b) => a.index - b.index);
    } else {
      items = (await queryRange(key, lo, hi)).filter((it) => Number(it.index) > 0);
    }
    for (const it of items) {
      const out = present(key, it);
      const size = JSON.stringify(out, null, 2).length + 64;
      if (used + size > LIST_BUDGET && records.length) {
        truncated = true;
        nextStart = Number(it.index);
        break;
      }
      used += size;
      records.push(out);
    }
  }
  if (!truncated && last < count) nextStart = last + 1;

  const body = {
    key, count, nextPop, start, returned: records.length,
    truncated, nextStart, importRef: (meta && meta.importRef) || null, records,
  };
  return respond(200, body);
}

// --- reset: rewind the pop pointer ---------------------------------------
async function doReset(req) {
  const key = req.key();
  try {
    const upd = await dynamo.update({
      TableName: TABLES.records,
      Key: { key, index: 0 },
      UpdateExpression: 'SET #n = :one',
      ConditionExpression: 'attribute_exists(#c)',
      ExpressionAttributeNames: { '#n': 'nextPop', '#c': 'count' },
      ExpressionAttributeValues: { ':one': 1 },
      ReturnValues: 'ALL_NEW',
    });
    return respond(200, {
      key, reset: true, count: Number(upd.Attributes.count), nextPop: 1,
      message: 'Pointer reset; the next pop returns index 1.',
    });
  } catch (e) {
    if (e.name === 'ConditionalCheckFailedException') {
      return respond(200, { key, reset: false, count: 0, nextPop: null, message: 'Nothing to reset; the key has no records.' });
    }
    throw e;
  }
}

// --- clear: delete every record for the key -------------------------------
// Rows stored under the plain key (the metadata row and any lists/push
// records) are deleted here and now. An imported key's shards can hold 100K+
// rows - too many to delete inside API Gateway's 29 s - so once the metadata
// row is gone (the key reads as empty immediately) the shard rows are handed
// to the worker to delete in the background. Variables, counters and the
// cleanup register are NOT touched.
async function doClear(req) {
  const key = req.key();
  const metaRes = await dynamo.get({ TableName: TABLES.records, Key: { key, index: 0 } });
  const meta = metaRes.Item;

  const keys = [];
  let lastKey;
  do {
    const page = await dynamo.query({
      TableName: TABLES.records,
      KeyConditionExpression: '#k = :key',
      ProjectionExpression: '#k, #i',
      ExpressionAttributeNames: { '#k': 'key', '#i': 'index' },
      ExpressionAttributeValues: { ':key': key },
      ExclusiveStartKey: lastKey,
    });
    for (const it of page.Items || []) keys.push({ key: it.key, index: it.index });
    lastKey = page.LastEvaluatedKey;
  } while (lastKey);

  // Metadata row (index 0) first, so the key is empty the moment it goes
  // even if the rest of the delete is still running.
  keys.sort((a, b) => a.index - b.index);

  let deleted = 0;
  for (let i = 0; i < keys.length; i += 25) {
    const group = keys.slice(i, i + 25);
    let un = group.map((k) => ({ DeleteRequest: { Key: { key: k.key, index: k.index } } }));
    let attempt = 0;
    while (un.length) {
      if (attempt) await new Promise((r) => setTimeout(r, Math.min(8000, 2 ** attempt * 200)));
      const res = await dynamo.batchWrite({ RequestItems: { [TABLES.records]: un } });
      un = res.UnprocessedItems?.[TABLES.records] ?? [];
      attempt++;
    }
    deleted += group.filter((k) => Number(k.index) > 0).length;
  }

  const out = {
    key, cleared: Boolean(meta), deleted, backgroundDelete: 0,
    message: meta ? 'All records for this key removed.' : 'Nothing to clear; the key has no records.',
  };
  if (meta && meta.shards) {
    await invokeWorker({ job: 'cleanup', key, gen: meta.gen, shards: meta.shards });
    out.backgroundDelete = Number(meta.count ?? 0) - deleted;
    out.message = 'Key cleared: it reads as empty now. Its imported records are being deleted in the background.';
  }
  return respond(200, out);
}

// exported for tests
exports._t = { randString, routes };
