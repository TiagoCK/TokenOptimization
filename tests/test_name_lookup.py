"""Workspace and API-key names, in place of bare IDs.

The Admin API's list endpoints give each workspace and key a name. The tool
keeps exactly id -> name from them -- never the partial key hint, the creator
or the principal -- and uses it only to label findings. A name never changes
a figure, and `--anonymize` takes the names away again, since key names
often name people.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_spend_audit.checks import run_checks
from claude_spend_audit.cli import main
from claude_spend_audit.describe import describe
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow, merge
from claude_spend_audit.ingest.api_client import (
    ApiClient,
    AuthError,
    Credential,
    ProtocolError,
    envelope,
)

from ._transport import FakeTransport, error, no_sleep, ok, page

KEY = "sk-ant-admin01-SECRETVALUE123"
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 8, tzinfo=timezone.utc)

WORKSPACE = {"id": "wrkspc_main", "name": "Production", "type": "workspace",
             "archived_at": None, "display_color": "#6C5BB9"}
KEY_ITEM = {"id": "apikey_main", "name": "alice-laptop", "type": "api_key",
            "status": "active", "partial_key_hint": "sk-ant-api03-R2D...igAA",
            "created_by": {"id": "user_01", "type": "user"},
            "principal": {"type": "user_actor", "user_id": "user_01"}}


def listing(items, *, has_more=False, last_id=None):
    return {"data": items, "has_more": has_more, "first_id": None,
            "last_id": last_id or (items[-1]["id"] if items else None)}


def client(transport) -> ApiClient:
    return ApiClient(Credential(KEY), transport=transport, sleep=no_sleep)


def query(request) -> dict:
    return parse_qs(urlsplit(request.url).query)


# -- the client ------------------------------------------------------------------


def test_workspace_names_follow_the_cursor_and_include_archived():
    archived = {**WORKSPACE, "id": "wrkspc_old", "name": "Old prototype",
                "archived_at": "2026-01-01T00:00:00Z"}
    transport = FakeTransport(
        ok(listing([WORKSPACE], has_more=True)),
        ok(listing([archived])),
    )
    names = client(transport).workspace_names()
    assert names == {"wrkspc_main": "Production", "wrkspc_old": "Old prototype"}
    first, second = (query(r) for r in transport.requests)
    assert urlsplit(transport.requests[0].url).path == "/v1/organizations/workspaces"
    assert first["include_archived"] == ["true"] and first["limit"] == ["1000"]
    assert "after_id" not in first and second["after_id"] == ["wrkspc_main"]


def test_api_key_names_keep_the_name_and_nothing_else():
    transport = FakeTransport(ok(listing([KEY_ITEM])))
    names = client(transport).api_key_names()
    assert names == {"apikey_main": "alice-laptop"}
    assert "status" not in query(transport.last)  # every status: old keys still bill


def test_a_list_that_never_ends_is_refused():
    transport = FakeTransport(ok(listing([WORKSPACE], has_more=True, last_id="wrkspc_main")),
                              ok(listing([WORKSPACE], has_more=True, last_id="wrkspc_main")))
    with pytest.raises(ProtocolError, match="last_id"):
        client(transport).workspace_names()


def test_a_key_without_list_permission_raises_auth_error():
    with pytest.raises(AuthError):
        client(FakeTransport(error(403, "forbidden"))).workspace_names()


# -- fetch -----------------------------------------------------------------------------


def _patch(monkeypatch, transport):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", KEY)
    original = ApiClient.__init__

    def patched(self, *a, **kw):
        kw["transport"] = transport
        kw["sleep"] = no_sleep
        original(self, *a, **kw)

    monkeypatch.setattr(ApiClient, "__init__", patched)


FETCH = ["fetch", "--from", "2026-08-01", "--to", "2026-08-08"]


def test_fetch_saves_names_and_nothing_else_about_keys(tmp_path, monkeypatch, capsys):
    transport = FakeTransport(ok(page()), ok(listing([WORKSPACE])), ok(listing([KEY_ITEM])))
    _patch(monkeypatch, transport)
    out = tmp_path / "raw.json"
    assert main([*FETCH, "-o", str(out)]) == 0
    text = out.read_text()
    saved = json.loads(text)
    assert saved["names"] == {"workspace_id": {"wrkspc_main": "Production"},
                              "api_key_id": {"apikey_main": "alice-laptop"}}
    for dropped in ("partial_key_hint", "R2D", "user_01", "display_color"):
        assert dropped not in text
    assert KEY not in text


def test_no_names_skips_the_lookup(tmp_path, monkeypatch, capsys):
    transport = FakeTransport(ok(page()))
    _patch(monkeypatch, transport)
    out = tmp_path / "raw.json"
    assert main([*FETCH, "-o", str(out), "--no-names"]) == 0
    assert transport.calls == 1
    assert "names" not in json.loads(out.read_text())


def test_a_refused_lookup_still_saves_the_usage(tmp_path, monkeypatch, capsys):
    transport = FakeTransport(ok(page()), error(403, "forbidden"))
    _patch(monkeypatch, transport)
    out = tmp_path / "raw.json"
    assert main([*FETCH, "-o", str(out)]) == 0
    assert "could not look up workspace and API-key names" in capsys.readouterr().err
    saved = json.loads(out.read_text())
    assert "names" not in saved and saved["pages"]


# -- labels ------------------------------------------------------------------------------


def _saved(tmp_path, names) -> str:
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(envelope(
        endpoint="usage", starting_at=START, ending_at=END, bucket_width="1d",
        group_by=["model", "workspace_id", "api_key_id"],
        pages=[page(uncached=0, output=0) | {"data": [{
            "starting_at": "2026-08-01T00:00:00Z", "ending_at": "2026-08-02T00:00:00Z",
            "results": [{
                "model": "claude-opus-5", "workspace_id": "wrkspc_main",
                "api_key_id": "apikey_main", "uncached_input_tokens": 50_000_000,
                "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                   "ephemeral_1h_input_tokens": 0},
                "output_tokens": 100_000,
            }],
        }]}],
        version="0.1.0", names=names,
    )), encoding="utf-8")
    return str(path)


NAMES = {"workspace_id": {"wrkspc_main": "Production"},
         "api_key_id": {"apikey_main": "alice-laptop"}}


def _c2_label(capsys, *argv) -> str:
    """The C2 evidence label, read whole from --json (the terminal table
    shortens long labels to fit its width)."""
    main(["audit", *argv, "--json"])
    payload = json.loads(capsys.readouterr().out)
    c2 = next(f for f in payload["findings"] if f["check_id"] == "C2")
    return c2["evidence"]["rows"][0][0]


def test_findings_show_names(tmp_path, capsys):
    """50M uncached Opus input with no cache reads: C2 names its segment."""
    label = _c2_label(capsys, _saved(tmp_path, NAMES))
    assert label == "claude-opus-5 / Production [...main] / alice-laptop [...main]"


def test_inspect_lists_names_under_dimensions(tmp_path, capsys):
    main(["inspect", _saved(tmp_path, NAMES)])
    out = capsys.readouterr().out
    dims = out.split("DIMENSIONS PRESENT", 1)[1]
    assert "Production [...main]" in dims and "alice-laptop [...main]" in dims


def test_anonymize_brings_the_ids_back(tmp_path, capsys):
    label = _c2_label(capsys, _saved(tmp_path, NAMES), "--anonymize")
    assert label == "claude-opus-5 / wrkspc_main / apikey_main"


def test_without_names_labels_are_unchanged(tmp_path, capsys):
    label = _c2_label(capsys, _saved(tmp_path, None))
    assert label == "claude-opus-5 / wrkspc_main / apikey_main"


def test_names_never_change_a_figure(pricing):
    seg = Segment(model="claude-opus-5", workspace_id="wrkspc_main")
    start = START
    row = UsageRow(start=start, end=start.replace(day=2), segment=seg,
                   tokens=TokenBuckets(uncached_input=50_000_000, output=100_000))
    plain = Frame(usage=(row,), grouped_by=frozenset({"model", "workspace_id"}))
    named = Frame(usage=(row,), grouped_by=plain.grouped_by, names=NAMES)
    a, b = run_checks(plain, pricing), run_checks(named, pricing)
    assert [f.measured_usd for f in a.findings] == [f.measured_usd for f in b.findings]
    assert [f.recoverable for f in a.findings] == [f.recoverable for f in b.findings]


def test_merge_unions_names():
    a = Frame(names={"workspace_id": {"w1": "One"}})
    b = Frame(names={"workspace_id": {"w2": "Two"}, "api_key_id": {"k": "Key"}})
    merged = merge(a, b)
    assert merged.names == {"workspace_id": {"w1": "One", "w2": "Two"},
                            "api_key_id": {"k": "Key"}}


def test_describe_counts_names_and_prints_none(tmp_path):
    out = describe(_saved(tmp_path, NAMES))
    assert "names: 1 workspace(s), 1 API key(s)" in out
    assert "Production" not in out and "alice-laptop" not in out
