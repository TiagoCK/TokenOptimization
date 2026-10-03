# Architecture

This document is for the engineer deciding whether to trust this tool's output,
or wanting to extend it.

The tool prints dollar figures that someone may take to their finance team. That
single constraint explains nearly every design decision below: where the data
model is more complicated than it looks, where a function returns a union type
instead of a number, and why several things that could be estimated are
deliberately left unestimated.

## Contents

- [Data flow](#data-flow)
- [The six invariants](#the-six-invariants)
- [The confidence model](#the-confidence-model)
- [What the tool changes about your data](#what-the-tool-changes-about-your-data)
- [Verifying a figure by hand](#verifying-a-figure-by-hand)
- [Adding a check](#adding-a-check)
- [Known limitations](#known-limitations)

## Data flow

```
Console CSV export ─┐
                    │   ingest/csv_ingest.py     (column mapping driven by
                    │                             schemas/*.json profiles)
                    ├──→  Frame  ──→  checks/  ──→  Finding[]  ──→  render/
                    │   frame.py      registry     findings.py    terminal.py
Usage & Cost API ───┘                                             markdown.py
  ingest/api_client.py -> saved file -> ingest/api_json.py
  (fetch: HTTP)          (raw pages)    (normalise)

Alongside, at every stage:  Gap (gaps.py) -- what this data cannot show
Alongside, at pricing:      PricingTable  <-- pricing/<version>.json

Enterprise Analytics API ──→ ingest/api_client.py ──→ saved file ──→ ingest/analytics_json.py
  (fetch --source analytics)    (windowed, 410-safe)               ├─→ Frame       (usage series)
                                                                   └─→ SpendReport (per-user series)

Team/Enterprise spend report ──→  SpendReport ──→ enterprise/checks.py ──→ Finding[] ──→ render/
  ingest/enterprise_csv.py        enterprise/      E1-E4, descriptive only   (observations)
  (schemas/enterprise_spend_csv.json)  report.py
```

Two ingest paths normalise into one `Frame`. Checks are pure functions of
`(Frame, PricingTable, CheckConfig)`. Renderers consume `Finding[]` and know
nothing about which checks exist. `cli.py` wires it together and owns exit
codes; nothing else imports it.

The CSV path is the one that matters in practice — it needs no admin key and no
security review — but the API path is what the data model is built around,
because that is the surface Anthropic documents. A test asserts both paths
produce an identical `Frame` from the same data.

**A third path does not feed the Frame at all.** A Team or Enterprise plan's
spend report is one row per person per model, summed over the export's range:
no time buckets, no cache-write split, no service tier. Normalised into a
Frame, every missing bucket would read as zero -- C1 would report a clean
result, C2 would flag every model as never caching, C6 would see a flat
month. So it loads into its own `SpendReport`, where a column the export lacks
is `None` rather than `0` (`SpendReport.present` is the counterpart of
`Frame.grouped_by`), and runs its own four checks. They reuse the `Finding`
contract, the renderers' finding and caveat sections, and C5's and C8's
helpers, and they are **descriptive only**: each is an `observation`, and
`run_enterprise_checks` raises if any check returns anything else. `cli.py`
routes a file there by its columns, and only a file no usage profile matches
can be claimed.

**The Enterprise Analytics API feeds both.** Its usage and cost reports share
the Usage & Cost API's wire format, so `api_json` normalises them unchanged into
a Frame (with `product` as an extra dimension), and the usage checks run on it.
Its per-user reports become a SpendReport, so E1-E4 run on documented fields
instead of a guessed CSV mapping. Two properties of the source need guarding.
Each grouped time bucket holds at most 100 groups and no remainder row, so
`fetch` also saves an ungrouped total and ingest compares the two bucket by
bucket, raising a gap that invalidates the total when they differ. And RBAC
group rows overlap -- a user in two groups counts in both -- so grouping a
Frame by `rbac_group_id` is refused outright: it would count tokens twice.
RBAC coverage (C7r) is computed from a separate cost series grouped by group,
and uses only the row for users in no group against the ungrouped cost total
-- the one comparison the overlap cannot corrupt. If a day's breakdown hits
the 100-group cap without that row, the figure is reported as a lower bound.

**More than one tool produces these CSVs**, and they disagree. The Console UI
export and Anthropic's published cookbook exporter use different column names
and — in the cost file — different units, one writing cents and the other
dollars under a differently-named column. So a file is matched against every
known profile in `schemas/` and the one that recognises the most of it wins;
guessing there is a 100× error. `--explain-schema` prints which profile was
chosen and what each column mapped to.

## The six invariants

These are correctness rules, not style preferences. Each is enforced somewhere
you can go and read.

### 1. Money is `Decimal`, never `float`

Binary floating point cannot represent $0.10, and the error compounds over a
month of buckets. The pricing loader rejects a JSON float outright rather than
accepting one and rounding. `tests/test_no_float_money.py` walks the AST of
every module in the package looking for a `float()` call, so this cannot regress
quietly.

JSON output serialises money as strings for the same reason.

### 2. An unavailable rate returns `Unpriced`, never `0`

Anthropic publishes rates for the `standard` and `batch` service tiers only.
`priority`, `priority_on_demand`, `flex` and `flex_discount` have no published
price, and Priority Tier costs are absent from the cost endpoint entirely.

`PricingTable.rate_for()` returns `Priced | Unpriced`. `Unpriced.cost_of()`
returns `None`. There is no code path that silently treats an unknown rate as
zero, because a silent zero produces a confidently wrong total — the single most
damaging thing this tool could do.

Unpriced tokens are reported alongside every estimate rather than folded into
it, so a partial estimate can never be mistaken for a complete one.

Model lookup is exact-match only: canonical ID, then an explicit alias table for
dated snapshots, then display name. Never fuzzy. Pricing the wrong model is
worse than refusing to price.

### 3. A null dimension is not the same as an absent one

This is the subtlest and most important one.

The Usage API returns `null` for `workspace_id` in two completely different
situations: the traffic belonged to the **default workspace**, or the caller
**did not group by workspace**. Same value, opposite meanings. The same is true
of `api_key_id`, where null means Console playground traffic.

`Frame.grouped_by` records which dimensions the source actually carried.
`Frame.value_of()` returns a `NOT_GROUPED` sentinel — not `None` — when a
dimension was never broken out.

Without this, an export that simply was not grouped by workspace would report as
100% unattributed spend. That would be a fabricated finding, on precisely the
check where a fabrication is most damaging.

### 4. A `candidate` never carries a point estimate

Any finding whose savings the data cannot confirm must present a **range**, with
the assumptions that produce it printed next to the number rather than in a
footnote.

This is enforced in `Finding.__post_init__`, which raises on: a candidate with a
point estimate, a candidate range with no assumptions, an observation claiming
any savings, an inverted range, or a dollar figure with no stated basis. A
finding that would break the rule cannot be constructed, so the failure lands in
a check's own tests rather than in a report that has already been forwarded.

### 5. "What was spent" and "what could be saved" are different claims

`Finding` carries both, separately:

- `measured_usd` — arithmetic on the data, with a required `measured_basis`
  saying what the number is.
- `recoverable` — an estimate of what could actually be saved. Frequently
  `None`, because aggregate data often cannot support one.

C6 can measure that a given amount was spent outside business hours — that is
arithmetic — while claiming nothing about whether any of it was avoidable,
because a token count cannot distinguish a runaway agent from a nightly batch
job. It reports the first and prints `not claimed` for the second.

Collapsing these into one "impact" number is how audit tools end up overstating
their case.

### 6. A check whose preconditions the data cannot meet is skipped, not approximated

Every check declares what it needs: which dimensions the data must be grouped
by, which time granularity, how many days of history, and which token buckets
would make it unsound. When the data cannot support a check, it does not run,
and the report says which check was skipped and why.

The sharpest case is C1. Its entire value is that the two prompt-cache TTLs are
priced differently — 1.25× base input for the 5-minute cache, 2× for the
1-hour, so break-even differs fourfold between them. Some exporters, including
Anthropic's own cookbook, sum the two into a single column before writing their
CSV. That sum cannot be undone. Given only the total, C1 would have to assume a
TTL, and would understate waste on 1-hour-heavy workloads with nothing visible
to say it had guessed. So it is blocked instead. Those tokens are still counted
and still priced, at the cheaper of the two rates, so the spend figure is an
explicit floor rather than a guess in an unknown direction — and the caveat
says so, and by how much it could understate.

C6a is the same argument about time rather than content. Its baseline is the
median of the preceding week, so that every baseline spans both weekdays and a
weekend — with three days, data starting on a Saturday judges Tuesday against a
weekend median and reports a spike that never happened. That makes a seven-day
export (the API's own default page) one baseline with no day left to judge, so
the check is skipped rather than run: it could only report nothing, and nothing
reads as a clean bill of health.

A tool that quietly produces a number for every input looks more capable and is
worth less.

## The confidence model

Every finding carries one of three levels, and the report is ranked by
confidence first, then by whether a saving is claimed, then by size.

| level | meaning | savings claim |
|---|---|---|
| `measured` | computed directly from the data; the arithmetic is the finding | may be a point estimate, when the arithmetic supports one |
| `candidate` | a pattern consistent with waste that the data cannot confirm | always a range, always with assumptions attached |
| `observation` | context, with no savings claim at all | none, structurally |

Ranking within tier rather than purely by size is deliberate. An un-sizeable
exposure can be numerically larger than a recoverable waste, and sorting them
into one list would imply a priority the data does not support.

The tier alone does not finish the job, because C1 and C6 are both `measured`:
a month of overnight spend easily dwarfs the cache premium C1 proves was
wasted. So within a level, a finding that claims a saving sorts ahead of one
that only measures spend. The two numbers are never compared with each other.

## Handling the admin credential

The API path is the only feature that asks for a credential, and it is an
org-wide one. Four rules govern it, each with a test that fails if it is
removed.

**It is only ever read from an environment variable.** There is no `--api-key`
flag. An argument lands in shell history and is visible to every user on the
machine via `ps`; that is not a trade worth making for typing convenience, and
the error message says so rather than just failing.

**It cannot be rendered.** The value lives behind a `Credential` whose `repr`
and `str` are redacted, so it cannot reach a log or a traceback through an
f-string — which is how secrets actually escape, not by anyone deciding to
print one.

**It is stripped from anything on its way to an exception.** An API can echo a
bad key back in its own error body, and that body ends up in our message.

**It never enters a URL.** URLs reach proxy logs, shell history and error
messages. The credential is a header and nothing else.

One defect worth recording, because it is the kind that only a test finds:
argparse's prefix matching originally accepted `--api-key sk-ant-...` as an
abbreviation of `--api-key-env`, which made the secret the *variable name* and
printed it straight back to stderr. The `fetch` parser now sets
`allow_abbrev=False` and refuses `--api-key` explicitly, without echoing what
it was given.

## What the tool changes about your data

Almost nothing — but not quite nothing, and the exceptions are listed here
rather than left to be discovered.

**Whitespace and zero-width characters are stripped from identifiers.** Two
model names that look identical but differ by a trailing space or an invisible
character are otherwise different strings, and one model's spend would split
across two rows. This is invisible and safe: the strings genuinely are the same.

**Different spellings of the same model are combined.** If both `Claude Opus 5`
and `claude-opus-5` appear, they are rewritten to the canonical id, because the
pricing file resolves both to one entry — this is not an inference. Left alone,
that model's spend would be split and every share and ranking involving it
would be computed on a fraction of it.

This is a rewrite of your data, so it is reported in the caveats naming exactly
which strings were combined. Three limits keep it narrow: only spellings that
actually collide are touched, so a model written one consistent way keeps your
own spelling; only spellings the pricing file can resolve are merged, since
nothing proves an unrecognised string is a duplicate of anything; and cost rows
are rewritten alongside usage rows so the two halves of the report cannot
disagree about how many models there were.

The totals never move as a result. Both spellings already priced correctly —
what merging fixes is the shares.

**Nothing else.** Rows are never dropped, tokens are never redistributed
between buckets, and no value is invented to fill a gap.

On the API path specifically, **partial data is never returned**. If a page or
a window fails after its retries, the whole fetch raises. Half a month that
looks like a whole one is the worst failure available to a tool that prints a
bill, and it is the one failure a caller cannot detect for themselves.

## Verifying a figure by hand

Every rate comes from a versioned file — `pricing/<version>.json` — that records
its own fetch date and source URL. Each entry carries a `provenance` marker:
either `published` (transcribed from Anthropic's pricing page) or a stated
derivation such as `derived: 1.25 x uncached_input`. A test re-derives every
derived entry and fails if the arithmetic drifts, and a second test
independently cross-checks the transcribed per-model cache rates against the
published multipliers.

So any figure in a report can be traced:

1. The report header names the pricing file and its fetch date.
2. Each finding states its `measured_basis` — what the number actually measures.
3. Each finding's evidence table shows the token counts behind it.
4. The rates are in the pricing file, with their provenance.

If a rate is wrong for your organisation — negotiated pricing, volume discounts
— edit that file and re-run. Every figure moves with it. **No rate is ever
hardcoded in check logic.**

## Adding a check

Checks are pure functions registered in a list. Adding one never requires
touching a renderer; there is a test asserting that a check the renderer has
never heard of still renders correctly.

```python
# src/claude_spend_audit/checks/c9_example.py

def run(frame, pricing, config):
    ...
    return [Finding(check_id="C9", ..., confidence=Confidence.MEASURED)]

from . import Check

CHECK = Check(
    id="C9",
    name="Example",
    run=run,
    needs_dimensions=frozenset({"model"}),         # optional
    needs_bucket_width=frozenset({"1h"}),          # optional
    blocked_by_tokens=frozenset({"cache_write_unsplit"}),  # optional
    needs_days=lambda config: config.my_window + 1,        # optional
)
```

Then add it to `CHECKS` in `checks/__init__.py`, and add any thresholds to
`CheckConfig` so they are settable from the command line.

The preconditions are the important part, and they answer different questions:
`needs_dimensions` is about how the data is grouped, `needs_bucket_width` about
its time granularity, `needs_days` about how much of it there is (a callable,
because the answer usually depends on a threshold the user can change), and
`blocked_by_tokens` about its *content* — a source can have every dimension and
the right granularity while having destroyed the one distinction a check is
built on.

A check that declares what it needs will be skipped on data that cannot support
it, and the report will say which check was skipped and why. A check that does
not declare its needs will silently compute something meaningless.

## Known limitations

Four are worth knowing before you rely on the output.

**The Console CSV column mapping is unverified.** Anthropic publishes the Usage
& Cost API's field names but not the Console export's column headers — they are
absent from the API documentation, both endpoint references, and the support
article on Console reporting. The canonical field names used internally come
from the documented API schema and are authoritative; the mapping from Console
headers onto them lives in `schemas/console_*.json`, marked `provisional`, and
is a set of educated guesses.

The cookbook profiles are the exception. Anthropic's published notebook
(`observability/usage_cost_api.ipynb`) ships an exporter whose source is
public, so `schemas/cookbook_*.json` are marked `verified` and their column
names are copied from it rather than guessed. **If you can run that notebook,
its output is the better input** — the schema is known, and every report says
which profile it used and whether that profile is verified.

The parser does not paper over the provisional case. An unrecognised export
fails with the actual header row printed and a pointer to the file to edit; an
ambiguous match is a hard error rather than a coin flip; and unmatched columns
are reported rather than silently dropped. `--explain-schema` prints the mapping
that was applied. Correcting it is a JSON edit, not a code change.

**Some spend cannot be priced or even seen.** Beyond the unpriceable service
tiers above: code execution and Managed Agents session runtime appear only in
cost data and never in usage, so they cannot be attributed to a model. Claude
Platform on AWS has no programmatic Usage/Cost API at all, so Bedrock-routed
traffic is invisible here — and no signal in aggregate data would reveal its
absence, so the tool states this as a standing caveat rather than pretending to
detect it.

Long context is a narrower case of the same thing. The pricing page confirms
that the full 1M window bills at standard rates on Claude 4.6 and later, and
says nothing about earlier models that offered it in beta. Traffic in the
200k-1M window on one of those is priced at the standard rate — the only figure
the rate card supports — and reported as unconfirmed rather than presented as
settled.

**Some questions this data shape cannot answer at all.** The usage report
carries no request counts, so C5 cannot tell ten long responses from a thousand
short ones, which is the distinction `max_tokens` discipline is actually about.
Thinking tokens are billed as output and are not broken out anywhere in the
report, so an output-heavy segment may be reasoning rather than verbosity. And
the frontier/mid/small tiers C8 groups by are this tool's classification,
recorded in the pricing file rather than published by Anthropic — the file says
so, and you can edit it to match how your organisation thinks about its models.

**The Team/Enterprise spend report supports description, not diagnosis.** It
carries no dates, so nothing about spikes or off-hours; no cache breakdown, so
nothing about caching; and its documentation does not say whether prompt
tokens include cache reads. Its column names are only partly documented, so
`schemas/enterprise_spend_csv.json` is a provisional set of many candidate
spellings per field. Spend is the report's own figure and is never re-priced:
the pricing file supplies only model tiers and list prices on this path.

Every limitation that applies to a given dataset is listed in that report's
caveats section, with what to re-export to remove it where that is possible.

## Dependencies

There are none at runtime. `pytest` is the only development dependency.

This is a deliberate constraint rather than minimalism for its own sake: the
tool is meant to be run by people on their own billing data, and an empty
dependency list is something a security-conscious reader can verify in a few
minutes.
