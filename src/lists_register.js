// lists counters/* and cleanup/* - atomic counters and the cleanup register.
const { respond, ApiError, nowSeconds } = require('./common');
const { dynamo, TABLES } = require('./lists_store');

const NAME_RE = /^[A-Za-z0-9._-]{1,64}$/;
const INT_RE = /^[+-]?\d+$/;
const MAX_STEP = 1000000000000000;        // 1e15, well inside a safe integer
const MAX_VALUE_LEN = 1024;
const MAX_NOTE_LEN = 1024;
const LIST_CAP = 5000;

// A counter is scoped to one key, so name it the same way the rest of a
// cycle is named - the rand10 from lists/codes works well as a key.
function counterName(req) {
  const name = req.text('name', { dflt: 'default' });
  if (!NAME_RE.test(name)) {
    throw new ApiError(400, "'name' must be 1-64 characters of letters, digits, '.', '_' or '-'.", { received: name });
  }
  return name;
}

// ===================================================================
// COUNTERS
// -------------------------------------------------------------------
// DynamoDB's ADD is an atomic counter: it needs no read first, so
// concurrent virtual users cannot lose each other's increments the way a
// read-then-write would. The trade is that one counter is one item, and a
// single item accepts roughly 1,000 writes per second - fine for sequence
// numbers, not for a per-transaction tally at load. For that, lists/codes
// gives collision-resistant unique values with no coordination at all.
// ===================================================================
async function doCounterAdd(req) {
  const key = req.key();
  const name = counterName(req);
  const raw = req.text('increment', { dflt: '1' });
  if (!INT_RE.test(raw)) {
    throw new ApiError(400, "'increment' must be a whole number. Negative values are allowed; fractions are not.",
      { received: raw });
  }
  const increment = Number(raw);
  if (!Number.isSafeInteger(increment) || Math.abs(increment) > MAX_STEP) {
    throw new ApiError(400, `'increment' must be between -${MAX_STEP} and ${MAX_STEP}.`, { received: raw });
  }
  const upd = await dynamo.update({
    TableName: TABLES.counters,
    Key: { key, name },
    UpdateExpression: 'ADD #v :inc SET #u = :now',
    ExpressionAttributeNames: { '#v': 'value', '#u': 'updatedTime' },
    ExpressionAttributeValues: { ':inc': increment, ':now': nowSeconds() },
    // ALL_NEW, not UPDATED_NEW: adding zero leaves 'value' unchanged, and
    // UPDATED_NEW returns only attributes that actually changed - so an
    // increment of 0 would come back with no value at all.
    ReturnValues: 'ALL_NEW',
  });
  return respond(200, {
    key, name, increment,
    value: Number(upd.Attributes.value),
    updatedTime: Number(upd.Attributes.updatedTime),
  });
}

async function doCounterGet(req) {
  const key = req.key();
  const name = counterName(req);
  const rec = await dynamo.get({ TableName: TABLES.counters, Key: { key, name } });
  if (!rec.Item) {
    // An unused counter reads as zero rather than 404, so a script can read
    // one before anything has incremented it without special-casing.
    return respond(200, { key, name, value: 0, exists: false, updatedTime: null });
  }
  return respond(200, {
    key, name,
    value: Number(rec.Item.value ?? 0),
    exists: true,
    updatedTime: rec.Item.updatedTime === undefined ? null : Number(rec.Item.updatedTime),
  });
}

// ===================================================================
// CLEANUP REGISTER
// -------------------------------------------------------------------
// Register anything a test creates in the system under test, then walk the
// register at the end of the cycle deleting as you go. Records are handed
// back one at a time in the order they were saved, and each one is stamped
// removedTime as it is handed over - so two cleanup scripts running at once
// never receive the same record.
//
// Row 0 is metadata (count, nextClean); rows 1..count are the records. This
// is the same shape lists/push and lists/pop use, so the claim is a single
// conditional ADD rather than a scan for unremoved rows.
// ===================================================================
async function doCleanupPush(req) {
  const key = req.key();
  const value = req.qs.value;
  if (value === undefined || value === null || String(value) === '') {
    throw new ApiError(400, "Missing required query parameter 'value' - the identifier of the record " +
      'that will need removing.');
  }
  if (String(value).length > MAX_VALUE_LEN) {
    throw new ApiError(400, `'value' must be ${MAX_VALUE_LEN} characters or fewer.`);
  }

  // Duplicates are kept as separate rows on purpose: if the same identifier
  // is created twice, that is two records to clean up, not one.
  const upd = await dynamo.update({
    TableName: TABLES.cleanup,
    Key: { key, index: 0 },
    UpdateExpression: 'ADD #c :one SET #n = if_not_exists(#n, :one)',
    ExpressionAttributeNames: { '#c': 'count', '#n': 'nextClean' },
    ExpressionAttributeValues: { ':one': 1 },
    ReturnValues: 'ALL_NEW',
  });
  const index = Number(upd.Attributes.count);
  const savedTime = nowSeconds();
  const item = { key, index, value: String(value), savedTime };
  const note = req.qs.note;
  if (note !== undefined && note !== null && String(note) !== '') item.note = String(note).slice(0, MAX_NOTE_LEN);
  await dynamo.put({ TableName: TABLES.cleanup, Item: item });

  return respond(200, {
    key, index, value: item.value, note: item.note ?? null, savedTime, registered: true,
  });
}

