"""`fetch --source claude-code`, then `audit`, `inspect` and `describe` on it."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from claude_spend_audit.cli import main
from claude_spend_audit.describe import describe
from claude_spend_audit.ingest.api_client import ApiClient

from . import _claude_code as C
from ._transport import FakeTransport, no_sleep, ok

KEY = "sk-ant-admin01-SECRETVALUE123"
PRICING = ["--pricing", "pricing/2026-09.json"]


@pytest.fixture
def saved(tmp_path):
    def write(**kw):
        path = tmp_path / "claude_code.json"
        path.write_text(json.dumps(C.fetched(**kw)), encoding="utf-8")
        return str(path)

    return write


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# -- fetch ---------------------------------------------------------------------------


def _patch(monkeypatch, transport):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", KEY)
    original = ApiClient.__init__

    def patched(self, *a, **kw):
        kw["transport"] = transport
        kw["sleep"] = no_sleep
        original(self, *a, **kw)

    monkeypatch.setattr(ApiClient, "__init__", patched)


def test_fetch_writes_one_entry_per_day(tmp_path, monkeypatch, capsys):
    transport = FakeTransport(*(ok(C.page(C.records(d))) for d in range(3)))
    _patch(monkeypatch, transport)
    out = tmp_path / "cc.json"
    code, _, err = run(capsys, "fetch", "--source", "claude-code",
                       "--from", "2026-08-03", "--to", "2026-08-06", "-o", str(out))
    assert code == 0, err
    saved = json.loads(out.read_text())
    assert saved["kind"] == "claude-spend-audit/claude-code-fetch"
    assert [d["date"] for d in saved["days"]] == ["2026-08-03", "2026-08-04", "2026-08-05"]
    assert KEY not in out.read_text()
    assert "2026-08-04: 1 page(s)" in err
    assert "email addresses" in err


@pytest.mark.parametrize("flag", [["--what", "cost"], ["--bucket-width", "1h"],
                                  ["--group-by", "model"], ["--no-users"], ["--no-names"]])
def test_flags_that_do_not_apply_are_refused(tmp_path, monkeypatch, capsys, flag):
    _patch(monkeypatch, FakeTransport())
    code, _, err = run(capsys, "fetch", "--source", "claude-code", "--from", "2026-08-03",
                       "--to", "2026-08-04", "-o", str(tmp_path / "x.json"), *flag)
    assert code == 2 and "does not apply" in err


# -- audit and inspect ------------------------------------------------------------------


def test_audit_runs_every_applicable_check(saved, capsys):
    code, out, err = run(capsys, "audit", saved(), *PRICING, "--json")
    payload = json.loads(out)
    ids = {f["check_id"] for f in payload["findings"]}
    assert {"C2", "C8", "E1", "E3", "K1", "K2", "K3"} <= ids
    assert "E2" not in ids
    assert payload["anthropic_estimate_usd"] == "152.25"
    codes = {g["code"] for g in payload["gaps"]}
    assert {"claude_code_estimates", "claude_code_shape", "cache_ttl_not_split",
            "subscription_not_billed_per_token"} <= codes
    assert "claude_code_input_unconfirmed" not in codes
    # Advice this source can never follow is not given.
    assert "dimension_not_grouped" not in codes and "granularity_too_coarse" not in codes


def test_a_disagreeing_estimate_is_flagged(saved, capsys):
    """Anthropic's API-row estimate inflated 40%: $150.15 against our $107.25."""
    _, out, _ = run(capsys, "audit", saved(inflate=Decimal("1.4")), *PRICING, "--json")
    gap = next(g for g in json.loads(out)["gaps"] if g["code"] == "claude_code_input_unconfirmed")
    assert gap["scope"] == "$107.25 vs $150.15"


def test_anonymize_uses_one_mapping_everywhere(saved, capsys):
    """Alice is the largest spender, so user-001 in E1, K1 and K2 alike."""
    _, out, _ = run(capsys, "audit", saved(), *PRICING, "--json", "--anonymize")
    assert "alice@example.com" not in out and "ci-bot" not in out
    findings = {f["check_id"]: f for f in json.loads(out)["findings"]}
    assert findings["E1"]["evidence"]["rows"][0][0] == "user-001"
    assert findings["K1"]["evidence"]["rows"][-1][0] == "user-001"
    assert findings["K2"]["evidence"]["rows"][0][0] == "user-001"


def test_inspect_says_what_would_run(saved, capsys):
    code, out, _ = run(capsys, "inspect", saved(), *PRICING)
    assert "Claude Code summary" in out
    assert "would run on this data" in out


def test_markdown_renders(saved, capsys, tmp_path):
    target = tmp_path / "report.md"
    run(capsys, "audit", saved(), *PRICING, "--format", "markdown", "-o", str(target))
    text = target.read_text()
    assert "K1" in text and "estimated cost" in text


# -- describe -----------------------------------------------------------------------------


def test_describe_prints_structure_and_no_people(saved):
    out = describe(saved())
    assert "saved fetch: Claude Code Analytics API, 3 day(s)" in out
    assert "tool_actions.edit_tool.accepted: number" in out
    for leaked in ("alice", "carol", "ci-bot", "1075", "7825", "00000000"):
        assert leaked not in out


def test_anonymize_covers_people_with_no_model_usage(tmp_path, capsys):
    """Dave has edit decisions but no tokens, so no SpendReport row. He must
    still get a label: 35 proposals, 14% accepted, so K1 lists him."""
    env = C.fetched()
    env["days"][0]["pages"][0]["data"].append(
        C._record(0, {"type": "user_actor", "email_address": "dave@example.com"}, "api", 2,
                  C._tools(edit=(5, 30)), [])
    )
    path = tmp_path / "cc.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    _, out, _ = run(capsys, "audit", str(path), *PRICING, "--json", "--anonymize")
    assert "dave@" not in out
    k1 = next(f for f in json.loads(out)["findings"] if f["check_id"] == "K1")
    assert "user-004" in {row[0] for row in k1["evidence"]["rows"]}


def test_a_clean_file_exits_zero(saved, capsys):
    """Summed cache writes are a caveat here, not an understated bill: this
    source has no bill, and Anthropic's estimate knows the TTLs."""
    code, out, _ = run(capsys, "audit", saved(), *PRICING, "--json")
    assert code == 0
    gap = next(g for g in json.loads(out)["gaps"] if g["code"] == "cache_ttl_not_split")
    assert not gap["invalidates_total"]


def test_spend_limit_advice_fits_a_console_organisation(saved, capsys):
    _, out, _ = run(capsys, "audit", saved(), *PRICING, "--json")
    e1 = next(f for f in json.loads(out)["findings"] if f["check_id"] == "E1")
    assert "workspaces can carry spend limits" in e1["remedy"]
    assert "Team and Enterprise" not in e1["remedy"]
