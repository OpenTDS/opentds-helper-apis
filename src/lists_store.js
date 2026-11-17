// Shared storage plumbing for the lists service (lists.js, lists_vars.js,
// lists_register.js, lists_import.js): AWS clients, table names, the
// sharded-row layout and the background-worker invoker.
const https = require('https');
const { DynamoDB } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocument } = require('@aws-sdk/lib-dynamodb');
const { S3 } = require('@aws-sdk/client-s3');
const { Lambda } = require('@aws-sdk/client-lambda');

// 64 sockets: the import worker keeps up to 64 BatchWriteItem calls in
// flight, and the default agent would queue most of them. maxAttempts is
// raised because a big load is expected to meet throttling while DynamoDB
// scales up. Harmless for the API function.
const agent = new https.Agent({ keepAlive: true, maxSockets: 64 });
const dynamo = DynamoDBDocument.from(
  new DynamoDB({ maxAttempts: 10, requestHandler: { httpsAgent: agent } }),
  {
    marshallOptions: { removeUndefinedValues: true, allowImpreciseNumbers: true },
    // Imported JSON can hold numbers outside the safe-integer range (1e20,
    // say). The default read path turns those into BigInt, which
    // JSON.stringify cannot serialise - so read every number back as a plain
    // JS number.
    unmarshallOptions: { wrapNumbers: (s) => Number(s) },
  });
const s3 = new S3({});
const lambda = new Lambda({});

const env = (name) => process.env[name];
const TABLES = {
  get records() { return env('LISTS_TABLE'); },
  get vars() { return env('VARS_TABLE'); },
  get counters() { return env('COUNTERS_TABLE'); },
  get cleanup() { return env('CLEANUP_TABLE'); },
};

// ---- storage layout ------------------------------------------------------
// Every key has a metadata row at {key: KEY, index: 0} holding count and
// nextPop. Data rows are {key: <pk>, index: i, data: {...user fields},
// createdTime}. Where they live depends on how the key was filled:
//
//   PLAIN   (filled by lists/push; metadata has no 'shards')
//           data row i  ->  {key: KEY, index: i}
//
//   SHARDED (filled by lists/import; metadata carries 'gen' and 'shards')
//           data row i  ->  {key: KEY#<gen>#<i % shards>, index: i}
//
// One DynamoDB partition key takes ~1,000 writes/second, so a 100K-record
// import into a single partition key needs 100+ seconds however it is
// batched. Spreading the rows over several partition keys lifts that
// ceiling; deriving the shard from the index keeps pop/random to one read.
// 'gen' changes on every import, so a replace is one metadata write (the
// swap) and the previous generation is deleted in the background.
// '#' cannot appear in a user key (common.KEY_RE), so these never collide.
const pkFor = (key, meta, index) =>
  (meta && meta.shards) ? `${key}#${meta.gen}#${index % meta.shards}` : key;

// Record fields the API adds; a user field of the same name would be hidden.
const RESERVED_FIELDS = ['key', 'index', 'createdTime'];

// A stored row as the caller sees it: their fields untouched, plus key
// (the caller's key, not the internal shard key), index and createdTime.
function present(key, row) {
  if (!row) return null;
  return {
    ...(row.data || {}),
    key,
    index: Number(row.index),
    createdTime: row.createdTime === undefined ? null : Number(row.createdTime),
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Start a background job on the worker function ({job: 'import' | 'cleanup'}).
// LOCAL_WORKER=1 (tests only) runs it in this process instead, detached, so
// the API path behaves as it does with a real asynchronous invoke.
async function invokeWorker(payload) {
  if (env('LOCAL_WORKER') === '1') {
    const { worker } = require('./lists_import');
    setImmediate(() => { worker(payload).catch((e) => console.error('local worker', e)); });
    return;
  }
  await lambda.invoke({
    FunctionName: env('WORKER_FUNCTION'),
    InvocationType: 'Event',
    Payload: Buffer.from(JSON.stringify(payload)),
  });
}

module.exports = {
  dynamo, s3, lambda, TABLES, pkFor, present, RESERVED_FIELDS, sleep, invokeWorker,
};
