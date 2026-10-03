# claude-spend-audit

**Everything runs on your machine. No data leaves it.** There is no hosted
service, no telemetry, and no uploads. All analysis is local: your usage data
is read, priced and reported without ever being sent anywhere.

The tool makes exactly one kind of network request, and only if you ask for it:
`fetch` calls one of Anthropic's own reporting APIs -- the Usage & Cost Admin
API or the Claude Enterprise Analytics API -- to *download* your data. It sends
nothing but the request. Everything else, including every CSV path, touches no
network at all.

The dependency list is empty, so you can verify all of that yourself in about
five minutes rather than taking it on trust.

A CLI that reads Anthropic API usage and Claude plan usage data and reports
where the money is going, with dollar figures you can trace to a file you
control.

---

See [`examples/sample-report.md`](examples/sample-report.md) for what the output
looks like. It comes from a synthetic month that ships with the repo, so you can
reproduce it before pointing anything at your own data:

```bash
claude-spend-audit audit examples/sample-usage.csv --cost examples/sample-cost.csv
```

The organisation in it is invented, and the numbers are chosen so that every
check has something to say.

## Read this first: nothing here has been tested on real account data

Every input path was built from Anthropic's published documentation and tested
only against synthetic data written to match it. No real export, and no real
API response from a real account, has been run through this tool.

How much that matters depends on the source:

- **The two API paths** (`fetch`, `fetch --source analytics`) use field names
  copied from Anthropic's published API references. They are the most likely
  to work as written.
- **The cookbook CSV** profile copies its column names from the published
  source of Anthropic's own exporter.
- **The Console CSV and Team/Enterprise spend-report CSV** mappings are
  educated guesses. Anthropic does not publish those exports' column headers.

**Check the billed total against your real bill before trusting any dollar
figure.** Cost units differ by source, and one of them is an assumption:

- The Cost API and the Enterprise Analytics cost reports document their
  amounts in cents (fractional cents for Analytics); they are divided by 100.
- The Console cost CSV is *assumed* to be in cents like the API it mirrors.
  This is a guess.
- The cookbook cost CSV and the spend report are read as dollars (their
  columns are named `amount_usd` and `..._spend_usd`).

If the "billed" line in the report is 100 times larger or smaller than the
figure in your Console or invoice, the unit assumption is wrong for your
export -- every cost in the report is off by the same factor. The report also
raises a critical caveat (and exits `1`) when billed and estimated cost are
20x or more apart. Please report it.

Because the exact formats are unknown, some files are refused rather than
guessed at. What you might see, and what to do:

| Refused or flagged | Why | What to do |
|---|---|---|
| A cost CSV that fits two profiles equally, one reading cents and one dollars | the file gives no evidence for either, and a wrong pick is 100x | pass `--cost-profile schemas/console_cost_csv.json` (cents) or `schemas/cookbook_cost_csv.json` (dollars) |
| Slash dates that could be either order (every day and month 12 or less) | 03/08/2026 could be March or August | pass `--date-order mdy` or `--date-order dmy` |
| Any currency other than USD | the tool reports dollars and converts nothing | export in USD |
| Decimal commas (`10,00`, `1.200,50`) | stripping the comma would read 100x too large | re-export with `.` as the decimal point |
| A file that is not UTF-8 or UTF-16 | a guessed code page garbles names silently | save as "CSV UTF-8" |
| A missing token column (for example no cache-read column) | a missing column is not zero tokens | the checks that need it are skipped, with a caveat naming the column |
| Rows labelled "Total", per-user subtotals, rows with no date | counting them would double the figures | they are excluded and counted in a caveat |

Every path fails loudly rather than guessing. An export that does not match
prints its real header row; an API response that does not make sense raises
rather than returning partial data. If you hit a mismatch, please open an issue
with the header row or the shape of the response -- never the data itself.

## Start here: which data do you have?

