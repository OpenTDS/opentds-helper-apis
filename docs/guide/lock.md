## lock — short-lived mutual exclusion between concurrent test scripts

Use `lock` when several scripts (or virtual users, or CI jobs) share a resource that only one may
use at a time — a single test account, a device, a one-at-a-time inbox, a sandbox that cannot
take parallel requests. A script acquires a lock on an agreed **key**, does its work, and
releases it.

* A lock **always expires 60 seconds** after it was taken. There is no renew and no way to ask
  for longer. A script that crashes or forgets to release costs everyone else at most a minute.
* **Busy is not an error:** `acquire` returns HTTP 200 with `locked: false`, so load-test tools do
  not count contention as a failed request. Branch on `locked`.
* `wait` (0–20 s) holds the request open until the lock frees; a waiter picks it up within about
  a quarter of a second of the release.

`{BASE}` is `https://api.<domain>/v1`. Every action answers GET. Parameter names are
case-insensitive.

### Actions

```
{BASE}/lock/acquire?key=test-account-07                 # try once, answer immediately
{BASE}/lock/acquire?key=test-account-07&wait=20         # wait up to 20 s for it to free
{BASE}/lock/release?ref=test-account-07%3A4ecc4935      # release (use releaseUrl from acquire)
```

### Fields to branch on

* `acquire` → **`locked`** (true/false), `key`, `ref` (null when not locked), `expiresTime`
  (epoch s, 60 s after acquiring), `maxSeconds` (60), `waitedMs`, `releaseUrl` (ready-made,
  URL-encoded), `freeInSeconds` (only when `locked: false`), `comment`.
* `release` → **`released`** (true/false), `key`, `ref`, `heldSeconds` (when released) or
  `reason` (when not): `already released`, `no lock has ever been taken on this key`,
  `not the current holder - …` (your lock expired and someone else has it, or has expired).

### Recipes

**Serialise use of a shared test account:**

```
1. GET {BASE}/lock/acquire?key=test-account-07&wait=20
   - locked=false  -> retry (or fail the iteration: the resource stayed busy for 20 s)
   - locked=true   -> save ref / releaseUrl
2. ... do the steps that need exclusive use (must finish well inside 60 s) ...
3. GET {releaseUrl}            (always, including in the error / finally path)
```

**Pool of resources:** try `key=acct-01`, `acct-02`, … with `wait=0` and use the first that
returns `locked: true`.

### Pitfalls

* Branch on `locked` / `released`, never on the HTTP status — all outcomes are 200.
* URL-encode `ref` when you build the release URL yourself (`:` → `%3A`, `+` → `%2B`), or just
  call `releaseUrl`.
* Work that can take longer than 60 s cannot be protected: the lock will expire and another
  script can take it. After a long step, `release` telling you `not the current holder` means
  exactly that.
* `wait` ties up a request for its whole duration — fine for functional/CI scripts, not for
  thousands of virtual users.
* Keys: 1–128 characters, letters, digits and `. _ - : @ + =`, starting with a letter, digit or +;
  case-sensitive. A phone-number key such as `+61400000000` is fine — send the `+` as `%2B`.
* Releasing is not required for correctness, but releasing promptly is what keeps other scripts
  moving.
