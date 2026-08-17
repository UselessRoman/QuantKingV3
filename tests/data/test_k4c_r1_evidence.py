"""S1-07K4C-R1 reverse tests for the repaired evidence contract.

These tests prove the twelve required rejection/acceptance behaviors without
any network access: sufficiency can never be claimed from call completion
alone, objectives need actual observations, extrema survive an unbounded
divergence set, tampered summaries and artifacts are rejected, replay of a
consumed authorization_id is refused before the first gateway call, a
mid-run failure leaves an exact invocation count, and the 50/0 boundaries of
the candidate thresholds stay exact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from quantking.data.comparison.k4c_evidence import (
    AUTHORIZATION_ALREADY_CONSUMED,
    INVALID_ATTEMPT_INPUT,
    K4C_R2_EVIDENCE_SCHEMA,
    K4C_R2_INVOCATION_BUDGET,
    K4C_R2_MARKER,
    K4C_R2_OBJECTIVES,
    K4C_R3_INVOCATION_BUDGET,
    K4C_R3_MARKER,
    K4CAttemptHeader,
    K4CAttemptLedger,
    K4CLedgerError,
    K4CVerifier,
    artifact_manifest_payload,
    comparison_rows_payload,
    compute_k4c_r2_sufficiency,
    k4c_r2_plan_sha256,
    observe_k4c_r2_objectives,
    recompute_comparison_statistics,
    validate_k4c_r2_objective_records,
)
from quantking.data.comparison.k4c_probe import fixed_k4c_probe_plan
from quantking.data.comparison.thresholds import k4_evidence_thresholds, rows_within_threshold

_FROZEN_LIBRARY = {"BAOSTOCK": "0.9.3", "TENCENT": "1.18.83", "SINA": "1.18.83"}
_FROZEN_CONTRACT = {
    "BAOSTOCK": "baostock-daily-bar.v1",
    "TENCENT": "akshare-daily-bar.v2",
    "SINA": "akshare-daily-bar.v2",
}
_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
_ROLES = ("A", "B", "C")
_ALL_CONTRACT_CHECKS = {
    "units_verified": True,
    "unadjusted": True,
    "complete": True,
    "dates_in_target_window": True,
    "fields_complete": True,
    "library_versions_frozen": True,
}


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _row(
    *,
    symbol: str = "000001.SZ",
    trade_date: str = "2026-06-11",
    close: float = 10.0,
    pre_close: float = 10.0,
    volume: int = 1_000_000,
    amount: float = 500_000_000.0,
) -> dict[str, object]:
    return {
        "code": symbol,
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": pre_close,
        "volume": volume,
        "amount": amount,
    }


def _full_rows() -> dict[str, dict[tuple[str, str], Mapping[str, object]]]:
    """Rows satisfying every objective (see test 5)."""
    row_a = _row(symbol="000001.SZ", trade_date="2026-06-11")
    row_sh = _row(symbol="600000.SH", trade_date="2026-06-11")
    low = _row(symbol="000858.SZ", trade_date="2026-06-12", amount=100_000_000.0)
    boundary = _row(symbol="600519.SH", trade_date="2026-06-12", amount=2_000_000_000.0)
    ex_rights = _row(symbol="000858.SZ", trade_date="2026-06-13", pre_close=10.0)
    others_day = _row(symbol="000001.SZ", trade_date="2026-06-14")
    partial = _row(symbol="600519.SH", trade_date="2026-06-14")
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {}
    for role in _ROLES:
        rows[role] = {
            ("000001.SZ", "2026-06-11"): row_a,
            ("600000.SH", "2026-06-11"): row_sh,
            ("000858.SZ", "2026-06-12"): low,
            ("600519.SH", "2026-06-12"): boundary,
            ("000858.SZ", "2026-06-13"): ex_rights,
            ("000001.SZ", "2026-06-14"): others_day,
        }
    rows["A"][("600519.SH", "2026-06-14")] = partial
    return rows


def test_sufficiency_false_when_36_calls_complete_but_objective_missing() -> None:
    objectives = [
        {"objective": name, "observed": name != "suspension_session"}
        for name in ("normal_session", "suspension_session")
    ]
    sufficient, reasons = compute_k4c_r2_sufficiency(
        invocation_count=36,
        budget=36,
        failure=None,
        objectives=objectives,
        contract_checks=_ALL_CONTRACT_CHECKS,
        comparison_verified=True,
        unrecorded_attempts=0,
    )
    assert sufficient is False
    assert "OBJECTIVE_UNOBSERVED:suspension_session" in reasons


def test_planned_objectives_without_observed_evidence_are_not_sufficient() -> None:
    observations = observe_k4c_r2_objectives({})
    assert all(record["observed"] is False for record in observations)
    assert all(record["reason_code"] for record in observations)
    sufficient, reasons = compute_k4c_r2_sufficiency(
        invocation_count=36,
        budget=36,
        failure=None,
        objectives=observations,
        contract_checks=_ALL_CONTRACT_CHECKS,
        comparison_verified=True,
        unrecorded_attempts=0,
    )
    assert sufficient is False
    assert "OBJECTIVE_UNOBSERVED:normal_session" in reasons
    assert "OBJECTIVE_UNOBSERVED:missing_or_empty_upstream" in reasons


def test_missing_or_empty_not_actually_observed_makes_sufficient_false() -> None:
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {
        role: {("000001.SZ", "2026-06-11"): _row()} for role in _ROLES
    }
    observations = observe_k4c_r2_objectives(rows)
    missing = next(
        record for record in observations if record["objective"] == "missing_or_empty_upstream"
    )
    assert missing["observed"] is False
    assert missing["reason_code"] == "NO_MISSING_OR_EMPTY_OBSERVED"
    sufficient, reasons = compute_k4c_r2_sufficiency(
        invocation_count=36,
        budget=36,
        failure=None,
        objectives=observations,
        contract_checks=_ALL_CONTRACT_CHECKS,
        comparison_verified=True,
        unrecorded_attempts=0,
    )
    assert sufficient is False
    assert "OBJECTIVE_UNOBSERVED:missing_or_empty_upstream" in reasons


def test_objective_record_referencing_out_of_plan_symbol_or_date_is_rejected() -> None:
    in_plan_symbol = {
        "objective": "normal_session",
        "observed": True,
        "symbol": "000001.SZ",
        "trade_date": "2026-06-11",
        "roles": ["A", "B", "C"],
        "evidence_type": "held_row_fact",
        "fact": {},
        "reason_code": None,
    }
    assert validate_k4c_r2_objective_records((in_plan_symbol,)) == ()
    out_of_plan_symbol = {**in_plan_symbol, "symbol": "999999.SZ"}
    assert validate_k4c_r2_objective_records((out_of_plan_symbol,)) == (
        "OBJECTIVE_OUT_OF_PLAN:normal_session",
    )
    out_of_window = {**in_plan_symbol, "trade_date": "2020-01-01"}
    assert validate_k4c_r2_objective_records((out_of_window,)) == (
        "OBJECTIVE_OUT_OF_PLAN:normal_session",
    )


def test_all_objectives_observed_and_calls_complete_allow_sufficient_true() -> None:
    rows = _full_rows()
    observations = observe_k4c_r2_objectives(
        rows,
        vendor_preclose={"A": {("000858.SZ", "2026-06-13"): "9.8"}},
    )
    observed_names = {record["objective"] for record in observations if record["observed"] is True}
    assert observed_names == {
        "normal_session",
        "ex_rights_session",
        "suspension_session",
        "low_liquidity_session",
        "volume_amount_boundary",
        "missing_or_empty_upstream",
        "shenzhen_code_segments",
        "shanghai_code_segments",
    }
    sufficient, reasons = compute_k4c_r2_sufficiency(
        invocation_count=36,
        budget=36,
        failure=None,
        objectives=observations,
        contract_checks=_ALL_CONTRACT_CHECKS,
        comparison_verified=True,
        unrecorded_attempts=0,
    )
    assert sufficient is True
    assert reasons == ()


def test_uncapped_extrema_survive_more_than_20_divergences() -> None:
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {"A": {}, "B": {}}
    for index in range(30):
        symbol = f"{index:06d}.SZ"
        rows["A"][(symbol, "2026-06-11")] = _row(symbol=symbol, volume=10_000 + index)
        rows["B"][(symbol, "2026-06-11")] = _row(symbol=symbol, volume=10_000)
    statistics = recompute_comparison_statistics(rows)
    volume = statistics["fields"]["volume"]
    assert volume["divergent_rows"] == 29  # index 0 has equal volumes
    assert volume["max_absolute"] is not None
    assert volume["max_absolute"]["absolute_difference"] == "29"
    assert volume["max_absolute"]["symbol"] == "000029.SZ"
    assert set(volume["max_absolute"]["values"]) == {"A", "B"}


def _synthetic_attempt(
    tmp_path: Path,
    rows_by_role: Mapping[str, Mapping[tuple[str, str], Mapping[str, object]]],
    *,
    authorization_id: str = "K4C-R2-synthetic-0001",
    vendor_preclose: Mapping[str, Mapping[tuple[str, str], str]] | None = None,
    drop_comparison: bool = False,
    tamper_comparison_sha: bool = False,
    drop_raw: tuple[int, ...] = (),
) -> tuple[Path, dict[str, object]]:
    vendor = {} if vendor_preclose is None else vendor_preclose
    ledger = K4CAttemptLedger(tmp_path / "attempts")
    directory = ledger.attempt_directory(authorization_id)
    # R2-style attempt fixture: the retired R2 marker cannot open new attempts,
    # so the fixture writes the immutable header directly (like the real R2
    # attempt directory, which is read-only evidence).
    directory.mkdir(parents=True)
    (directory / "attempt-header.json").write_text(
        _canonical(
            K4CAttemptHeader(
                authorization_id=authorization_id,
                attempt_id="attempt-fixture00000001",
                marker=K4C_R2_MARKER,
                plan_sha256=k4c_r2_plan_sha256(),
                invocation_budget=K4C_R2_INVOCATION_BUDGET,
                created_at="2026-08-16T00:00:00Z",
            ).to_dict()
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (directory / "artifacts" / "raw").mkdir(parents=True)
    plan = fixed_k4c_probe_plan()
    for sequence, invocation in enumerate(plan, start=1):
        chunk_rows = [
            (symbol, trade_date, row)
            for (symbol, trade_date), row in sorted(rows_by_role.get(invocation.role, {}).items())
            if symbol == invocation.symbol
            and invocation.target_start.isoformat()
            <= trade_date
            <= invocation.target_end.isoformat()
        ]
        records = [
            {
                "stock_code": symbol,
                "date": trade_date,
                **{field: row[field] for field in _FIELDS},
            }
            for symbol, trade_date, row in chunk_rows
        ]
        # Lookback context rows: the closure verifier derives pre_close from
        # the previous close over the upstream series, so every fixture adds
        # synthetic context rows before the target window.
        lookback_rows: list[list[object]] = []
        lookback_records: list[dict[str, object]] = []
        from datetime import timedelta as _timedelta

        cursor = invocation.upstream_start
        while cursor < invocation.target_start:
            day = cursor.isoformat()
            lookback_rows.append(
                [day, invocation.symbol, 10.0, 10.0, 10.0, 10.0, 10.0, 1000, 10000.0]
            )
            lookback_records.append(
                {
                    "date": day,
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "volume": 1000,
                    "amount": 10000.0,
                }
            )
            cursor += _timedelta(days=1)
        if invocation.upstream.value == "BAOSTOCK":
            provider_payload: dict[str, object] = {
                "fields": [
                    "date",
                    "code",
                    "open",
                    "high",
                    "low",
                    "close",
                    "preclose",
                    "volume",
                    "amount",
                ],
                "rows": [
                    *lookback_rows,
                    *[
                        [
                            trade_date,
                            symbol,
                            row["open"],
                            row["high"],
                            row["low"],
                            row["close"],
                            vendor.get("A", {}).get((symbol, trade_date), str(row["pre_close"])),
                            row["volume"],
                            row["amount"],
                        ]
                        for symbol, trade_date, row in chunk_rows
                    ],
                ],
            }
        else:
            lot_units = invocation.upstream.value == "TENCENT" and invocation.symbol.startswith(
                "000"
            )
            provider_payload = {
                "records": [
                    *lookback_records,
                    *[
                        {
                            **record,
                            "volume": (
                                int(record["volume"]) // 100 if lot_units else record["volume"]
                            ),
                        }
                        for record in records
                    ],
                ],
                "row_count": len(lookback_records) + len(records),
                "content_sha256": "a" * 64,
                "unit_policy_version": "akshare-1.18.83-returned-units.v1",
            }
        envelope = {
            "schema_version": "market-data-raw-response.v1",
            "binding": {
                "sequence": sequence,
                "dataset": "daily_bar_unadjusted",
                "scope": [invocation.symbol],
                "start": invocation.target_start.isoformat(),
                "end": invocation.target_end.isoformat(),
                "request_sha256": "a" * 64,
            },
            "request_window": {
                "target_start": invocation.target_start.isoformat(),
                "target_end": invocation.target_end.isoformat(),
                "upstream_start": invocation.upstream_start.isoformat(),
                "upstream_end": invocation.upstream_end.isoformat(),
                "lookback_days": 15,
            },
            "contract": {
                "dataset": "daily_bar_unadjusted",
                "contract_version": _FROZEN_CONTRACT[invocation.upstream.value],
                "required_fields": [
                    "stock_code",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "pre_close",
                    "volume",
                    "amount",
                ],
                "volume_unit": (
                    "lot_100_shares"
                    if invocation.upstream.value == "TENCENT"
                    and invocation.symbol.startswith("000")
                    else "share"
                ),
                "amount_unit": "cny_yuan",
                "unadjusted": True,
                "lineage": {
                    "provider": invocation.provider.value,
                    "upstream": invocation.upstream.value,
                    "endpoint": invocation.endpoint,
                    "library_version": _FROZEN_LIBRARY[invocation.upstream.value],
                    "fetched_at": "2026-08-16T00:00:00Z",
                },
            },
            "response": {
                "complete": True,
                "partial_reason": None,
                "records": records,
                "upstream_row_count": len(records),
            },
            "provider_payload": provider_payload,
        }
        if sequence not in drop_raw:
            (directory / "artifacts" / "raw" / f"{sequence}.json").write_text(
                _canonical(envelope) + "\n", encoding="utf-8", newline="\n"
            )
        ledger.record(
            authorization_id,
            sequence=sequence,
            outcome={
                "sequence": sequence,
                "symbol": invocation.symbol,
                "role": invocation.role,
                "upstream": invocation.upstream.value,
                "status": "SUCCEEDED",
            },
        )
    if not drop_comparison:
        comparison_text = _canonical(comparison_rows_payload(rows_by_role))
        comparison_path = directory / "artifacts" / "comparison-rows.json"
        comparison_path.write_text(comparison_text + "\n", encoding="utf-8", newline="\n")
    raw_shas: dict[int, str] = {}
    for sequence in range(1, K4C_R2_INVOCATION_BUDGET + 1):
        path = directory / "artifacts" / "raw" / f"{sequence}.json"
        if path.is_file():
            raw_shas[sequence] = hashlib.sha256(path.read_bytes()).hexdigest()
    comparison_path = directory / "artifacts" / "comparison-rows.json"
    comparison_sha = (
        "0" * 64
        if tamper_comparison_sha
        else (
            hashlib.sha256(comparison_path.read_bytes()).hexdigest()
            if comparison_path.is_file()
            else "0" * 64
        )
    )
    manifest = artifact_manifest_payload(
        comparison_rows_sha256=comparison_sha,
        raw_shas=raw_shas,
        rows_by_role_count={role: len(rows_by_role.get(role, {})) for role in _ROLES},
    )
    manifest_path = directory / "artifacts" / "artifact-manifest.json"
    manifest_path.write_text(_canonical(manifest) + "\n", encoding="utf-8", newline="\n")
    return directory, manifest


def _evidence(directory: Path) -> dict[str, object]:
    recomputed = K4CVerifier().recompute(directory)
    header = json.loads((directory / "attempt-header.json").read_text(encoding="utf-8"))
    manifest_path = directory / "artifacts" / "artifact-manifest.json"
    return {
        "schema_version": K4C_R2_EVIDENCE_SCHEMA,
        "authorization_id": header["authorization_id"],
        "attempt_id": header["attempt_id"],
        "authorization_marker": header["authorization_marker"],
        "plan_sha256": header["plan_sha256"],
        "invocation_budget": header["invocation_budget"],
        "artifact_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "artifacts_root": str(directory),
        "application_retries": 0,
        "fail_fast": True,
        "writes_formal_data_root": False,
        "failure": None,
        "planned_objectives": [item.to_dict() for item in K4C_R2_OBJECTIVES],
        "invocation_count": recomputed.recomputed["invocation_count"],
        "not_called_invocation_count": recomputed.recomputed["not_called_invocation_count"],
        "observed_objectives": recomputed.recomputed["observed_objectives"],
        "representative_evidence_sufficient": recomputed.recomputed[
            "representative_evidence_sufficient"
        ],
        "cross_source": recomputed.recomputed["statistics"],
        "contract_checks": recomputed.recomputed["contract_checks"],
        "threshold_coverage": recomputed.recomputed["threshold_coverage"],
        "sufficiency_reasons": recomputed.recomputed["sufficiency_reasons"],
    }


def _divergent_rows() -> dict[str, dict[tuple[str, str], Mapping[str, object]]]:
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {}
    for role in _ROLES:
        rows[role] = {
            ("000001.SZ", "2026-06-11"): _row(volume=1_000_000),
        }
    rows["B"][("000001.SZ", "2026-06-11")] = _row(volume=999_900)
    return rows


def test_tampered_summary_maximum_is_rejected_by_verifier(tmp_path: Path) -> None:
    directory, _manifest = _synthetic_attempt(tmp_path, _divergent_rows())
    evidence = _evidence(directory)
    assert K4CVerifier().verify(attempt_directory=directory, evidence=evidence).ok
    tampered_cross_source = json.loads(json.dumps(evidence["cross_source"]))
    tampered_cross_source["fields"]["volume"]["max_absolute"]["absolute_difference"] = "999"
    tampered = {**evidence, "cross_source": tampered_cross_source}
    outcome = K4CVerifier().verify(attempt_directory=directory, evidence=tampered)
    assert outcome.ok is False
    assert "EVIDENCE_SUMMARY_MISMATCH:cross_source" in outcome.reasons


def test_missing_or_sha_tampered_artifact_is_rejected_by_verifier(
    tmp_path: Path,
) -> None:
    directory, _manifest = _synthetic_attempt(
        tmp_path,
        _divergent_rows(),
        authorization_id="K4C-R2-synthetic-0001",
        drop_comparison=True,
    )
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert "ARTIFACT_MISSING:artifacts/comparison-rows.json" in outcome.reasons

    directory, _manifest = _synthetic_attempt(
        tmp_path,
        _divergent_rows(),
        authorization_id="K4C-R2-synthetic-0002",
        tamper_comparison_sha=True,
    )
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert "ARTIFACT_SHA_MISMATCH:artifacts/comparison-rows.json" in outcome.reasons

    directory, _manifest = _synthetic_attempt(
        tmp_path,
        _divergent_rows(),
        authorization_id="K4C-R2-synthetic-0003",
        drop_raw=(1,),
    )
    outcome = K4CVerifier().recompute(directory)
    assert outcome.ok is False
    assert "ARTIFACT_MISSING:artifacts/raw/1.json" in outcome.reasons


def test_replay_of_consumed_authorization_id_is_rejected_before_first_fetch(
    tmp_path: Path,
) -> None:
    class FakeGateway:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("network call must never happen in a rejected attempt")

    ledger = K4CAttemptLedger(tmp_path / "attempts")
    ledger.begin(
        authorization_id="K4C-R3-replay-00000001",
        marker=K4C_R3_MARKER,
        plan_sha256="a" * 64,
        invocation_budget=K4C_R3_INVOCATION_BUDGET,
        created_at="2026-08-16T00:00:00Z",
    )
    gateway = FakeGateway()
    with pytest.raises(K4CLedgerError) as caught:
        ledger.begin(
            authorization_id="K4C-R3-replay-00000001",
            marker=K4C_R3_MARKER,
            plan_sha256="a" * 64,
            invocation_budget=K4C_R3_INVOCATION_BUDGET,
            created_at="2026-08-16T00:00:00Z",
        )
    assert caught.value.code == AUTHORIZATION_ALREADY_CONSUMED
    assert gateway.calls == 0


def test_failure_at_invocation_n_records_exact_counts_and_no_further_calls(
    tmp_path: Path,
) -> None:
    ledger = K4CAttemptLedger(tmp_path / "attempts")
    ledger.begin(
        authorization_id="K4C-R3-failure-0000001",
        marker=K4C_R3_MARKER,
        plan_sha256="a" * 64,
        invocation_budget=K4C_R3_INVOCATION_BUDGET,
        created_at="2026-08-16T00:00:00Z",
    )
    for sequence in range(1, 8):
        ledger.record(
            "K4C-R3-failure-0000001",
            sequence=sequence,
            outcome={
                "sequence": sequence,
                "status": "SUCCEEDED" if sequence < 7 else "FAILED",
                "stable_code": "AKSHARE_NETWORK" if sequence == 7 else None,
            },
        )
    invoked, not_called = ledger.counts("K4C-R3-failure-0000001")
    assert invoked == 7
    assert not_called == K4C_R3_INVOCATION_BUDGET - 7
    journal = (ledger.attempt_directory("K4C-R3-failure-0000001") / "invocations.jsonl").read_text(
        encoding="utf-8"
    )
    assert journal.count("\n") == 7


def test_threshold_boundary_50_passes_and_beyond_is_rejected() -> None:
    thresholds = k4_evidence_thresholds()
    fields = _FIELDS
    left = _row(volume=1_000_000, amount=10_000_000.0)
    right = _row(volume=999_950, amount=10_000_050.0)
    assert rows_within_threshold(thresholds, left, right, fields) is True
    beyond = _row(volume=999_949.999999, amount=10_000_050.000001)
    assert rows_within_threshold(thresholds, left, beyond, fields) is False
    ohlc_divergent = _row(close=10.200001)
    assert rows_within_threshold(thresholds, left, ohlc_divergent, fields) is False


def test_ledger_rejects_wrong_marker_budget_and_plan_sha(tmp_path: Path) -> None:
    ledger = K4CAttemptLedger(tmp_path / "attempts")
    with pytest.raises(K4CLedgerError) as caught:
        ledger.begin(
            authorization_id="K4C-R3-invalid-000001",
            marker="K4C-REPRESENTATIVE-2026-08-14-v1",
            plan_sha256="a" * 64,
            invocation_budget=K4C_R3_INVOCATION_BUDGET,
            created_at="2026-08-16T00:00:00Z",
        )
    assert caught.value.code == "MARKER_NOT_FROZEN"
    with pytest.raises(K4CLedgerError):
        ledger.begin(
            authorization_id="K4C-R3-invalid-000001",
            marker=K4C_R3_MARKER,
            plan_sha256="a" * 64,
            invocation_budget=12,
            created_at="2026-08-16T00:00:00Z",
        )
    with pytest.raises(K4CLedgerError) as caught:
        ledger.begin(
            authorization_id="K4C-R3-invalid-000001",
            marker=K4C_R3_MARKER,
            plan_sha256="0" * 64,
            invocation_budget=K4C_R3_INVOCATION_BUDGET,
            created_at="2026-08-16T00:00:00Z",
        )
    assert caught.value.code == INVALID_ATTEMPT_INPUT


def test_uncapped_extrema_match_decimal_semantics() -> None:
    rows: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {
        "A": {("600519.SH", "2026-06-11"): _row(amount=1_300_000_000.0)},
        "B": {("600519.SH", "2026-06-11"): _row(amount=1_299_999_950.0)},
        "C": {("600519.SH", "2026-06-11"): _row(amount=1_300_000_000.0)},
    }
    statistics = recompute_comparison_statistics(rows)
    amount = statistics["fields"]["amount"]
    assert amount["max_absolute"]["absolute_difference"] == "50"
    assert amount["max_absolute"]["values"] == {
        "A": "1300000000",
        "B": "1299999950",
        "C": "1300000000",
    }