| Your account | Best source | Fallback | What you get |
|---|---|---|---|
| Developer Console (API) with an Admin API key | `fetch` (Usage & Cost Admin API) | Console CSV export | every usage check |
| Developer Console, no Admin API key | Console CSV export | CSV from Anthropic's cookbook exporter | most usage checks, depending on the export's columns |
| Claude Enterprise | `fetch --source analytics` (Analytics API key) | spend report CSV | most usage checks, C7r and E1-E4 |
| Claude Team | spend report CSV | none | E1-E4 only, descriptive |
| Claude Pro or Max (individual) | not supported | none | no usage export exists |
| Amazon Bedrock, Google Vertex AI, Claude Platform on AWS | not supported | none | this usage never reaches Anthropic's reporting APIs |

An organisation that uses both the Developer Console and Claude Enterprise can
audit each separately: they are different accounts with different keys and
different data.

### Sources, best to worst

1. **Usage & Cost Admin API** and **Claude Enterprise Analytics API**.
   Documented field names, real time buckets (daily or hourly), all five token
   categories with the two cache-write durations kept apart, and cost data
   beside the usage. Everything the checks need, with the least guessing.
2. **Cookbook exporter CSV.** Verified column names, but daily buckets only,
   no context-window column, and it adds the two cache-write durations
   together -- which blocks C1, the strongest check.
3. **Console CSV export.** Potentially as detailed as the API, but its column
   names are guessed, and the cost file's units (cents or dollars) are
   assumed. Check the billed total against your invoice before trusting
   anything else.
4. **Team/Enterprise spend report CSV.** One row per person per model, summed
   over the whole range: no time axis, no cache breakdown, mostly guessed
   column names. It supports a descriptive picture of where the money went
   and nothing stronger.

## Caveats that apply to every source

- **Untested on real data** -- see above.
- **Check figures use list rates.** Every dollar figure a check computes comes
  from [`pricing/2026-09.json`](pricing/2026-09.json). Negotiated discounts and
  plan pricing are not reflected unless you edit that file. The Enterprise
  Analytics path shows your billed-to-list ratio in the header so you can scale
  mentally; billed totals read from cost data are always what was billed.
- **Some spend cannot be priced or seen.** Priority and flex service tiers have
  no published rate and are counted but not priced. Priority Tier costs are
  missing from the cost data entirely. Bedrock, Vertex AI and Claude Platform
  on AWS traffic never appears. Each report says so where it applies.
- **No request-level detail.** Every source is aggregated. Nothing can tell ten
  long requests from a thousand short ones, and thinking tokens are billed as
  output without ever being broken out.
- **Confidence levels mean what they say.** Only a `measured` finding is
  arithmetic on your data. A `candidate` is a pattern the data cannot confirm
  and always carries a range with its assumptions. An `observation` makes no
  savings claim at all -- and on the spend-report path, every finding is one.

## Using it with each kind of account

### Developer Console (Claude API)

**With an Admin API key** (best). Create one in the Console under Settings >
Admin keys, then:

```bash
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
```

```bash
claude-spend-audit fetch --from 2026-08-01 --to 2026-09-01 -o raw.json
```

```bash
claude-spend-audit audit raw.json
```

`fetch` saves the raw responses and stops. Analysis then runs offline as many
times as you like, at any thresholds, without touching the API again -- and
the saved file is something you can read before deciding whether to share it.
Add `--bucket-width 1h` for the off-hours check (C6b), and fetch cost data
separately with `--what cost` to compare against billing.

**With a CSV export** (no credentials). Export from the Console's Usage and Cost
pages and run against the files:

```bash
claude-spend-audit audit usage.csv --cost cost.csv
```

The file is matched against every profile in `schemas/` and the best match
wins; `--explain-schema` prints which column mapped to which field. CSVs from
Anthropic's cookbook exporter (`observability/usage_cost_api.ipynb`) are
recognised the same way.

Caveats for this account type:

- Console CSV column names are guessed (profiles marked `provisional`). If an
  export does not parse, the error names the file to edit -- a JSON change,
  not a code change.
- The Console cost CSV is assumed to be in cents, like the API. If its billed
  total looks 100 times too small or large, that assumption is wrong.
- The cookbook exporter adds the two cache-write durations together, so C1
  cannot run on its output; the tool says so and prices those tokens at the
  cheaper rate, making the estimate a floor.

### Claude Enterprise

