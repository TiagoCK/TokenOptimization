"""The `fetch` command and the saved-artifact round trip.

The workflow this protects: fetch once, then analyse offline as many times as
you like. One network round trip, a file the prospect can inspect before
sharing anything, and reproducible re-runs at different thresholds.

The credential tests here matter as much as the ones in test_api_client.py --
the saved file is the artifact most likely to be forwarded to someone else.
"""

import json
from datetime import datetime, timezone

import pytest

from claude_spend_audit.cli import main
from claude_spend_audit.ingest.api_client import (
    ApiClient,
    Credential,
    envelope,
    is_envelope,
)

from ._transport import FakeTransport, no_sleep, ok, page

KEY = "sk-ant-admin01-SECRETVALUE123"
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 8, tzinfo=timezone.utc)


def saved(tmp_path, pages=None, *, group_by=("model", "workspace_id"), endpoint="usage"):
    """Write a fetch envelope the way the command would."""
    path = tmp_path / f"{endpoint}.json"
    path.write_text(
        json.dumps(
            envelope(
                endpoint=endpoint,
                starting_at=START,
                ending_at=END,
                bucket_width="1d" if endpoint == "usage" else "1d",
                group_by=list(group_by),
                pages=pages if pages is not None else [page()],
                version="0.1.0",
                fetched_at=START,
            )
        ),
        encoding="utf-8",
    )
    return path


# ==========================================================================
# The envelope
# ==========================================================================


def test_the_envelope_records_the_requested_group_by():
    """The load-bearing field. A response cannot report which dimensions were
    requested -- an ungrouped one and a genuinely-null one both come back as
    null -- so without this, re-reading would have to guess and
    Frame.grouped_by would be a lie."""
    payload = envelope(
        endpoint="usage",
        starting_at=START,
        ending_at=END,
        bucket_width="1d",
        group_by=["model", "workspace_id"],
        pages=[page()],
        version="0.1.0",
    )
    assert payload["request"]["group_by"] == ["model", "workspace_id"]


def test_the_envelope_records_its_own_provenance():
    payload = envelope(
        endpoint="usage",
        starting_at=START,
        ending_at=END,
        bucket_width="1h",
        group_by=[],
        pages=[],
        version="9.9.9",
        fetched_at=START,
    )
    assert payload["version"] == "9.9.9"
    assert payload["fetched_at"] == "2026-08-01T00:00:00Z"
    assert payload["request"]["starting_at"] == "2026-08-01T00:00:00Z"
    assert payload["request"]["ending_at"] == "2026-08-08T00:00:00Z"
    assert payload["request"]["bucket_width"] == "1h"


def test_the_envelope_never_contains_the_credential():
    """This file is the artifact most likely to be forwarded to a colleague."""
    payload = envelope(
        endpoint="usage",
        starting_at=START,
        ending_at=END,
        bucket_width="1d",
        group_by=[],
        pages=[page()],
        version="0.1.0",
    )
    assert KEY not in json.dumps(payload)
    assert "x-api-key" not in json.dumps(payload)


def test_an_envelope_is_recognisable():
    payload = envelope(
        endpoint="usage", starting_at=START, ending_at=END, bucket_width="1d",
        group_by=[], pages=[], version="0.1.0",
    )
    assert is_envelope(payload)
    assert not is_envelope({"data": []})
    assert not is_envelope([1, 2, 3])
    assert not is_envelope("nope")


# ==========================================================================
# The command -- failures
# ==========================================================================


