"""`fetch --source analytics`, and `audit` / `inspect` on what it saves."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from claude_spend_audit.cli import DEFAULT_ANALYTICS_GROUP_BY, main
from claude_spend_audit.ingest.api_client import ApiClient, is_analytics_envelope

from . import _analytics as A
from ._transport import FakeTransport, no_sleep, ok

KEY = "sk-ant-api01-ANALYTICSSECRET987"


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def saved(tmp_path, **kw):
    path = tmp_path / "analytics.json"
    path.write_text(json.dumps(A.fetched(**kw)), encoding="utf-8")
    return str(path)


@pytest.fixture
def patched_client(monkeypatch):
    """Route every ApiClient through a scripted transport and a fixed clock."""
    holder = {}

    def install(*responses):
        transport = FakeTransport(*responses)
        holder["transport"] = transport
        real_init = ApiClient.__init__

        def patched(self, credential, **kw):
            kw["transport"] = transport
            kw["sleep"] = no_sleep
            kw["clock"] = lambda: datetime(2026, 10, 3, tzinfo=timezone.utc)
            real_init(self, credential, **kw)

        monkeypatch.setattr(ApiClient, "__init__", patched)
        return transport

    return install


def _pages():
    """One page per series, in the order fetch requests them."""
    return [
        ok(A.page(A.grouped_usage())),
        ok(A.page(A.total_usage())),
        ok(A.page(A.total_cost())),
        ok(A.user_usage_pages()[1]),
        ok(A.user_cost_pages()[1]),
    ]


# -- fetch -------------------------------------------------------------------


def test_fetch_writes_every_series_without_the_key(tmp_path, monkeypatch, capsys, patched_client):
    transport = patched_client(*_pages())
    monkeypatch.setenv("ANTHROPIC_ANALYTICS_KEY", KEY)
    out = tmp_path / "raw.json"
    code, _, err = run(
        capsys, "fetch", "--source", "analytics", "--from", "2026-08-01", "--to", "2026-08-11", "-o", str(out)
    )
    assert code == 0, err
    assert transport.calls == 5
    text = out.read_text(encoding="utf-8")
    assert KEY not in text
    payload = json.loads(text)
    assert is_analytics_envelope(payload)
    assert set(payload["series"]) == {"usage", "usage_total", "cost_total", "user_usage", "user_cost"}
    assert payload["series"]["usage"]["group_by"] == list(DEFAULT_ANALYTICS_GROUP_BY)
    assert payload["series"]["usage_total"]["group_by"] == []


def test_no_users_saves_no_per_user_series(tmp_path, monkeypatch, capsys, patched_client):
    transport = patched_client(*_pages()[:3])
    monkeypatch.setenv("ANTHROPIC_ANALYTICS_KEY", KEY)
    out = tmp_path / "raw.json"
    code, _, _ = run(
        capsys, "fetch", "--source", "analytics", "--no-users",
        "--from", "2026-08-01", "--to", "2026-08-11", "-o", str(out),
    )
    assert code == 0
    assert transport.calls == 3
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "user_usage" not in payload["series"]
    assert "@" not in json.dumps(payload)


def test_fetch_reads_the_analytics_env_var_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_ANALYTICS_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin01-WRONGKIND")
    code, _, err = run(
        capsys, "fetch", "--source", "analytics", "--from", "2026-08-01", "--to", "2026-08-11",
        "-o", str(tmp_path / "x.json"),
    )
    assert code == 2
    assert "no Analytics API key found in $ANTHROPIC_ANALYTICS_KEY" in err


def test_fetch_refuses_rbac_grouping(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_ANALYTICS_KEY", KEY)
    code, _, err = run(
        capsys, "fetch", "--source", "analytics", "--group-by", "model,rbac_group_id",
        "--from", "2026-08-01", "--to", "2026-08-11", "-o", str(tmp_path / "x.json"),
    )
    assert code == 2
    assert "more than once" in err


# -- audit -------------------------------------------------------------------


def test_audit_runs_usage_and_per_user_checks(tmp_path, capsys):
    code, out, err = run(capsys, "audit", saved(tmp_path), "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["source"] == "enterprise_analytics"
    assert payload["per_user"] is True
    found = {f["check_id"] for f in payload["findings"]}
    assert {"C1", "C2", "C8", "E1", "E2", "E3"} <= found
    assert "Batch tier adoption (C3)" in payload["checks_blocked"]
    assert "Attribution coverage (C7)" in payload["checks_blocked"]
    assert "Day-over-day spend spikes (C6a)" in payload["checks_clean"]
    assert Decimal(payload["spend"]["billed_usd"]) == Decimal("160")
    assert Decimal(payload["spend"]["estimated_usd"]) == Decimal("170")


def test_c1_figure_by_hand(tmp_path, capsys):
    """Opus 1h writes, never read: 5M x $10 = $50 paid against 5M x $5 = $25
    uncached. Waste $25.00."""
    _, out, _ = run(capsys, "audit", saved(tmp_path), "--json")
    c1 = next(f for f in json.loads(out)["findings"] if f["check_id"] == "C1")
    assert Decimal(c1["measured_usd"]) == Decimal("25")


def test_caveats_that_cannot_apply_are_dropped(tmp_path, capsys):
    _, out, _ = run(capsys, "audit", saved(tmp_path), "--json")
    gaps = json.loads(out)["gaps"]
    codes = {g["code"] for g in gaps}
    assert "pricing_dimension_not_grouped" not in codes
    assert not any(g["code"] == "dimension_not_grouped" and g["scope"] == "service_tier" for g in gaps)
    assert "analytics_plan_context" in codes


def test_terminal_and_markdown_show_list_price(tmp_path, capsys):
    path = saved(tmp_path)
    _, out, _ = run(capsys, "audit", path)
    assert "list price (from cost data)" in out
    assert "billed is 80.0% of list price" in out
    _, md, _ = run(capsys, "audit", path, "--format", "markdown")
    assert "| List price | $200.00 | before discounts; billed is 80.0% of it |" in md


@pytest.mark.parametrize("extra", [(), ("--json",), ("--format", "markdown")])
def test_anonymize_leaves_no_email(tmp_path, capsys, extra):
    code, out, _ = run(capsys, "audit", saved(tmp_path), "--anonymize", *extra)
    assert code == 0
    assert "@" not in out


def test_truncation_sets_the_exit_code(tmp_path, capsys):
    code, out, _ = run(capsys, "audit", saved(tmp_path, drop_sonnet_on=(3, 7)))
    assert code == 1
    assert "top 100 groups" in " ".join(out.split())


def test_inspect_counts_checks_across_both_registries(tmp_path, capsys):
    code, out, _ = run(capsys, "inspect", saved(tmp_path))
    assert code == 0
    # 9 usage checks + 4 per-user; C3, C6b and C7 cannot run on daily data
    # without tier or workspace.
    assert "No checks run. 10 of 13 would run" in out


def test_cost_flag_is_refused(tmp_path, capsys):
    code, _, err = run(capsys, "audit", saved(tmp_path), "--cost", "x.csv")
    assert code == 2
    assert "already carries cost data" in err
