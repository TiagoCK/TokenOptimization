# claude-spend-audit

**Everything runs on your machine. No data leaves it.** There is no hosted
service, no telemetry, no uploads, and no network access of any kind in the CSV
path. You export a CSV from the Claude Console, run this against it, and read
the output. Nothing is sent anywhere. The dependency list is empty so you can
verify that claim yourself in about five minutes.

A CLI that reads Anthropic Usage & Cost data and reports where the money is
going, with dollar estimates you can trace to a file you control.

---

## Status: M1 (ingest)

Ingest, normalization, and pricing only. **No analysis yet** — the checks land in
M2. What works today:

- Console CSV exports (Usage and Cost pages) parsed into one internal shape
- The same shape produced from Usage & Cost API JSON, so later checks never need
  to know which path the data came from
- A pricing table loaded from a versioned, inspectable file
- `inspect`, which prints what the data covers and — just as importantly — what
  it cannot show

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
claude-spend-audit inspect usage.csv --cost cost.csv
```

Useful flags:

- `--explain-schema` — print exactly which CSV column mapped to which field
- `--json` — machine-readable output
- `--pricing pricing/2026-09.json` — use a specific rate card

Exit code is `1` when something in the data means the headline total understates
the real bill, so a script wrapping this cannot mistake a caveated number for a
clean one.

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
