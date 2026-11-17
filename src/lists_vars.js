// lists vars/* - run-scoped variables: named values saved under a key
// (a base URL, a token, an ID created in one script and needed in another).
const { respond, ApiError, nowSeconds } = require('./common');
const { dynamo, TABLES } = require('./lists_store');

const MAX_NAME = 256;

function varName(req) {
  const name = req.text('name', { required: true });
  if (name.length > MAX_NAME) throw new ApiError(400, `'name' must be ${MAX_NAME} characters or fewer.`);
  return name;
}

async function doSet(req) {
  const key = req.key();
  const name = varName(req);
  // An empty value is a legitimate value, so test for presence, not content.
  const value = req.qs.value;
  if (value === undefined || value === null) {
    throw new ApiError(400, "Missing required query parameter 'value'.");
  }
  const savedTime = nowSeconds();
  await dynamo.put({ TableName: TABLES.vars, Item: { key, name, value: String(value), savedTime } });
  return respond(200, { key, name, value: String(value), savedTime });
}

const MAX_WAIT_S = 20;          // the gateway gives up at 29 s
const POLL_MS = [250, 250, 500, 500, 1000];   // then every 1000 ms

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function readVar(key, name) {
  // Strongly consistent: a script that has just seen setupReady=1 must also
  // see the namespace written before it.
  const res = await dynamo.get({ TableName: TABLES.vars, Key: { key, name }, ConsistentRead: true });
  return res.Item || null;
}

function shape(key, name, item) {
  return {
    key, name, value: item ? item.value : null,
    savedTime: item && item.savedTime !== undefined ? Number(item.savedTime) : null,
  };
}

// vars/get. Plain: the value, or 404 if never set.
// With wait and/or equals: always 200 (exists:false when not set), and with
// wait=N the request is held up to N seconds until
//   - equals given: the value equals it, or the value changes to something else
//     (so 0 -> failed is reported at once);
//   - no equals: the variable exists.
async function doGet(req) {
  const key = req.key();
  const name = varName(req);
  const raw = req.qs.equals;
  const equals = raw === undefined || raw === null ? undefined : String(raw);
  const waitGiven = req.qs.wait !== undefined && req.qs.wait !== null && String(req.qs.wait).trim() !== '';
  const wait = req.integer('wait', { dflt: 0, lo: 0, hi: MAX_WAIT_S });

  let item = await readVar(key, name);
  if (equals === undefined && !waitGiven) {
    if (!item) throw new ApiError(404, 'Variable not found.', { key, name });
    return respond(200, shape(key, name, item));
  }

  const started = Date.now();
  const deadline = started + wait * 1000;
  const startValue = item ? item.value : null;
  const done = (it) => (equals !== undefined
    ? (it !== null && it.value === equals) || (it ? it.value : null) !== startValue
    : it !== null);
  for (let i = 0; !done(item) && Date.now() < deadline; i += 1) {
    await sleep(Math.min(POLL_MS[Math.min(i, POLL_MS.length - 1)], Math.max(0, deadline - Date.now())));
    item = await readVar(key, name);
  }
  const out = { ...shape(key, name, item), exists: item !== null };
  if (equals !== undefined) {
    out.equals = equals;
    out.matched = item !== null && item.value === equals;
  }
  out.waitedMs = Date.now() - started;
  return respond(200, out);
}

async function doList(req) {
  const key = req.key();
  const variables = [];
  let lastKey;
  do {
    const page = await dynamo.query({
      TableName: TABLES.vars,
      KeyConditionExpression: '#k = :key',
      ExpressionAttributeNames: { '#k': 'key' },
      ExpressionAttributeValues: { ':key': key },
      ExclusiveStartKey: lastKey,
    });
    for (const v of page.Items || []) {
      variables.push({
        name: v.name, value: v.value,
        savedTime: v.savedTime === undefined ? null : Number(v.savedTime),
      });
    }
    lastKey = page.LastEvaluatedKey;
  } while (lastKey);
  return respond(200, { key, count: variables.length, variables });
}

exports.routes = { 'vars/set': doSet, 'vars/get': doGet, 'vars/list': doList };
