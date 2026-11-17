## lists — shared test-data queues, variables, counters and a cleanup register

Use `lists` when several scripts or virtual users need to share state during a
test: hand each user a unique record (`pop`), pick any record (`random`),
pass a value from one script to another (`vars`), generate sequence numbers
(`counters`), and remember everything a test created so a teardown script can
delete it (`cleanup`). Bulk data is loaded from a CSV or JSON file with
`import`.

Everything is scoped by `key`, a namespace you choose: 1–128 characters of
letters, digits and `. _ - : @ + =`, starting with a letter, digit or +,
case-sensitive. Use a fresh key per run (for example the `rand10` from
`lists/codes`) to isolate runs. All endpoints answer GET; parameter names are
case-insensitive.

### Actions

```
GET {BASE}/lists/push?key=run42&email=alice@example.com&plan=gold   # append a record; every param except key becomes a field
GET {BASE}/lists/pop?key=run42                  # next unused record; each record handed out once across all callers
GET {BASE}/lists/cyclepop?key=run42             # like pop, but goes round again from record 1 instead of running out
GET {BASE}/lists/random?key=run42               # a random record; does not consume it
GET {BASE}/lists/list?key=run42&start=1&limit=10   # read records (paged)
GET {BASE}/lists/reset?key=run42                # rewind pop to record 1 (records kept)
GET {BASE}/lists/clear?key=run42                # delete every record of the key
GET {BASE}/lists/codes                          # random A-Z0-9 codes: rand3, rand4, rand6, rand8, rand10

GET {BASE}/lists/vars/set?key=run42&name=orderId&value=SO-1001   # save a variable
GET {BASE}/lists/vars/get?key=run42&name=orderId                 # read it (404 if never set)
GET {BASE}/lists/vars/list?key=run42                             # all variables of the key

GET {BASE}/lists/counters/add?key=run42&name=orders&increment=1  # atomic add; returns the new value
GET {BASE}/lists/counters/get?key=run42&name=orders              # read (0 with exists:false if unused)

GET {BASE}/lists/cleanup/push?key=run42&value=SO-1001&note=vu7   # register something to delete later
GET {BASE}/lists/cleanup/pop?key=run42                           # next thing to delete (oldest first)
GET {BASE}/lists/cleanup/list?key=run42&status=pending           # audit trail: all | pending | removed

POST {BASE}/lists/import?key=run42&format=csv    (body = the file)   # replace the key's records with a file (<= 4 MB)
GET  {BASE}/lists/upload/url?key=run42&format=csv&size=52428800     # presigned S3 POST for files up to 50 MB
GET  {BASE}/lists/import?key=run42&format=csv&ref=REF               # import an uploaded file (or reload an old one)
GET  {BASE}/lists/import/status?key=run42&ref=REF                   # progress / result of an import
GET  {BASE}/lists/upload?format=json                                # HTML drag-and-drop loader for humans
```

### Response fields to branch on

* **Records** (`pop`, `random`, `list`, `push`) come back as `record`: your
  fields exactly as stored at the top level, plus `key`, `index` (1-based
  position) and `createdTime` (epoch seconds). JSONPath: `$.record.email`.
  Pushed values are strings; imported JSON keeps its types (numbers, objects,
  arrays).
* `pop`: `exhausted` (true = nothing left; still HTTP 200, `record` is null),
  `index`, `count`, `remaining`.
* `cyclepop`: like `pop` plus `pass` (times round the list) and `wrapped`; `exhausted` only when the key has no records. Shares pop's pointer.
* `random`: `empty` (true = key has no records), `count`, `index`.
* `list`: `count`, `nextPop`, `returned`, `truncated`, `nextStart` (pass as
  `start` to get the next page; null when done), `records`.
* `vars/get`: `value`; HTTP 404 when the variable does not exist (with `wait=`/`equals=`: 200, `exists: false`). `equals=1&wait=20` waits for a value.
* `counters/add` / `counters/get`: `value` (after the change); `exists`.
* `cleanup/pop`: `exhausted`, `value`, `note`, `index`.
* `import` / `import/status`: `state` is `done` (HTTP 200), `queued` or
  `running` (HTTP 202 — poll `import/status` with `ref`), `failed` (HTTP 422,
  reason in `error`), or `uploaded` (status only: in S3, never imported). On
  `done`: `loaded`, `skippedCount`, `skipped[]` (line/item + reason),
  `warnings[]`, `columns`, `sample` (record 1 exactly as `pop` returns it),
  `next.pop` etc. (ready URLs).
* Errors: `{"error": "...", "status": 400}`; bad or missing `key` is 400,
  unknown action is 404 with the valid `actions`.

### Recipes

**1. Load test data before a run, then consume it (one record per iteration).**

```
POST {BASE}/lists/import?key=run42-users&format=csv     Content-Type: text/csv
     body: email,password\nu1@example.com,Pa55-1\nu2@example.com,Pa55-2\n
  -> 200 {"state":"done","loaded":2,...}      (202 => poll GET {BASE}/lists/import/status?key=run42-users&ref={ref})
each iteration:
GET  {BASE}/lists/pop?key=run42-users
  -> if $.exhausted == true: stop (or GET {BASE}/lists/reset?key=run42-users to recycle)
  -> use $.record.email and $.record.password
```

**2. Hand a value from a setup script to the test script.**

```
setup:  GET {BASE}/lists/vars/set?key=run42&name=accountId&value=ACC-7781
test:   GET {BASE}/lists/vars/get?key=run42&name=accountId    -> $.value
```

**3. Clean up everything the test created.**

```
after each create:  GET {BASE}/lists/cleanup/push?key=run42&value={createdId}
teardown loop:      GET {BASE}/lists/cleanup/pop?key=run42
                    -> while $.exhausted == false: delete $.value in the system under test
check:              GET {BASE}/lists/cleanup/list?key=run42&status=pending   -> $.pending == 0
```

### Pitfalls

* **Import always replaces** the key's records and rewinds `pop` to 1. It does
  not append. The swap is atomic; a failed import leaves the old records.
* `import` needs `format=csv` or `format=json`. `wait` is **seconds** (0–25,
  default 25); `wait=0` returns `queued` immediately. Always check `state`,
  not just the HTTP status.
* CSV values are always strings (leading zeros kept). In JSON, quote long IDs;
  unquoted integers above 2^53 are converted to exact strings.
* A field named `key`, `index` or `createdTime` would clash with the fields
  every record carries: `push` rejects it (400); `import` renames it to
  `key_2`, `index_2`, `createdTime_2` and warns.
* `push` values are strings; URL-encode `+ & % #` in them.
* The body route takes up to 4 MB. Bigger files: `upload/url`, POST the
  `fields` then the file (part name `file`, last) to `url`, then GET `next`.
* `clear` removes records only; variables, counters and the cleanup register
  of the same key are kept.
* A counter is one item (~1,000 updates/s). For unique values at high volume
  use `lists/codes`.
* `cleanup/pop` marks an entry removed when it hands it out, not when you
  confirm the delete.
