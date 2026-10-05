"""Regenerate the synthetic month and the sample report built from it.

    python -m tests.generate_sample

Writes examples/sample-usage.csv and examples/sample-cost.csv first, then
examples/sample-report.md from those files -- so the report is always the
output of the data sitting next to it. The Enterprise samples follow the same
order: the analytics fetch and spend report first, then their two reports.
"""

from tests._sample import (
    SAMPLE,
    SAMPLE_COST,
    SAMPLE_USAGE,
    build,
    build_cost_csv,
    build_usage_csv,
)
from tests import _sample_enterprise as E

if __name__ == "__main__":
    SAMPLE_USAGE.write_text(build_usage_csv(), encoding="utf-8", newline="\n")
    SAMPLE_COST.write_text(build_cost_csv(), encoding="utf-8", newline="\n")
    SAMPLE.write_text(build(), encoding="utf-8", newline="\n")
    E.ANALYTICS.write_text(E.build_analytics(), encoding="utf-8", newline="\n")
    E.SPEND.write_text(E.build_spend_csv(), encoding="utf-8", newline="\n")
    E.ANALYTICS_REPORT.write_text(E.build_analytics_report(), encoding="utf-8", newline="\n")
    E.SPEND_REPORT.write_text(E.build_spend_report(), encoding="utf-8", newline="\n")
    for path in (SAMPLE_USAGE, SAMPLE_COST, SAMPLE, E.ANALYTICS, E.SPEND,
                 E.ANALYTICS_REPORT, E.SPEND_REPORT):
        print(f"wrote {path} ({path.stat().st_size:,} bytes)")
