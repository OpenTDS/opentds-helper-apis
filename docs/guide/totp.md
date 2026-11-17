## totp — one-time codes for MFA-protected test accounts

Use `totp` when the application under test enforces authenticator-app MFA (RFC 6238 TOTP). During
enrolment the app shows a QR code and usually the secret as text (or an `otpauth://` URI whose
`secret=` parameter holds it). Store that secret once; afterwards ask for the current code whenever
the login asks for one.

### Actions

```
{BASE}/totp/set?key=run-7Q2K9ZX4M1&user=alice@example.com&secret=JBSWY3DPEHPK3PXP
{BASE}/totp/get?key=run-7Q2K9ZX4M1&user=alice@example.com
{BASE}/totp/get?secret=JBSWY3DPEHPK3PXP                    # stateless, nothing stored
```

Optional on both: `digits` (6/7/8), `period` (seconds, default 30), `algorithm`
(sha1 default, sha256, sha512) — only needed for non-standard authenticators.

### Fields to branch on

* `get` → `code`, `nextCode`, `previousCode`, `secondsRemaining`.
* `set` → `saved: true`. A bad secret is a 400; `get` for an unknown key/user is a 404.

### Recipe: enrol and log in

```
1. Sign up in the app; on the MFA screen capture the secret text (or the secret= value of the
   otpauth:// URI, URL-decoded).
2. GET {BASE}/totp/set?key=<runkey>&user=<login>&secret=<secret>
3. GET {BASE}/totp/get?key=<runkey>&user=<login>  -> submit code to confirm enrolment
4. At every later login: totp/get again and submit code.
```

### Pitfalls

* If `secondsRemaining` < 3, the code may roll over before the app checks it: wait
  `secondsRemaining + 1` seconds and call `get` again.
* Many apps reject re-use of the same code for the same user within one period. Two logins in
  the same 30 s window for one user may need a wait for the next code.
* URL-encode `user` (`@` is fine, `+` must be `%2B`).
