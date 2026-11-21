# recon — design and integration notes

Source: `v1/src/recon.py`. Uses `common.run` / `Req` / `ApiError` / `respond` / `raw`, and
`perf.record()` (factored out of `perf/add`) for `perfkey=`.

**Version history.** First build (1.2.0, November 2026): one record per value, so a value sent
twice was a duplicate. Count matching (1.3.0, November 2026), after the first real use: a
reconciliation of ~1,000 SMS whose 9-digit participant ids were masked to their last 4 digits.
41 last-4 values were shared by 2–3 participants. With one record per value, a second expectation of
`1262` was a duplicate of the first, one text marked it `matched`, and the shortfall was
**hidden** (71 missing reported instead of 78: a false pass). The scripts worked around it by
numbering occurrences (`1262-1`, `1262-2`). Count matching makes that unnecessary.

## What it does

Every `expect` and every `confirm` is one **record**. Records are counted per **match key**
(multiset semantics), never deduplicated. The match key is the trimmed value (lower-cased with
`ignorecase`), or its last N characters with `suffix=N`. Both settings are fixed by the key's
first write, so the two sides cannot derive keys differently.

| Group state | Counts | |
|---|---|---|
| `matched` | e = c = 1 | fields given on both sides agree |
| `mismatched` | e = c = 1 | a field given on both sides differs |
| `reconciledByCount` | e = c > 1 | balances; which confirmation is whose cannot be told |
| `short` | c < e | `shortBy` = e − c |
| `over` | c > e | `overBy` = c − e |

Summary, in records: `expected` = Σe, `confirmed` = Σc, `matched` = Σmin(e, c),
`outstanding` = Σmax(0, e − c), `unexpected` = Σmax(0, c − e), `mismatched` = records in
mismatched groups; `groups` per state; `ambiguousGroups` = groups with max(e, c) > 1.
`reconciled` = expected > 0, outstanding = 0, mismatched = 0, unexpected (beyond `grace`) = 0.

Fields are compared only in 1-vs-1 groups. With several records under a key there is no way to
pair an expected record with a confirmed one, so comparing "the" fields would be invented. The
first record on each side keeps its fields; they are shown in every group.

## Decisions (count-matching change request)

