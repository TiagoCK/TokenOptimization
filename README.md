# claude-spend-audit

**Everything runs on your machine. No data leaves it.** There is no hosted
service, no telemetry, no uploads, and no network access of any kind in the CSV
path. You export a CSV from the Claude Console, run this against it, and read
the output. Nothing is sent anywhere. The dependency list is empty so you can
verify that claim yourself in about five minutes.

A CLI that reads Anthropic Usage & Cost data and reports where the money is
going, with dollar estimates you can trace to a file you control.

---

## Status: M2 (the first two checks)

- **C1, cache write amortization.** Finds prompt-cache traffic that costs more
  than it saves, and says by how much. Arithmetic, not an estimate.
- **C6a/C6b, spikes and off-hours volume.** Finds days that cost several times a
  typical one, and spend that lands outside business hours.
- Console CSV exports (Usage and Cost pages) parsed into one internal shape
- The same shape produced from Usage & Cost API JSON, so checks never need to
  know which path the data came from
- A pricing table loaded from a versioned, inspectable file
- `inspect`, which prints what the data covers and what it cannot show

C2, C3 and C7, plus the shareable Markdown report, arrive in M3.

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

```bash
claude-spend-audit audit usage.csv --cost cost.csv
```

`audit` runs the checks and prints ranked findings. `inspect` does the same
loading but runs nothing, reporting only what the data covers and which checks
it can support.

Useful flags:

- `--explain-schema` — print exactly which CSV column mapped to which field
- `--json` — machine-readable output
- `--pricing pricing/2026-09.json` — use a specific rate card
- `--utc-offset -5` — set your local business day for the off-hours check
- `--business-hours 08:00-17:00` — set the window it measures against
- `--min-impact 50` — drop findings below a dollar threshold

Exit code is `1` when something in the data means the headline total understates
the real bill, so a script wrapping this cannot mistake a caveated number for a
clean one.

## How findings are ranked, and what a dollar figure means

Every finding carries a confidence level, and the ranked table is ordered by
confidence first, dollars second. A `measured` finding is arithmetic on your
data. A `candidate` is a pattern consistent with waste that the data cannot
confirm, and it never carries a point estimate — only a range with the
assumption stated beside it. An `observation` makes no savings claim at all.

Two different numbers are also kept apart. **Measured** is what was spent, and
is arithmetic. **Recoverable** is what could actually be saved, and is often
unknowable from aggregate data. C6 can measure that $40,000 was spent overnight
without being able to say whether any of it was avoidable — so it reports the
first and prints `not claimed` for the second. Only C1 carries a point
recoverable, because switching caching off recovers exactly the waste it
measures.

## Two things worth knowing before you trust a number

**The CSV column mapping is unverified.** Anthropic publishes the Usage & Cost
API's field names but not the Console CSV export's column headers. So the
header-to-field mapping lives in `schemas/*.json`, marked `provisional`, and the
parser refuses to guess: an unrecognised export fails with the actual header row
printed and a pointer to the file to edit. If your export doesn't parse, fix the
JSON — no code change needed — and please open an issue with the header row so
the shipped profile can be corrected.

**Not all spend can be priced.** Anthropic publishes rates for the standard and
batch service tiers only. Traffic on `priority`, `priority_on_demand`, `flex`, or
`flex_discount` is counted in tokens but excluded from the estimate rather than
priced at zero, and the report says so loudly. Priority Tier costs are absent
from the cost endpoint entirely, so a billed total that includes them understates
the real bill by an amount this tool cannot determine.

The same principle runs throughout: where the data cannot support a claim, the
tool says so instead of estimating. Every rate it does use is in
[`pricing/2026-09.json`](pricing/2026-09.json), with the source URL and fetch
date recorded, and each entry marked either as published or with the derivation
that produced it. If a rate is wrong for your organization — negotiated pricing,
volume discounts — edit that file and the numbers change accordingly.

## Development

```bash
python -m pytest -q
```

Every check is a pure function of the normalized data, and every dollar figure
in the tests is hand-calculated in a comment beside it, so a changed formula
fails a test rather than quietly changing someone's number.

## License

MIT
