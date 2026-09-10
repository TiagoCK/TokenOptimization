"""Regenerate examples/sample-report.md.

    python -m tests.generate_sample
"""

from tests._sample import SAMPLE, build

if __name__ == "__main__":
    SAMPLE.write_text(build(), encoding="utf-8")
    print(f"wrote {SAMPLE}")
