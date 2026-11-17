## webhook — capture webhooks sent by the system under test

Use `webhook` when the application you are testing calls out to a URL (payment notifications,
status callbacks, integration events) and the test needs to prove the call happened with the
right content. Configure the application's webhook URL as
`{BASE}/webhook/receive?key=<RUNKEY>`; every request it makes is stored under that key.

* No registration: any key works the moment it is used. Use a fresh key per test run.
* Any method (GET, POST, PUT, PATCH, DELETE) is captured, with query parameters, headers, source
  IP and body. JSON bodies come back parsed under `json`.
* `status=` makes the catcher answer with that HTTP status (e.g. `status=503`) so you can test the
  sender's retry logic.

### Actions

```
{BASE}/webhook/receive?key=run-7Q2K9ZX4M1                 # the URL you give the system under test
{BASE}/webhook/receive?key=run-7Q2K9ZX4M1&status=500      # ...that fails, to exercise retries
{BASE}/webhook/list?key=run-7Q2K9ZX4M1                    # newest first
{BASE}/webhook/list?key=run-7Q2K9ZX4M1&since=1759112340&wait=20   # wait for one to arrive
{BASE}/webhook/clear?key=run-7Q2K9ZX4M1
```

### Fields to branch on

* `list` → `count`, `more`, `events[]` newest first. Each event: `method`, `query`, `headers`
  (lower-case names), `body` (text), `json` (parsed, JSON bodies only), `bodyBase64` (non-text
  bodies), `receivedTime` (epoch s), `receivedTimeMs`, `sourceIp`, `seq`.

### Recipe: assert a webhook was sent

```
1. key = {BASE}/lists/codes -> rand10                (unique per run)
2. Register {BASE}/webhook/receive?key=<key> as the webhook URL in the app under test
3. t0 = now (epoch seconds)
4. Trigger the business action in the app
5. GET {BASE}/webhook/list?key=<key>&since=<t0>&wait=20
   - count == 0 -> fail: no webhook within 20 s
   - assert events[0].json.<field> == expected
```

### Pitfalls

* Header names are lower-cased; look up `events[0].headers["authorization"]`, not `Authorization`.
* The catcher stores everything it receives, including secrets in headers. Use test credentials.
* `wait` returns as soon as **any** matching event exists — use `since` so an older event from an
  earlier step does not satisfy the wait.
* Events expire after 30 days.