| Asked | Done | Why |
|---|---|---|
| Count matching | Yes, the default for every key | recon had no other users; one behaviour to learn |
| `matchKey=last4` / `suffix=N` | `suffix=N` only | One name per concept (CONVENTIONS 3) |
| Group report: key, expected, confirmed, status, shortBy/overBy, candidates | Yes; the match key is `value` (`key` is the namespace in every response) | |
| Summary `yetToBeConfirmed`, `confirmedNotExpected` | Kept as `outstanding`, `unexpected`, now in records | The request said to keep the existing counts |
| `ambiguousGroups` | Yes | |
| "Values still to reconcile" | `recon/list?status=unreconciled` (short + over + mismatched) | |
| Health cases 2/2, 3/2, 1/2 | Yes, with full values expected and masked ones confirmed | |
| Phase 2 | Deferred: channels, catcher auto-confirm, client record ids, a 10-writer test against real DynamoDB | |
| `recon/wait`, 28 s, 409 | Not added: `recon/get?wait=` (≤ 20 s) and `strict=true` → 412 already do this | Conventions: `wait` ≤ 20 s everywhere (a cold start plus the last reads must fit in API Gateway's 29 s); 412 is the API's "gate failed" status (`perf/assert`) |

## Lambda function — ReconFunction, `recon.handler`

* python3.12, MemorySize 512, Timeout 29. `/recon` and `/recon/{proxy+}` (ANY), AWS_PROXY.
* Environment: `RECON_TABLE`, `PERF_TABLE` (for `perfkey=`; without it `perfkey` is a 400),
  `BASE_URL`, the three `BUILD_*` variables.
* IAM (ReconRole): on the recon table and `…/index/*` — `GetItem, BatchGetItem, PutItem,
  UpdateItem, DeleteItem, BatchWriteItem, Query`; on the perf table — `UpdateItem`.
* A POST of up to 5,000 values is written by 16 threads; shard sweeps run 16 queries in parallel.

## Table — ReconTable (unchanged by count matching)

`PK` (S) / `SK` (S); GSI `ByState` `gk` (S) / `gt` (N), projection ALL; TTL `ttl`;
`DeletionPolicy: Retain` (Terraform: `prevent_destroy`).

| Item | PK | SK | Attributes |
|---|---|---|---|
| group | `<key>#G<0-15>` (CRC32 of the match key) | match key | `e`, `c` counts; `ev`, `cv` string sets of the values given (≤ 100 each); `ef`, `cf` first fields; `et`, `ct` first times (ms); `st` state; `gk` `<key>#<state>#<shard>`; `gt` ms it entered the state (short: when cyclepop last handed it out); `k` cyclepop count; `ttl` |
| summary | `<key>#S<0-15>` (random) | `S` | `e c m o u x a` (expected, confirmed, matched, outstanding, unexpected, mismatched, ambiguous), `gm gx gr gs go` (groups per state), `ln ls lmn lmx` (lag count, sum, bounds), `ttl` |
| settings | `<key>#M` | `M` | `ic` ignorecase, `sfx` suffix, `schema` (2), `created`, `exp`, `ttl` |

The first build's items (`#R`, `#C`, settings without `schema`) are not read. A key with old
settings is refused with a 400 until `recon/clear`, which also sweeps the old partitions.
Every item of a key carries the same `ttl`: its first write + 30 days.

## Atomicity

* **A record is one `UpdateItem`:** `ADD e :1, c :0` (or the other way round), `ADD ev :{value}`,
  `SET et = if_not_exists(et, :now)`, `ef = if_not_exists(ef, :fields)`, `ReturnValues=ALL_NEW`.
  There is no read-modify-write. DynamoDB applies concurrent updates to an item one at a
  time, so the counts after this record are exact, and so are the counts before it (one less
  on this side).
* **The summary moves by exactly the difference this record made:** tally(before) → tally(after),
  as `ADD` deltas on a random summary shard (one update per call or per POST batch).
* **The state index follows:** if `ALL_NEW.st` is not the new state, a second update sets
  `st, gk, gt` on condition that `e` and `c` are still what this record left. If another record
  moved the counts in between, this update fails, and that record's own follow-up sets the state
  for the newer counts. If the later record finds the stored state already right, it skips its
  follow-up. Either way the last writer leaves the right state.
* **The candidate cap** is a condition on the same update (`size(ev) < 100 OR contains(ev, :v)`).
  When it fails, nothing was written, and the record is counted again without adding its value.
* **The three requests are not one transaction.** If a function is stopped between them, the
  summary or the index can lag the groups; the groups stay exact. `recon/get?fields=…` recounts
  from the groups. A `TransactWriteItems` per record would double the write cost and could not
  return the counts the deltas are computed from.
* **Retries are not idempotent yet:** records are counted, not stored one by one, so a retried
  add counts twice. Client-supplied record ids (one item per record, `attribute_not_exists`)
  are phase 2.

## Health

`health/check` probes recon on a `suffix=4` key: it expects `111110001 222220001` (2),
`111110002 222220002 333330002` (3) and `111110003` (1), and confirms `xxxxx0001` ×2,
`xxxxx0002` ×2 and `xxxxx0003` ×2. It then checks `0001` is reconciledByCount 2/2, `0002` is
short by 1 with 3 candidates, `0003` is over by 1, and the summary is 6 / 6 / 5 matched /
1 outstanding / 1 unexpected / 3 ambiguous. About 6 requests.

## Tests

`tests/test_recon.py` (moto). moto does not make updates atomic across threads, so:

* batch tests run on one writer thread;
* the follow-up race is tested by injecting a second record between a record's `ADD` and its
  follow-up;
* `test_counters_agree_with_an_independent_multiset_count` checks the running counters, a
  recount from the groups, and a plain `Counter` against each other on 300 random records.

Concurrency against real DynamoDB (the 10 writers × 1,000 test) has to run against a deployed
stack.

## Not verified against the real service

1. Many writers on one hot group: one item takes about 1,000 writes/second, so a single match
   key confirmed by every virtual user at once is the limit (a test rarely shares one key that
   widely).
2. The ~400 KB item limit with large candidate sets (capped at 100 values per side).
