## recon — reconcile what a test submitted with what was processed

Use `recon` when a test submits records to a system (sign-ups, orders, emails into an
email-to-case pipeline) and must later prove that each one was processed — correctly — and that
nothing else was. `recon/expect` records each record the test submitted, `recon/confirm` each
record it saw processed. Records are matched by count per key (the value, or its last N
characters with `suffix=N`); `recon/get` counts outstanding and unexpected records and the
groups that are short, over or reconciled only by count.

`{BASE}` is `https://api.<domain>/v1`. Every action answers GET. Parameter names are
case-insensitive.

### Actions

```
{BASE}/recon/expect?key=run42&value=jane.doe@example.com&ignorecase=true   # submitted (first write fixes ignorecase)
{BASE}/recon/expect?key=run42&value=ORD-1001&amount=120.00                 # with a field to check
{BASE}/recon/expect?key=sms42&value=609829094&suffix=4                      # full id, matched on its last 4
POST {BASE}/recon/expect?key=run42        (body: one value per line, CSV with a value column, or JSON)
{BASE}/recon/confirm?key=run42&value=jane.doe@example.com                  # seen processed
{BASE}/recon/confirm?key=run42&value=ORD-1001&amount=125.00&perfkey=run42.lag
{BASE}/recon/confirm?key=sms42&value=xxxxx9094                              # masked: matches 9094
{BASE}/recon/get?key=run42                                                 # the counts
{BASE}/recon/get?key=run42&wait=20&strict=true                             # wait, then 412 unless reconciled
{BASE}/recon/get?key=run42&value=jane.doe@example.com                      # one key's group
{BASE}/recon/list?key=run42&status=unreconciled                            # short, over, mismatched
{BASE}/recon/list?key=run42&format=csv                                     # a report
{BASE}/recon/cyclepop?key=run42                                            # next short key to check
{BASE}/recon/clear?key=run42
```

### Fields to branch on

* `get` → `reconciled`, `expected`, `confirmed`, `matched`, `outstanding`, `unexpected`,
  `mismatched`, `ambiguousGroups`, `groups`, `latencyMs`.
* `list` → `groups[]`: `value`, `status`, `expected`, `confirmed`, `shortBy`/`overBy`, `candidates`.
* `expect` / `confirm` → `match`, `status`, `expected`, `confirmed`; POST → `recorded`, `paired`.
* `cyclepop` → `value`, or `exhausted: true`.

### Pitfalls

* Values match exactly (case-sensitive) unless the key's first write sent `ignorecase=true`;
  the first write also fixes `suffix`.
* A value sent twice is two records: a resent message makes its group `over`.
* Fields are compared only in groups unique on both sides, and only when given on both sides
  (or named in `fields=`).
* On `expect`/`confirm`, every parameter other than `key value value64 ignorecase suffix perfkey`
  is a field; `wait`, `strict`, `fields`, `status`… are rejected there.
* A key with nothing in it is never reconciled.