**With an Analytics API key** (best). Only the Primary Owner can create one, in
claude.ai under Organization settings > API; it carries the `read:analytics`
scope. An Admin API key does not work here.

```bash
export ANTHROPIC_ANALYTICS_KEY=...
```

```bash
claude-spend-audit fetch --source analytics --from 2026-08-01 --to 2026-09-01 -o raw.json
```

```bash
claude-spend-audit audit raw.json
```

One fetch saves every series the audit needs into one file: usage by model,
product, context window, region and speed; an ungrouped total to cross-check
it; billed and list-price cost; spend by RBAC group; and per-user usage and
cost. Pass `--no-users` to leave the per-user reports -- and every email
address -- out of the file, and `--anonymize` at audit time to replace emails
with `user-001`, `user-002`... in the report.

Caveats for this source:

- **Check figures are at list price.** The header shows billed, list price and
  the ratio between them; actual spend on a finding is likely lower by roughly
  that ratio, though the discount need not be uniform.
- **Recent data moves.** Figures refresh about every four hours and can be
  revised for 30 days. The report flags a range that runs past the latest
  refresh (which also sets exit code 1) or into the revision window.
- **Grouped results can be cut short.** The API keeps at most 100 groups per
  time bucket, with no remainder row. The tool cross-checks against the
  ungrouped total and flags any shortfall, with exit code 1.
- **RBAC groups overlap.** A user in two groups counts in both, so group rows
  are never added up; C7r uses only the row for users in no group.
- Data starts on 2026-01-01, and each request covers at most 31 days (the tool
  splits longer ranges itself). Plan products manage caching themselves, so
  cache remedies may not be available to you even where C1 or C2 measures a
  problem. Per-user figures cover seat users only.

**With the spend report** (fallback). See Claude Team below; it works the same.

### Claude Team

Team plans have no reporting API, so the source is the **spend report**:
Settings > Analytics > "Export spend report". Run the same command as for any
other file:

```bash
claude-spend-audit audit spend-report.csv
```

```bash
claude-spend-audit audit spend-report.csv --format markdown --anonymize -o overview.md
```

The file is recognised by its columns and routed to its own checks, E1-E4.

Caveats for this source:

- **Descriptive only.** It is one row per person per model, summed over the
  range you exported, so it shows where the money went but cannot support a
  savings claim. Every finding is an `observation`, and the tool refuses to
  produce anything else from this data.
- **The column mapping is a guess.** Anthropic documents five of the report's
  column names and describes the rest in prose, so
  `schemas/enterprise_spend_csv.json` carries many candidate spellings per
  field. A file with a totals row has that row excluded and reported.
- No dates means no spike or off-hours checks; no cache breakdown means no
  cache checks; and whether its prompt-token figure includes cache reads is not
  documented.

## What it checks

| | check | confidence |
|---|---|---|
| **C1** | Cache writes not read back often enough to pay for themselves | `measured` |
| **C2** | High uncached input with almost no cache reads | `candidate` |
| **C3** | Standard-tier spend that could take the 50% batch discount | `candidate` |
| **C4** | Long-context composition, and what a token removed there is worth | `observation` |
| **C5** | Segments where output is most of the bill | `observation` |
| **C6a** | Daily spend spikes above the recent baseline | `measured` |
| **C6b** | Spend falling outside business hours | `measured` |
| **C7** | Spend that cannot be allocated to a named workspace or key | `observation` |
| **C7r** | Spend from users in no RBAC group (Claude Enterprise) | `observation` |
| **C8** | Model mix across frontier, mid and small models | `observation` |
| **E1** | How concentrated spend is across users | `observation` |
| **E2** | Spend by Claude product (chat, Claude Code, Cowork...) | `observation` |
| **E3** | Model mix, from per-user data | `observation` |
| **E4** | Product/model groups where completion tokens are a large share | `observation` |

Which checks can run depends on the source. A check the data cannot support is
skipped, and the report names it and says why.