async function doCleanupPop(req) {
  const key = req.key();
  let claimed;
  try {
    // One atomic step: move the pointer only while there is something left.
    // Whoever wins the conditional write owns that record; nobody else can
    // be handed it.
    const upd = await dynamo.update({
      TableName: TABLES.cleanup,
      Key: { key, index: 0 },
      UpdateExpression: 'ADD #n :one',
      ConditionExpression: 'attribute_exists(#n) AND #n <= #c',
      ExpressionAttributeNames: { '#n': 'nextClean', '#c': 'count' },
      ExpressionAttributeValues: { ':one': 1 },
      ReturnValues: 'UPDATED_OLD',
    });
    claimed = Number(upd.Attributes.nextClean);
  } catch (e) {
    if (e.name === 'ConditionalCheckFailedException') {
      return respond(200, {
        key, exhausted: true, index: null, value: null,
        message: 'Nothing left to clean up for this key.',
      });
    }
    throw e;
  }

  const removedTime = nowSeconds();
  // A concurrent cleanup/push bumps the count before it writes the row, so a
  // freshly claimed index can be momentarily absent. Give it a few seconds
  // before treating it as abandoned - skipping it would lose the entry.
  let rec = null;
  for (let attempt = 0; attempt < 8 && !rec; attempt++) {
    if (attempt) await new Promise((r) => setTimeout(r, 100 * attempt));
    rec = await dynamo.update({
      TableName: TABLES.cleanup,
      Key: { key, index: claimed },
      UpdateExpression: 'SET #r = :now',
      ConditionExpression: 'attribute_exists(#i)',
      ExpressionAttributeNames: { '#r': 'removedTime', '#i': 'index' },
      ExpressionAttributeValues: { ':now': removedTime },
      ReturnValues: 'ALL_NEW',
    }).catch((e) => {
      if (e.name === 'ConditionalCheckFailedException') return null;
      throw e;
    });
  }

  if (!rec) {
    // The counter was bumped but the row never landed (the push died between
    // its two writes). Skip it rather than stalling the walk - the caller
    // simply calls again.
    return respond(200, {
      key, exhausted: false, index: claimed, value: null,
      message: 'Register entry missing; call again for the next one.',
    });
  }
  const a = rec.Attributes;
  return respond(200, {
    key, exhausted: false, index: claimed,
    value: a.value, note: a.note ?? null,
    savedTime: Number(a.savedTime), removedTime,
  });
}

async function doCleanupList(req) {
  const key = req.key();
  const wanted = (req.text('status', { dflt: 'all' })).toLowerCase();
  if (!['all', 'pending', 'removed'].includes(wanted)) {
    throw new ApiError(400, "'status' must be all, pending or removed.", { allowed: ['all', 'pending', 'removed'], received: wanted });
  }

  let meta = null;
  const records = [];
  let ExclusiveStartKey;
  let truncated = false;
  do {
    const page = await dynamo.query({
      TableName: TABLES.cleanup,
      KeyConditionExpression: '#k = :k',
      ExpressionAttributeNames: { '#k': 'key' },
      ExpressionAttributeValues: { ':k': key },
      ExclusiveStartKey,
    });
    for (const it of page.Items || []) {
      if (Number(it.index) === 0) { meta = it; continue; }
      records.push({
        index: Number(it.index),
        value: it.value ?? null,
        note: it.note ?? null,
        savedTime: it.savedTime === undefined ? null : Number(it.savedTime),
        removedTime: it.removedTime === undefined ? null : Number(it.removedTime),
      });
    }
    ExclusiveStartKey = page.LastEvaluatedKey;
    if (ExclusiveStartKey && records.length >= LIST_CAP) { truncated = true; break; }
  } while (ExclusiveStartKey);

  records.sort((a, b) => a.index - b.index);
  const removed = records.filter((r) => r.removedTime !== null).length;
  const filtered = wanted === 'all' ? records
    : records.filter((r) => (wanted === 'removed') === (r.removedTime !== null));

  return respond(200, {
    key,
    saved: meta ? Number(meta.count ?? 0) : 0,
    removed,
    pending: records.length - removed,
    nextPop: meta ? Number(meta.nextClean ?? 1) : 1,
    filter: wanted,
    returned: filtered.length,
    truncated,
    records: filtered,
  });
}

exports.routes = {
  'counters/add': doCounterAdd,
  'counters/get': doCounterGet,
  'cleanup/push': doCleanupPush,
  'cleanup/pop': doCleanupPop,
  'cleanup/list': doCleanupList,
};
