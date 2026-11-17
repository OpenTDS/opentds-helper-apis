## sms — catch, send and simulate text messages

Use `sms` when the application under test sends a one-time code or a notification by text.
Two kinds of message share the same read calls:

* **Real** — texts that really arrived on a registered handset. A capture app on the phone posts
  each one to `sms/receive` with a secret device key, so anything real came from a known device.
* **Mocked** — texts you simulate with `sms/mocksend` for any number that is *not* a registered
  handset. No SMS is sent; `sms/get` returns them exactly like real ones, with `mocked: true`.

Every message carries `mocked: true|false`. Numbers match on their last nine digits
(`0491570156`, `61491570156` and `+61491570156` are one inbox); send a leading `+` as `%2B`.

### Actions

```
{BASE}/sms/get?number=0491570156                          # newest message for a number
{BASE}/sms/get?number=0491570156&wait=20                  # wait for one that arrives after this call
{BASE}/sms/get?number=0491570156&since=1791248500&wait=20 # ...or after a time you captured earlier
{BASE}/sms/get?number=%2B61400000001&wait=20&real=true    # only a genuine delivery will do
{BASE}/sms/list?seconds=600                               # everything in the last 10 minutes, all numbers
{BASE}/sms/mocksend?number=0491570156                     # simulate "Your verification code is 482913"
{BASE}/sms/mocksend?number=0491570156&code=777&body=PIN%20%7Bcode%7D&from=MyBank
{BASE}/sms/send?number=%2B61400000001                     # REAL text - registered handsets only
```

`sms/receive` is for capture handsets only (POST with the `X-Device-Key` header); scripts never
call it.

### Fields to branch on

* `get` → **`found`** (false = nothing yet; still HTTP 200), `body`, `sender`, **`mocked`**,
  `receivedTime`, `ageSeconds`, `handsetTime`, `waitedMs`. When nothing new arrived:
  `newestAgeSeconds` tells you how old the last message is.
* `send` / `mocksend` → **`code`** (assert this is in the message you read back), `body`,
  `sentTime`, `getUrl` (a ready-made read-back call).
* `send` to a number that is not a registered handset → **403**, no text sent, with `hint` and
  `mockUrl` (the equivalent `mocksend` call). `mocksend` to a registered handset → 403 with
  `sendUrl`. Too many texts to one number → **429** with `retryAfterSeconds`.

### Recipes

**The application under test texts a code to a test user (real handset):**

```
1. t0 = now (epoch seconds)
2. Trigger the app to text the user's phone (a registered handset)
3. GET {BASE}/sms/get?number=<handset>&since=<t0>&wait=20&real=true
   - found=false -> fail: no text within 20 s
   - extract the code from body with a regex
```

**Build and debug the script without a handset (mocked):**

```
1. GET {BASE}/sms/mocksend?number=<random 04915xxxxx>      -> code
2. GET {BASE}/sms/get?number=<same>&wait=5                  -> body contains code, mocked=true
```

**Exercise the whole loop end to end (registered handset):**

```
1. GET {BASE}/lock/acquire?key=<handset>&wait=20           (handsets are shared - take turns)
2. GET {BASE}/sms/send?number=<handset>                     -> code, getUrl
3. GET <getUrl>                                             -> body contains code, mocked=false
4. GET {BASE}/lock/release?ref=<ref>
```

### Pitfalls

* Assert on the **code**, not just that a message arrived — well-known fictitious numbers are
  shared by everyone using the public service, and a handset may have older texts.
* Use `since` (captured just before triggering) or `wait` — a plain `get` returns whatever is
  newest, which may be from an earlier run.
* A real text takes a few seconds; use `wait=20`, not a single immediate call. Don't use `wait`
  from thousands of virtual users.
* `+` in a number must be sent as `%2B`, or it arrives as a space.
* Real messages are kept 7 days, mocked ones 1 day. Anyone who knows a number can read its
  recent messages: use test SIMs, never a personal phone.