def test_a_missing_key_explains_why_there_is_no_flag(capsys, tmp_path, monkeypatch):
    """Refusing a --api-key flag is a deliberate choice, so the error says so
    rather than leaving the user hunting for the option."""
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    code = main(
        ["fetch", "--from", "2026-08-01", "--to", "2026-08-08",
         "-o", str(tmp_path / "out.json")]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "ANTHROPIC_ADMIN_KEY" in err
    assert "export ANTHROPIC_ADMIN_KEY=" in err
    assert "shell history" in err
    assert "ps" in err


def test_an_empty_key_is_treated_as_missing(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "   ")
    code = main(
        ["fetch", "--from", "2026-08-01", "--to", "2026-08-08",
         "-o", str(tmp_path / "out.json")]
    )
    assert code == 2
    assert "no Admin API key" in capsys.readouterr().err


def test_a_custom_env_var_is_named_in_the_error(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    code = main(
        ["fetch", "--from", "2026-08-01", "--to", "2026-08-08",
         "-o", str(tmp_path / "out.json"), "--api-key-env", "MY_KEY"]
    )
    assert code == 2
    assert "$MY_KEY" in capsys.readouterr().err


def test_the_key_is_not_accepted_as_a_flag(tmp_path, capsys):
    """There is no --api-key, and the refusal must not echo the value.

    Found by this test: argparse's prefix matching originally accepted
    `--api-key` as an abbreviation of `--api-key-env`, so the secret became
    the *variable name* and was printed straight back to stderr in the
    "not found in $..." error. `allow_abbrev=False` plus this explicit
    refusal closes it.
    """
    code = main(
        ["fetch", "--from", "2026-08-01", "--to", "2026-08-08",
         "-o", str(tmp_path / "o.json"), "--api-key", KEY]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert KEY not in err, "the refusal must not echo the secret"
    assert "no --api-key flag, on purpose" in err
    assert "shell history" in err


def test_the_key_passed_as_the_env_var_name_is_not_echoed(tmp_path, capsys):
    """The other half of the same mistake: `--api-key-env sk-ant-...` puts the
    key where a variable name belongs. Caught before it reaches an error."""
    code = main(
        ["fetch", "--from", "2026-08-01", "--to", "2026-08-08",
         "-o", str(tmp_path / "o.json"), "--api-key-env", KEY]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert KEY not in err
    assert "NAME of an environment variable" in err


def test_no_flag_on_fetch_can_be_abbreviated(tmp_path, capsys):
    """allow_abbrev=False, so a partial flag is an error rather than a guess.
    This is what stops --api-key silently becoming --api-key-env."""
    with pytest.raises(SystemExit):
        main(
            ["fetch", "--fro", "2026-08-01", "--to", "2026-08-08",
             "-o", str(tmp_path / "o.json")]
        )


def test_a_bad_date_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        main(["fetch", "--from", "last-tuesday", "--to", "2026-08-08",
              "-o", str(tmp_path / "o.json")])


def test_no_file_is_written_when_the_key_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    out = tmp_path / "out.json"
    main(["fetch", "--from", "2026-08-01", "--to", "2026-08-08", "-o", str(out)])
    capsys.readouterr()
    assert not out.exists()


# ==========================================================================
# Reading a saved file back
# ==========================================================================


def test_audit_reads_a_saved_fetch(capsys, tmp_path):
    code = main(["audit", str(saved(tmp_path))])
    out = capsys.readouterr().out
    assert "claude-spend-audit -- audit" in out
    #   1,000,000 uncached @ $5.00/MTok + 200,000 output @ $25.00/MTok = $10.00
    assert "$10.00" in out
    assert code == 0


def test_inspect_reads_a_saved_fetch(capsys, tmp_path):
    main(["inspect", str(saved(tmp_path))])
    out = capsys.readouterr().out
    assert "period covered" in out
    assert "2026-08-01" in out


def test_grouped_by_comes_from_the_envelope_not_from_guessing(capsys, tmp_path):
    """The whole reason the envelope records it. Here the pages carry values
    for dimensions that were NOT requested, and the frame must respect the
    request rather than the response."""
    path = saved(tmp_path, group_by=["model"])
    main(["inspect", str(path)])
    out = " ".join(capsys.readouterr().out.split())
    assert "model 1 distinct" in out
    # service_tier has a value in the page but was not grouped by, so it must
    # not be claimed as a present dimension.
    assert "service_tier" not in out.split("DIMENSIONS PRESENT")[1].split("WHAT")[0]


def test_an_ungrouped_fetch_blocks_the_checks_that_need_dimensions(capsys, tmp_path):
    main(["audit", str(saved(tmp_path, group_by=[]))])
    out = capsys.readouterr().out
    assert "CHECKS THAT COULD NOT RUN" in out
    assert "Attribution coverage (C7)" in out
    assert "Batch tier adoption (C3)" in out


def test_a_cost_file_given_as_usage_is_refused(capsys, tmp_path):
    path = saved(tmp_path, pages=[{"data": [], "has_more": False}], endpoint="cost")
    code = main(["audit", str(path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "'cost' data" in err and "'usage'" in err


def test_a_non_envelope_json_file_falls_through_to_the_csv_path(capsys, tmp_path):
    """Detection is by content, not extension. A .json file that is not ours
    should get the CSV path's error, which names the actual header row."""
    path = tmp_path / "notours.json"
    path.write_text('{"some": "other json"}', encoding="utf-8")
    code = main(["audit", str(path)])
    assert code in (1, 2)
    assert "schema profile" in capsys.readouterr().err.lower()


def test_a_saved_fetch_and_the_csv_path_agree(capsys, tmp_path):
    """Extends the existing paths-agree property to the real fetch path.

    The same numbers through fetch->save->audit and through a CSV must produce
    the same estimate.
    """
    csv_path = tmp_path / "same.csv"
    csv_path.write_text(
        "start,model,workspace_id,api_key_id,service_tier,"
        "uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,claude-opus-5,wrkspc_main,apikey_main,standard,"
        "1000000,200000\n",
        encoding="utf-8",
    )
    main(["audit", str(csv_path), "--json"])
    from_csv = json.loads(capsys.readouterr().out)

    main([
        "audit",
        str(saved(tmp_path, group_by=["model", "workspace_id", "api_key_id",
                                      "service_tier"])),
        "--json",
    ])
    from_api = json.loads(capsys.readouterr().out)

    assert from_csv["spend"]["estimated_usd"] == from_api["spend"]["estimated_usd"]
    assert from_csv["tokens"] == from_api["tokens"]


# ==========================================================================
# The command -- success, driven through an injected transport
# ==========================================================================


def test_fetch_writes_a_readable_file(tmp_path, monkeypatch, capsys):
    """Drives the real _fetch path with the transport swapped out."""
    transport = FakeTransport(ok(page(has_more=True, next_page="c1")), ok(page()))
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", KEY)

    real_init = ApiClient.__init__

    def patched(self, credential, **kw):
        kw["transport"] = transport
        kw["sleep"] = no_sleep
        real_init(self, credential, **kw)

    monkeypatch.setattr(ApiClient, "__init__", patched)

    out = tmp_path / "raw.json"
    code = main([
        "fetch", "--from", "2026-08-01", "--to", "2026-08-08",
        "-o", str(out), "--group-by", "model,workspace_id",
    ])
    err = capsys.readouterr().err

    assert code == 0
    assert out.exists()
    assert "2 page(s)" in err
    assert "audit" in err  # tells you what to do next

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert is_envelope(payload)
    assert payload["request"]["group_by"] == ["model", "workspace_id"]
    assert len(payload["pages"]) == 2
    # And the artifact carries no secret.
    assert KEY not in out.read_text(encoding="utf-8")


def test_the_fetched_file_is_immediately_auditable(tmp_path, monkeypatch, capsys):
    """The workflow end to end: fetch once, then analyse offline."""
    transport = FakeTransport(ok(page()))
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", KEY)
    real_init = ApiClient.__init__

    def patched(self, credential, **kw):
        kw["transport"] = transport
        kw["sleep"] = no_sleep
        real_init(self, credential, **kw)

    monkeypatch.setattr(ApiClient, "__init__", patched)

    out = tmp_path / "raw.json"
    assert main([
        "fetch", "--from", "2026-08-01", "--to", "2026-08-08", "-o", str(out),
    ]) == 0
    capsys.readouterr()

    assert main(["audit", str(out)]) == 0
    assert "$10.00" in capsys.readouterr().out


# ==========================================================================
# What a default fetch asks for
# ==========================================================================


def test_the_default_group_by_covers_every_check_that_needs_one():
    """Before M5 the default omitted context_window, so C4 could never run
    on fetched data -- while the help text claimed the default "covers the
    checks that need grouping". Held against the live registry so the next
    check with a new dimension fails here rather than in someone's report."""
    from claude_spend_audit.checks import CHECKS
    from claude_spend_audit.cli import DEFAULT_FETCH_GROUP_BY

    needed = set().union(*(c.needs_dimensions for c in CHECKS))
    assert needed <= set(DEFAULT_FETCH_GROUP_BY), needed - set(DEFAULT_FETCH_GROUP_BY)


def test_the_default_group_by_covers_every_price_dimension():
    """Without inference_geo and speed, US-only (1.1x) and fast-mode (2x)
    traffic prices at the baseline and nothing in the output shows it."""
    from claude_spend_audit.cli import DEFAULT_FETCH_GROUP_BY
    from claude_spend_audit.pricing import PRICE_DIMENSIONS

    assert set(PRICE_DIMENSIONS) <= set(DEFAULT_FETCH_GROUP_BY)


def test_the_parser_default_is_the_declared_default():
    from claude_spend_audit.cli import DEFAULT_FETCH_GROUP_BY, build_parser

    args = build_parser().parse_args(
        ["fetch", "--from", "2026-08-01", "--to", "2026-08-08", "-o", "x.json"]
    )
    assert args.group_by == ",".join(DEFAULT_FETCH_GROUP_BY)


def test_a_default_fetch_requests_every_default_dimension(tmp_path, monkeypatch, capsys):
    """On the wire, not just in the parser -- including the beta header the
    docs require whenever speed is in group_by."""
    from urllib.parse import parse_qs, urlsplit

    from claude_spend_audit.cli import DEFAULT_FETCH_GROUP_BY

    transport = FakeTransport(ok(page()))
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", KEY)
    real_init = ApiClient.__init__

    def patched(self, credential, **kw):
        kw["transport"] = transport
        kw["sleep"] = no_sleep
        real_init(self, credential, **kw)

    monkeypatch.setattr(ApiClient, "__init__", patched)

    out = tmp_path / "raw.json"
    assert main(["fetch", "--from", "2026-08-01", "--to", "2026-08-08", "-o", str(out)]) == 0
    capsys.readouterr()

    sent = parse_qs(urlsplit(transport.last.url).query)["group_by[]"]
    assert sent == list(DEFAULT_FETCH_GROUP_BY)
    assert "fast-mode-2026-02-01" in (transport.header("anthropic-beta") or "")

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["request"]["group_by"] == list(DEFAULT_FETCH_GROUP_BY)


def test_a_default_fetch_raises_no_pricing_dimension_gap(tmp_path, monkeypatch, capsys):
    """The point of the new default: a fetched file with no flags carries every
    dimension the estimate needs, so the caveats do not warn about any."""
    transport = FakeTransport(ok(page()))
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", KEY)
    real_init = ApiClient.__init__

    def patched(self, credential, **kw):
        kw["transport"] = transport
        kw["sleep"] = no_sleep
        real_init(self, credential, **kw)

    monkeypatch.setattr(ApiClient, "__init__", patched)

    out = tmp_path / "raw.json"
    assert main(["fetch", "--from", "2026-08-01", "--to", "2026-08-08", "-o", str(out)]) == 0
    capsys.readouterr()

    main(["audit", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    codes = {g["code"] for g in payload["gaps"]}
    assert "pricing_dimension_not_grouped" not in codes


# ==========================================================================
# The recorded artifact
# ==========================================================================

RECORDED = "tests/fixtures/api/fetched_usage.json"


def test_the_recorded_fetch_carries_no_credential():
    """The committed fixture is a real fetch artifact. If a credential could
    reach one of these, it would reach this file."""
    import pathlib

    text = pathlib.Path(RECORDED).read_text(encoding="utf-8")
    assert "sk-ant" not in text
    assert "x-api-key" not in text
    assert "Authorization" not in text


def test_the_recorded_fetch_audits_end_to_end(capsys):
    """Two stitched pages through the whole pipeline.

    Day 2 carries 2,000,000 1h-cache writes against 200,000 reads on
    claude-opus-5 -- the same shape as the CSV fixture, so C1 must reach the
    same figure by a different route:

        1h writes 2,000,000 @ $10.00/MTok = $20.00
        reads       200,000 @  $0.50/MTok =  $0.10
        uncached  2,200,000 @  $5.00/MTok = $11.00
        waste                               = $9.10
    """
    code = main(["audit", RECORDED])
    out = capsys.readouterr().out
    assert code == 0
    assert "$9.10" in out
    for check in ("C1", "C2", "C3", "C7"):
        assert check in out


def test_the_recorded_fetch_stitched_both_pages(capsys):
    """Three buckets across two pages; losing one would change every total."""
    main(["inspect", RECORDED, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["period"]["start"].startswith("2026-08-01")
    assert payload["period"]["end"].startswith("2026-08-04")
    #   5,000,000 + 0 + 3,000,000 uncached across the three buckets
    assert payload["tokens"]["uncached_input"] == 8_000_000
    assert payload["tokens"]["cache_write_1h"] == 2_000_000


def test_c7_sees_the_null_workspace_from_the_api_path(capsys):
    """The null-vs-not-grouped distinction has to survive the fetch round
    trip: workspace_id was requested, so a null genuinely means default."""
    main(["audit", RECORDED, "--json"])
    payload = json.loads(capsys.readouterr().out)
    c7 = next(f for f in payload["findings"] if f["check_id"] == "C7")
    #   sonnet bucket: 3,000,000 @ $2.00 + 400,000 @ $10.00 = $10.00
    # Compared as Decimal, not as text: JSON preserves the exact value, so
    # "10" and "10.00" are both correct and a string compare would be brittle.
    from decimal import Decimal

    assert Decimal(c7["measured_usd"]) == Decimal("10.00")
    labels = [row[0] for row in c7["evidence"]["rows"]]
    assert "default workspace, and no API key" in labels
