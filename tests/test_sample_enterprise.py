"""The committed Enterprise samples: two inputs and the reports built from them.

They exist so someone can try the Enterprise paths and read the output before
pointing the tool at real data. The rules are the same as for the API
sample: the committed files are exactly what the generator produces, the
reports are exactly what the CLI makes of the inputs, and every check the
source supports has something to say.
"""

from __future__ import annotations

import json

import pytest

from claude_spend_audit.cli import main

from . import _sample_enterprise as E


@pytest.mark.parametrize(
    "path, build",
    [
        (E.ANALYTICS, E.build_analytics),
        (E.SPEND, E.build_spend_csv),
        (E.ANALYTICS_REPORT, E.build_analytics_report),
        (E.SPEND_REPORT, E.build_spend_report),
    ],
    ids=lambda v: getattr(v, "name", ""),
)
def test_committed_file_matches_the_generator(path, build):
    assert path.read_text(encoding="utf-8") == build(), (
        f"{path.name} is stale. Regenerate with: python -m tests.generate_sample"
    )


def _findings(capsys, path) -> tuple[int, dict]:
    code = main(["audit", str(path), "--pricing", str(E.PRICING), "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_the_analytics_sample_exercises_every_check_it_can(capsys):
    code, payload = _findings(capsys, E.ANALYTICS)
    assert code == 0
    ids = {f["check_id"] for f in payload["findings"]}
    assert ids == {"C1", "C2", "C4", "C5", "C6a", "C8", "C7r", "E1", "E2", "E3", "E4"}


def test_the_spend_report_sample_exercises_every_check(capsys):
    code, payload = _findings(capsys, E.SPEND)
    assert code == 0
    assert {f["check_id"] for f in payload["findings"]} == {"E1", "E2", "E3", "E4"}


def test_the_two_inputs_describe_the_same_month(capsys):
    """Net spend per person agrees between the API file and the download."""
    _, analytics = _findings(capsys, E.ANALYTICS)
    _, spend = _findings(capsys, E.SPEND)
    top = lambda p: next(f for f in p["findings"] if f["check_id"] == "E1")["evidence"]["rows"][0]
    assert top(analytics)[0] == top(spend)[0] == "priya@example.com"
    assert top(analytics)[-2] == top(spend)[-2] == "$772.22"