| check | Admin API | Console CSV | Cookbook CSV | Enterprise Analytics | Spend report |
|---|---|---|---|---|---|
| C1 | yes | if the export splits cache writes | no: cache writes summed | yes | no |
| C2 | yes | yes | yes | yes | no |
| C3 | yes | if it has a service tier column | yes | no: no service tier | no |
| C4 | yes | if it has a context window column | no | yes | no |
| C5 | yes | yes | yes | yes | no (E4 instead) |
| C6a | yes, 8+ days | yes, 8+ days | yes, 8+ days | yes, 8+ days | no |
| C6b | with `--bucket-width 1h` | if exported hourly | no: daily only | with `--bucket-width 1h` | no |
| C7 | yes | if it has a workspace column | yes | no (C7r instead) | no |
| C7r | no | no | no | yes | no |
| C8 | yes | yes | yes | yes | no (E3 instead) |
| E1-E4 | no | no | no | yes, unless `--no-users` | yes, where columns allow |

## Install

```bash
pip install -e .
```

Python 3.11 or newer. No runtime dependencies.

Install editable, or run from a checkout. The rate card (`pricing/`) and the CSV
schema profiles (`schemas/`) deliberately live at the repo root rather than
inside the package, because both are meant to be read and edited — that is the
whole point of the rates being traceable. The trade-off is that a plain
`pip install .` leaves them behind; if you need that, pass `--pricing` and
`--usage-profile` explicitly. Either way the tool refuses to run rather than
falling back on rates it cannot show you.

## Use

`audit` runs the checks and prints ranked findings. `inspect` does the same
loading but runs nothing, reporting only what the data covers and which checks
it can support. For the shareable version, add `--format markdown -o report.md`.

Useful flags:

- `--explain-schema` — print exactly which CSV column mapped to which field
- `--date-order mdy|dmy` — how to read slash dates when the file cannot settle
  it; only needed if the tool asks
- `--cost-profile` / `--usage-profile` — force a schema profile, for example
  when a cost file's cents-or-dollars reading is ambiguous
- `--json` — machine-readable output
- `--anonymize` — replace user emails with `user-001`... (per-user data only)
- `--pricing pricing/2026-09.json` — use a specific rate card
- `--utc-offset -5` — set your local business day for the off-hours check
- `--business-hours 08:00-17:00` — set the window it measures against
- `--min-impact 50` — drop findings below a dollar threshold
- `--cache-prefix-share`, `--batch-eligible-share` — narrow C2's and C3's
  savings ranges from your own knowledge of the workload. Both default to a
  floor of $0, because guessing them would put an invented number in the report.

For `fetch`: `--source admin|analytics`, `--from` / `--to` (UTC dates, end
exclusive), `--bucket-width 1d|1h|1m`, `--group-by`, `--no-users`, and
`--api-key-env` to read the key from a different variable. **The key is only
ever read from an environment variable.** There is no `--api-key` flag,
deliberately: an argument lands in shell history and is visible to every user
on the machine through `ps`.

Exit code is `1` when something in the data means the headline total understates
the real bill, so a script wrapping this cannot mistake a caveated number for a
clean one.

## How findings are ranked, and what a dollar figure means

Findings are ranked by confidence first, dollars second, and two different
numbers are kept apart throughout: **measured** is what was spent and is
arithmetic; **recoverable** is what could actually be saved and is often
unknowable from aggregate data. C6 can measure that $40,000 was spent overnight
without being able to say whether any of it was avoidable, so it reports the
first and prints `not claimed` for the second. Within a confidence level, a
finding that claims a saving comes before one that only measures spend.

Every rate the tool uses is in [`pricing/2026-09.json`](pricing/2026-09.json),
with the source URL and fetch date recorded, and each entry marked either as
published or with the derivation that produced it. If a rate is wrong for your
organisation, edit that file and every figure moves with it.

[`ARCHITECTURE.md`](ARCHITECTURE.md) explains the confidence model in full,
along with the invariants that keep the numbers honest and how to verify any
figure by hand.

## Development

```bash
python -m pytest -q
```

Adding a check is described in [`ARCHITECTURE.md`](ARCHITECTURE.md); it never
requires touching a renderer. After changing anything that affects output,
regenerate the committed sample:

```bash
python -m tests.generate_sample
```

Every check is a pure function of the normalized data, and every dollar figure
in the tests is hand-calculated in a comment beside it, so a changed formula
fails a test rather than quietly changing someone's number.

## License

MIT
