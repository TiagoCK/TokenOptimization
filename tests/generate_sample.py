"""Regenerate the synthetic month and the sample report built from it.

    python -m tests.generate_sample

Writes examples/sample-usage.csv and examples/sample-cost.csv first, then
examples/sample-report.md from those files -- so the report is always the
output of the data sitting next to it.
"""

from tests._sample import (
    SAMPLE,
    SAMPLE_COST,
    SAMPLE_USAGE,
    build,
    build_cost_csv,
    build_usage_csv,
)

if __name__ == "__main__":
    SAMPLE_USAGE.write_text(build_usage_csv(), encoding="utf-8", newline="\n")
    SAMPLE_COST.write_text(build_cost_csv(), encoding="utf-8", newline="\n")
    SAMPLE.write_text(build(), encoding="utf-8", newline="\n")
    for path in (SAMPLE_USAGE, SAMPLE_COST, SAMPLE):
        print(f"wrote {path} ({path.stat().st_size:,} bytes)")
