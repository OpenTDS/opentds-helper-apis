## names — realistic personal names for test personas, by region

Use `names` when a test needs believable first/middle/last names — for sign-up forms, customer
records, or to check that a system handles non-ASCII names (diacritics, family-name-first order,
gendered surnames) correctly. Each call returns one **female** and one **male** persona sharing a
surname, plus ASCII-folded copies of every name. Fifteen regions: `AU NZ GB IE US CA DE FR ES IT
NL PL IN CN JP`. Names only — no addresses, dates of birth or identifiers.

`{BASE}` is `https://api.<domain>/v1`. Every action answers GET. Parameter names are
case-insensitive; region codes too.

### Actions

```
{BASE}/names/get?region=AU                       # random Australian pair
{BASE}/names/get?region=PL&seed=run-0037         # repeatable: same seed -> same names
{BASE}/names/get?region=ANY                      # any region
{BASE}/names/get?notregion=AU,NZ,GB              # anywhere except these
{BASE}/names/regions                             # codes, list sizes, flags
```

### Fields to branch on

* `region`, `regionName`, `seed` (echoed, or null).
* `femaleFirstName femaleMiddleName femaleLastName femaleFullName` and the same four `male…`
  fields.
* `ascii` — the same eight fields with diacritics folded (`Kamińska` → `Kaminska`, `ß` → `ss`).
* `hasDiacritics` — true when any name differs from its ASCII form.
* `familyNameFirst` — true for `JP`/`CN`: `…FullName` is `Last First` and omits the middle name
  (a middle name is still supplied for forms that require one).
* `genderedSurname` — true when the female and male surnames differ (Polish `-ski`/`-ska`).
* `excludedRegions`, `chosenFrom` — only with `notregion`.

### Recipes

**Unique but reproducible persona per iteration:** `{BASE}/names/get?region=ANY&seed={runId}-{iteration}`
— rerunning a failed iteration with the same seed gives the same person (and region).

**Diacritics round-trip:** get `{BASE}/names/get?region=PL` (or `DE`, `FR`, `ES`), enter
`femaleFullName` into the system under test, read it back, and assert it equals
`femaleFullName` — not `ascii.femaleFullName`. Skip the check when `hasDiacritics` is false.

### Pitfalls

* Pass `region` **or** `notregion`, not both (400); one of them is required (use `region=ANY`
  for no preference).
* An unknown code (`AUS`, `UK`) is a **404**, including inside `notregion` — use `GB` for the
  United Kingdom.
* Without `seed` every call is random and may repeat names; with the same `seed` it always
  repeats. Put something unique (run id + iteration) in the seed when you need both.
* Names contain spaces and apostrophes in some regions (`van den Berg`, `O'Brien`); URL-encode
  them when reusing them in query strings.
