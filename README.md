# claude-spend-audit

**Everything runs on your machine. No data leaves it.** There is no hosted
service, no telemetry, no uploads, and no network access of any kind in the CSV
path. You export a CSV from the Claude Console, run this against it, and read
the output. Nothing is sent anywhere. The dependency list is empty so you can
verify that claim yourself in about five minutes.

A CLI that reads Anthropic Usage & Cost data and reports where the money is
going, with dollar estimates you can trace to a file you control.

---

See [`examples/sample-report.md`](examples/sample-report.md) for what the output
looks like, generated from synthetic data.

## What it checks

| | check | confidence |
|---|---|---|
| **C1** | Cache writes not read back often enough to pay for themselves | `measured` |
| **C2** | High uncached input with almost no cache reads | `candidate` |
| **C3** | Standard-tier spend that could take the 50% batch discount | `candidate` |
| **C6a** | Daily spend spikes above the recent baseline | `measured` |
| **C6b** | Spend falling outside business hours | `measured` |
| **C7** | Spend that cannot be allocated to a named workspace or key | `observation` |

A `measured` finding is arithmetic on your data. A `candidate` is a pattern the
data cannot confirm, and always carries a range with its assumptions attached
rather than a single number. An `observation` makes no savings claim at all.

The remaining checks (C4, C5, C8) and the Admin API ingest path are not built
yet.

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

For the shareable version:

```bash
claude-spend-audit audit usage.csv --cost cost.csv --format markdown -o report.md
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
- `--cache-prefix-share`, `--batch-eligible-share` — narrow C2's and C3's
  savings ranges from your own knowledge of the workload. Both default to a
  floor of $0, because guessing them would put an invented number in the report.

Exit code is `1` when something in the data means the headline total understates
the real bill, so a script wrapping this cannot mistake a caveated number for a
clean one.

## How findings are ranked, and what a dollar figure means

Findings are ranked by confidence first, dollars second, and two different
numbers are kept apart throughout: **measured** is what was spent and is
arithmetic; **recoverable** is what could actually be saved and is often
unknowable from aggregate data. C6 can measure that $40,000 was spent overnight
without being able to say whether any of it was avoidable, so it reports the
first and prints `not claimed` for the second.

[`ARCHITECTURE.md`](ARCHITECTURE.md) explains the confidence model in full,
along with the invariants that keep the numbers honest and how to verify any
figure by hand.

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
