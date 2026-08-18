"""S1-08R3 A: formal authorization envelope, inventory plan and attempt ledger.

Pure-domain contract tests: the envelope refuses tampering, scope
imbalance, widened budgets and any drift from the frozen ADR-027 facts;
the inventory plan builder performs zero filesystem access; the attempt
ledger is single-use and append-only.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from quantking.data.eod.formal_authorization import (
    FORMAL_ENVELOPE_SCHEMA_VERSION,
    FORMAL_INVENTORY_PLAN_SCHEMA_VERSION,
    FormalAttemptLedger,
    FormalAuthorizationEnvelope,
    FormalAuthorizationError,
    assert_envelope_matches_manifest,
    build_formal_authorization_envelope,
    build_formal_inventory_plan,
    code_tree_entries,
    compute_code_tree_sha256,
    excluded_symbols_sha256,
    inventory_plan_sha256,
)
from quantking.data.eod.release_manifest import build_release_manifest

_GIT_HEAD = "9f5b9ea"
_CODE_TREE = "b" * 64


def _manifest(
    symbols: tuple[str, ...] = ("000001.SZ", "600000.SH"),
) -> object:
    return build_release_manifest(
        symbols=symbols,
        target_start=date(2026, 6, 11),
        target_end=date(2026, 8, 9),
        lookback_days=15,
        calendar_version_id="exchange-calendar-2026h2.v1",
        calendar_sha256="a" * 64,
        source_route_version="BAO-TX-SINA.k4.v1",
        adapter_version="baostock-akshare-daily-bar.k4.v1",
        code_version="s1-08r3.v1",
        quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
        threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
        resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
        unit_policy_versions={"baostock": "b", "akshare": "a"},
    )


def _envelope(
    manifest=None,
    *,
    excluded: tuple[tuple[str, str], ...] = (),
    universe_total: int | None = None,
    git_head: str = _GIT_HEAD,
    code_tree_sha256: str = _CODE_TREE,
) -> FormalAuthorizationEnvelope:
    manifest = manifest or _manifest()
    included_count = len(manifest.symbols)
    total = universe_total if universe_total is not None else included_count + len(excluded)
    return build_formal_authorization_envelope(
        manifest=manifest,
        authorization_marker="S1-08R3-E1-FORMAL-RELEASE-V1",
        authorization_id="S1-08R3-E1-ab12cd34ef56",
        idempotency_key="s1-08r3-formal-20260817-v1",
        git_head=git_head,
        code_tree_sha256=code_tree_sha256,
        universe_version="configs/default.toml@9f5b9ea",
        universe_sha256="c" * 64,
        universe_total=total,
        excluded_symbols=excluded,
        calendar_source="published-warehouse-exchange-calendar",
        calendar_version_id="data_version:00000000-0000-4000-8000-0000000000c1",
        base_coverage_binding="none: first formal release without a base",
    )


def test_envelope_roundtrip_is_canonical_and_byte_stable() -> None:
    envelope = _envelope()
    first = envelope.canonical_json()
    second = FormalAuthorizationEnvelope.from_dict(json.loads(first))
    assert second.canonical_json() == first
    assert second.envelope_sha256() == envelope.envelope_sha256()
    payload = envelope.to_dict()
    assert payload["schema_version"] == FORMAL_ENVELOPE_SCHEMA_VERSION
    assert payload["application_retries"] == 0
    assert payload["fail_fast"] is True
    assert payload["requires_new_real_authorization"] is True
    assert payload["hard_timeouts"] == {
        "akshare_session_seconds": 60,
        "baostock_session_seconds": 20,
    }


def test_envelope_freezes_the_adr027_rejection_facts() -> None:
    legacy = _envelope().legacy_disposition
    assert legacy["legacy_import_manifest_id"] is None
    assert legacy["legacy_import_manifest_sha256"] is None
    assert legacy["inventory_evidence_sha256"] == (
        "140978ea3e3f4d9139c15d641c92564af8f58928bf77017117393ce988a04ce2"
    )
    assert legacy["pilot_attempt2_diagnostic_sha256"] == (
        "b75f27b4096571f04eb790e8cd78f1da1faf174187dc44ab0ca09d20fe1c02e1"
    )
    assert legacy["discovered_files"] == 5208
    assert legacy["discovered_bytes"] == 527_708_270
    assert legacy["readable_files"] == 5208
    assert legacy["read_errors"] == 0
    assert legacy["imported_files"] == 0
    assert legacy["imported_rows"] == 0
    assert legacy["published_files"] == 0
    assert legacy["published_rows"] == 0
    assert legacy["rejected_files"] == 5208


def test_scope_conservation_with_exclusions() -> None:
    manifest = _manifest(symbols=("000001.SZ",))
    envelope = _envelope(
        manifest,
        excluded=(
            ("600000.SH", "USER_EXCLUDED_FROM_SCOPE"),
            ("688981.SH", "NO_VERIFIED_BASE_COVERAGE"),
        ),
    )
    assert envelope.universe.total == 3
    assert len(envelope.included_symbols) + len(envelope.excluded_symbols) == 3
    assert envelope.excluded_symbols_sha256 == excluded_symbols_sha256(envelope.excluded_symbols)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_field",
        "extra_field",
        "bad_included_sha",
        "unsorted_included",
        "unknown_exclusion_reason",
        "included_excluded_overlap",
        "scope_imbalance",
        "widened_role_budget",
        "budget_sum_mismatch",
        "http_not_derived",
        "protocol_not_derived",
        "literal_retries",
        "literal_fail_fast",
        "literal_requires_authorization",
        "legacy_rejected_count",
        "legacy_import_manifest_sha",
        "hard_timeout",
        "empty_write_paths",
    ),
)
def test_envelope_parse_fails_closed_on_tampering(mutation: str) -> None:
    envelope = _envelope(
        _manifest(symbols=("000001.SZ",)),
        excluded=(("600000.SH", "USER_EXCLUDED_FROM_SCOPE"),),
    )
    payload = json.loads(envelope.canonical_json())
    if mutation == "missing_field":
        del payload["calendar_sha256"]
    elif mutation == "extra_field":
        payload["surprise"] = 1
    elif mutation == "bad_included_sha":
        payload["included_symbols_sha256"] = "0" * 64
    elif mutation == "unsorted_included":
        payload["included_symbols"] = ["600000.SH", "000001.SZ"]
    elif mutation == "unknown_exclusion_reason":
        payload["excluded_symbols"][0]["reason_code"] = "BECAUSE"
    elif mutation == "included_excluded_overlap":
        payload["excluded_symbols"][0]["symbol"] = "000001.SZ"
    elif mutation == "scope_imbalance":
        payload["universe"]["total"] = 9
    elif mutation == "widened_role_budget":
        payload["a_baostock_endpoint_max"] = 513
    elif mutation == "budget_sum_mismatch":
        payload["endpoint_total_max"] = 999
    elif mutation == "http_not_derived":
        payload["akshare_http_max"] = 9999
    elif mutation == "protocol_not_derived":
        payload["baostock_protocol_max"] = 9999
    elif mutation == "literal_retries":
        payload["application_retries"] = 1
    elif mutation == "literal_fail_fast":
        payload["fail_fast"] = False
    elif mutation == "literal_requires_authorization":
        payload["requires_new_real_authorization"] = False
    elif mutation == "legacy_rejected_count":
        payload["legacy_disposition"]["rejected_files"] = 0
    elif mutation == "legacy_import_manifest_sha":
        payload["legacy_disposition"]["legacy_import_manifest_sha256"] = "d" * 64
    elif mutation == "hard_timeout":
        payload["hard_timeouts"]["baostock_session_seconds"] = 999
    elif mutation == "empty_write_paths":
        payload["allowed_write_paths"] = []
    with pytest.raises(FormalAuthorizationError):
        FormalAuthorizationEnvelope.from_dict(payload)


def test_envelope_builder_refuses_scope_imbalance() -> None:
    with pytest.raises(FormalAuthorizationError):
        _envelope(universe_total=5)


def test_envelope_matches_manifest_and_rejects_drift() -> None:
    manifest = _manifest()
    envelope = _envelope(manifest)
    assert_envelope_matches_manifest(envelope, manifest)

    other = _manifest(symbols=("000858.SZ",))
    other_envelope = _envelope(other)
    with pytest.raises(FormalAuthorizationError):
        assert_envelope_matches_manifest(other_envelope, manifest)


def test_code_tree_digest_is_stable_and_sensitive(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__main__.py").write_text("print('hi')\n", encoding="utf-8")
        (root / "pkg" / "__pycache__").mkdir()
        (root / "pkg" / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")
        (root / "pkg" / "notes.txt").write_text("ignored", encoding="utf-8")
    assert compute_code_tree_sha256(left) == compute_code_tree_sha256(right)
    entries = code_tree_entries(left)
    assert [path for path, _digest in entries] == ["pkg/__main__.py"]

    (right / "pkg" / "extra.py").write_text("value = 1\n", encoding="utf-8")
    assert compute_code_tree_sha256(left) != compute_code_tree_sha256(right)


def test_inventory_plan_is_pure_and_byte_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quantking.data.eod.formal_authorization as module

    def _forbid(name: str):  # type: ignore[no-untyped-def]
        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"inventory plan must not call {name}")

        return boom

    # The builder is structurally zero-I/O: it accepts no Path and must not
    # touch any tree-walking helper even indirectly.
    monkeypatch.setattr(module, "code_tree_entries", _forbid("code_tree_entries"))
    monkeypatch.setattr(module, "compute_code_tree_sha256", _forbid("compute_code_tree_sha256"))

    first = build_formal_inventory_plan(
        marker="S1-08R3-I0-INVENTORY-READONLY-V1",
        authorization_id="S1-08R3-I0-99aa88bb77cc",
        allowed_root_label=r"E:\QuantKingV3Data",
        sqlite_tables=(
            "jobs",
            "job_events",
            "dataset_versions",
            "ingestion_batches",
        ),
        directory_listings=(
            "metadata",
            "warehouse/daily_bar_unadjusted",
            "raw",
        ),
        file_sha_targets=("metadata/quantking.db",),
        query_limit=200,
        code_tree_sha256=_CODE_TREE,
        git_head=_GIT_HEAD,
    )
    second = build_formal_inventory_plan(
        marker="S1-08R3-I0-INVENTORY-READONLY-V1",
        authorization_id="S1-08R3-I0-99aa88bb77cc",
        allowed_root_label=r"E:\QuantKingV3Data",
        sqlite_tables=(
            "jobs",
            "job_events",
            "dataset_versions",
            "ingestion_batches",
        ),
        directory_listings=(
            "metadata",
            "warehouse/daily_bar_unadjusted",
            "raw",
        ),
        file_sha_targets=("metadata/quantking.db",),
        query_limit=200,
        code_tree_sha256=_CODE_TREE,
        git_head=_GIT_HEAD,
    )
    raw_first = json.dumps(first, ensure_ascii=False, sort_keys=True).encode("utf-8")
    raw_second = json.dumps(second, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert raw_first == raw_second
    assert inventory_plan_sha256(first) == inventory_plan_sha256(second)
    assert first["schema_version"] == FORMAL_INVENTORY_PLAN_SCHEMA_VERSION
    assert first["network_calls"] == 0
    assert first["writes"] == 0
    assert first["sqlite_checkpoints"] == 0
    assert first["legacy_root_access"] is False
    assert first["backup_root_access"] is False
    assert first["requires_new_real_authorization"] is True


def test_inventory_plan_rejects_unsafe_targets() -> None:
    with pytest.raises(FormalAuthorizationError):
        build_formal_inventory_plan(
            marker="S1-08R3-I0-INVENTORY-READONLY-V1",
            authorization_id="S1-08R3-I0-99aa88bb77cc",
            allowed_root_label=r"E:\QuantKingV3Data",
            sqlite_tables=("../secrets",),
            directory_listings=(),
            file_sha_targets=(),
            query_limit=10,
            code_tree_sha256=_CODE_TREE,
            git_head=_GIT_HEAD,
        )


def test_attempt_ledger_is_single_use_and_append_only(tmp_path: Path) -> None:
    ledger = FormalAttemptLedger(tmp_path / "attempts")
    attempt_root = ledger.begin(
        envelope_sha256="e" * 64,
        marker="S1-08R3-E1-FORMAL-RELEASE-V1",
        authorization_id="S1-08R3-E1-ab12cd34ef56",
        idempotency_key="s1-08r3-formal-20260817-v1",
        now_iso="2026-08-17T12:00:00+00:00",
    )
    header = json.loads((attempt_root / "attempt-header.json").read_text(encoding="utf-8"))
    assert header["status"] == "STARTED"
    assert header["envelope_sha256"] == "e" * 64

    with pytest.raises(FormalAuthorizationError) as replay:
        ledger.begin(
            envelope_sha256="e" * 64,
            marker="S1-08R3-E1-FORMAL-RELEASE-V1",
            authorization_id="S1-08R3-E1-ab12cd34ef56",
            idempotency_key="s1-08r3-formal-20260817-v1",
            now_iso="2026-08-17T12:01:00+00:00",
        )
    assert replay.value.code == "AUTHORIZATION_ALREADY_CONSUMED"

    completed = ledger.record(
        "S1-08R3-E1-ab12cd34ef56",
        status="COMPLETED",
        now_iso="2026-08-17T12:02:00+00:00",
    )
    assert completed.name == "completed.json"
    with pytest.raises(FormalAuthorizationError):
        ledger.record(
            "S1-08R3-E1-ab12cd34ef56",
            status="COMPLETED",
            now_iso="2026-08-17T12:03:00+00:00",
        )
