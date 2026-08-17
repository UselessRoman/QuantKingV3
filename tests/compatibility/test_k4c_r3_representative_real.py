"""Real S1-07K4C-R3 representative three-source probe executor (Phase B).

The three user-accepted frozen samples (ADR-022) drive nine fixed endpoint
invocations: suspension 688237.SH window 2026-07-14..16, low-liquidity
688237.SH 2026-07-15 and legal pre-listing empty 688825.SH 2026-07-24
(listing 2026-07-27). The calendar proof for the legal-empty sample is built
from the frozen SHA-bound calendar reference BEFORE the first gateway call;
the file-backed attempt ledger rejects a consumed authorization_id before any
network traffic. Without the marker the test skips immediately, so the
default pytest run and ``scripts/quality.ps1`` never touch the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.adapters.sources.akshare import (
    AKSHARE_ADAPTER_VERSION,
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.baostock import (
    BAOSTOCK_ADAPTER_VERSION,
    BaostockDailyBarAdapter,
    BaostockMarketDataGateway,
)
from quantking.data.adapters.sources.baostock_worker import (
    BAOSTOCK_DISTRIBUTION_VERSION,
    BAOSTOCK_FIELDS,
)
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    VolumeUnit,
)
from quantking.data.adapters.sources.legal_empty import K4ExpectedSessionResolver
from quantking.data.adapters.sources.normalization import normalize_market_data_response
from quantking.data.adapters.sources.units import (
    AKSHARE_UNIT_POLICY_VERSION,
    BAOSTOCK_UNIT_POLICY_VERSION,
)
from quantking.data.comparison.k4c_evidence import (
    K4C_R3_EVIDENCE_SCHEMA,
    K4C_R3_INVOCATION_BUDGET,
    K4C_R3_MARKER,
    K4CAttemptLedger,
    K4CLedgerError,
    K4CVerifier,
    artifact_manifest_payload,
    comparison_rows_payload,
    k4c_r3_frozen_samples,
    k4c_r3_plan_invocations,
    k4c_r3_plan_sha256,
)
from quantking.data.comparison.k4c_probe import K4CProbeInvocation
from quantking.data.incremental import FetchChunk
from quantking.data.models import (
    DataProvider,
    DatasetKind,
    EmptyResultInfo,
    LegalEmptyClassification,
)

_AKSHARE_LOCKED_LIBRARY_VERSION = "1.18.83"
_EVIDENCE_PREFIX = "K4C_R3_EVIDENCE_JSON "
_ENV_MARKER = "QUANTKING_K4C_R3_PROBE_AUTHORIZATION"
_ENV_AUTHORIZATION_ID = "QUANTKING_K4C_R3_AUTHORIZATION_ID"
_ENV_OUTPUT_DIR = "QUANTKING_K4C_R3_OUTPUT_DIR"
_ROLES = ("A", "B", "C")

_CALENDAR_REFERENCE_RELATIVE = (
    "docs/stage1/evidence/k4c_r3_references/exchange_calendar_sh_2026-07.json"
)
_CALENDAR_REFERENCE_SHA256 = "244e62109be84c8dc0cb778c751c6aa9e2f98c4b50e4dd13073221e81432e10f"
_CALENDAR_COVERAGE_START = date(2026, 7, 1)
_CALENDAR_COVERAGE_END = date(2026, 7, 27)
_LEGAL_EMPTY_SYMBOL = "688825.SH"
_LEGAL_EMPTY_LISTING_DATE = date(2026, 7, 27)
_LEGAL_EMPTY_WINDOW_START = date(2026, 7, 24)
_LEGAL_EMPTY_WINDOW_END = date(2026, 7, 24)


def _request_sha256(invocation: K4CProbeInvocation) -> str:
    payload = {
        "provider": invocation.provider.value,
        "upstream": invocation.upstream.value,
        "symbol": invocation.symbol,
        "target_start": invocation.target_start.isoformat(),
        "target_end": invocation.target_end.isoformat(),
        "upstream_start": invocation.upstream_start.isoformat(),
        "upstream_end": invocation.upstream_end.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _emit(payload: dict[str, object]) -> None:
    print(
        _EVIDENCE_PREFIX
        + json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _failure_info(error: BaseException) -> dict[str, object]:
    if isinstance(error, SourceAdapterError):
        return {
            "stable_code": error.code.value,
            "error_type": type(error).__name__,
        }
    return {
        "stable_code": None,
        "error_type": type(error).__name__,
    }


def _outcome(
    invocation: K4CProbeInvocation,
    *,
    sequence: int,
    status: str,
    stable_code: str | None,
    library_version: str | None = None,
    contract_version: str | None = None,
    volume_unit: str | None = None,
    amount_unit: str | None = None,
    unit_policy_version: str | None = None,
    unadjusted: bool | None = None,
    complete: bool | None = None,
    target_row_count: int | None = None,
    upstream_row_count: int | None = None,
    content_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "role": invocation.role,
        "provider": invocation.provider.value,
        "upstream": invocation.upstream.value,
        "endpoint": invocation.endpoint,
        "symbol": invocation.symbol,
        "target_start": invocation.target_start.isoformat(),
        "target_end": invocation.target_end.isoformat(),
        "upstream_start": invocation.upstream_start.isoformat(),
        "upstream_end": invocation.upstream_end.isoformat(),
        "status": status,
        "stable_code": stable_code,
        "library_version": library_version,
        "contract_version": contract_version,
        "volume_unit": volume_unit,
        "amount_unit": amount_unit,
        "unit_policy_version": unit_policy_version,
        "unadjusted": unadjusted,
        "complete": complete,
        "target_row_count": target_row_count,
        "upstream_row_count": upstream_row_count,
        "content_sha256": content_sha256,
    }


def _calendar_classification(
    root: Path,
) -> tuple[EmptyResultInfo, dict[str, Mapping[str, object]]]:
    """Classify the pre-listing window against the frozen calendar reference.

    The reference file is SHA-bound; the resolver proves that 2026-07-24 is
    an SSE trading day whose expected session for 688825.SH is skipped by the
    pre-listing exception, i.e. no expected trading session exists for the
    requested security in the requested window. The returned
    ``EmptyResultInfo`` is the only classification the executor may feed to
    normalization for empty 688825 responses.
    """
    path = root / _CALENDAR_REFERENCE_RELATIVE
    if not path.is_file():
        pytest.fail("frozen calendar reference file is missing")
    if hashlib.sha256(path.read_bytes()).hexdigest() != _CALENDAR_REFERENCE_SHA256:
        pytest.fail("frozen calendar reference SHA mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = [
        {
            "exchange": str(row["exchange"]),
            "trade_date": date.fromisoformat(str(row["trade_date"])),
            "is_open": bool(row["is_open"]),
        }
        for row in payload["rows"]
    ]
    table = ensure_sorted(
        DatasetKind.EXCHANGE_CALENDAR.value,
        pa.Table.from_pylist(rows, schema=canonical_schema(DatasetKind.EXCHANGE_CALENDAR.value)),
    )
    resolver = K4ExpectedSessionResolver(
        calendar_table=table,
        expected_securities=(_LEGAL_EMPTY_SYMBOL,),
        security_exceptions={
            _LEGAL_EMPTY_SYMBOL: (
                (
                    TradingDate(_CALENDAR_COVERAGE_START),
                    TradingDate(_LEGAL_EMPTY_LISTING_DATE - timedelta(days=1)),
                ),
            )
        },
        coverage_start=_CALENDAR_COVERAGE_START,
        coverage_end=_CALENDAR_COVERAGE_END,
    )
    classification = resolver.classify(
        symbols=(_LEGAL_EMPTY_SYMBOL,),
        start_date=_LEGAL_EMPTY_WINDOW_START,
        end_date=_LEGAL_EMPTY_WINDOW_END,
    )
    if (
        classification is None
        or classification.classification is not LegalEmptyClassification.NO_TRADING_SESSION
    ):
        pytest.fail("frozen calendar does not classify the window as legal pre-listing empty")
    proofs = {
        _LEGAL_EMPTY_SYMBOL: {
            "no_expected_trading_sessions": True,
            "classification": classification.classification.value,
            "reason": classification.reason,
            "calendar_reference_sha256": _CALENDAR_REFERENCE_SHA256,
            "listing_date": _LEGAL_EMPTY_LISTING_DATE.isoformat(),
        }
    }
    return classification, proofs


@pytest.mark.real_k4c_probe
def test_fixed_bounded_k4c_r3_representative_probe() -> None:
    """Execute the frozen R3 plan under a one-shot file-backed authorization."""
    if os.environ.get(_ENV_MARKER) != K4C_R3_MARKER:
        pytest.skip("K4C-R3 probe requires the frozen marker plus a fresh authorization id")
    authorization_id = os.environ.get(_ENV_AUTHORIZATION_ID)
    if not authorization_id:
        pytest.skip("K4C-R3 probe requires QUANTKING_K4C_R3_AUTHORIZATION_ID")
    output_dir = os.environ.get(_ENV_OUTPUT_DIR)
    if not output_dir:
        pytest.skip("K4C-R3 probe requires QUANTKING_K4C_R3_OUTPUT_DIR")

    root = Path(__file__).resolve().parents[2]
    samples = k4c_r3_frozen_samples()
    plan = k4c_r3_plan_invocations(samples)
    assert len(plan) == K4C_R3_INVOCATION_BUDGET
    legal_empty_result, calendar_proofs = _calendar_classification(root)
    ledger = K4CAttemptLedger(Path(output_dir))
    header = ledger.begin(
        authorization_id=authorization_id,
        marker=K4C_R3_MARKER,
        plan_sha256=k4c_r3_plan_sha256(samples),
        invocation_budget=K4C_R3_INVOCATION_BUDGET,
        created_at=datetime.now(UTC).isoformat(),
    )
    attempt_directory = ledger.attempt_directory(authorization_id)
    rows_by_role: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {
        role: {} for role in _ROLES
    }
    rows_by_role_count: dict[str, int] = {role: 0 for role in _ROLES}
    try:
        for sequence, invocation in enumerate(plan, start=1):
            if invocation.provider is DataProvider.BAOSTOCK:
                gateway = BaostockMarketDataGateway(BaostockDailyBarAdapter())
            else:
                gateway = AkshareMarketDataGateway(AkshareDailyBarAdapter(invocation.upstream))
            chunk = FetchChunk(
                sequence=sequence,
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                scope=(invocation.symbol,),
                start=TradingDate(invocation.target_start),
                end=TradingDate(invocation.target_end),
                request_sha256=_request_sha256(invocation),
            )
            response = gateway.fetch(chunk)
            contract = response.contract
            lineage = contract.lineage
            assert lineage.provider is invocation.provider
            assert lineage.upstream is invocation.upstream
            assert lineage.endpoint == invocation.endpoint
            expected_library = (
                BAOSTOCK_DISTRIBUTION_VERSION
                if invocation.provider is DataProvider.BAOSTOCK
                else _AKSHARE_LOCKED_LIBRARY_VERSION
            )
            assert lineage.library_version == expected_library
            expected_contract = (
                BAOSTOCK_ADAPTER_VERSION
                if invocation.provider is DataProvider.BAOSTOCK
                else AKSHARE_ADAPTER_VERSION
            )
            assert contract.contract_version == expected_contract
            assert contract.dataset is DatasetKind.DAILY_BAR_UNADJUSTED
            assert contract.volume_unit is not VolumeUnit.UNVERIFIED
            assert contract.amount_unit is AmountUnit.CNY_YUAN
            assert contract.unadjusted is True
            assert response.complete is True
            window = response.request_window
            assert window.target_start == invocation.target_start
            assert window.target_end == invocation.target_end
            assert window.upstream_start == invocation.upstream_start
            assert window.upstream_end == invocation.upstream_end
            assert window.lookback_days == 15
            if invocation.provider is DataProvider.BAOSTOCK:
                raw_envelope = json.loads(response.raw_payload)
                provider_payload = raw_envelope["provider_payload"]
                assert provider_payload["fields"] == list(BAOSTOCK_FIELDS)
                unit_policy_version = provider_payload["response_metadata"]["unit_policy_version"]
                assert unit_policy_version == BAOSTOCK_UNIT_POLICY_VERSION
            else:
                raw_envelope = json.loads(response.raw_payload)
                unit_policy_version = str(raw_envelope["provider_payload"]["unit_policy_version"])
                assert unit_policy_version == AKSHARE_UNIT_POLICY_VERSION
            table = normalize_market_data_response(
                DatasetKind.DAILY_BAR_UNADJUSTED,
                response,
                empty_result=(
                    legal_empty_result if invocation.symbol == _LEGAL_EMPTY_SYMBOL else None
                ),
            )
            for row in table.to_pylist():
                assert row["code"] == invocation.symbol
                assert invocation.target_start <= row["trade_date"] <= invocation.target_end
                rows_by_role[invocation.role][
                    (invocation.symbol, row["trade_date"].isoformat())
                ] = row
            rows_by_role_count[invocation.role] += table.num_rows
            (attempt_directory / "artifacts" / "raw" / f"{sequence}.json").write_bytes(
                response.raw_payload
            )
            ledger.record(
                authorization_id,
                sequence=sequence,
                outcome=_outcome(
                    invocation,
                    sequence=sequence,
                    status="SUCCEEDED",
                    stable_code=None,
                    library_version=lineage.library_version,
                    contract_version=contract.contract_version,
                    volume_unit=contract.volume_unit.value,
                    amount_unit=contract.amount_unit.value,
                    unit_policy_version=unit_policy_version,
                    unadjusted=True,
                    complete=True,
                    target_row_count=table.num_rows,
                    upstream_row_count=response.upstream_row_count,
                    content_sha256=hashlib.sha256(response.raw_payload).hexdigest(),
                ),
            )
    except Exception as error:
        failure = _failure_info(error)
        invoked, _ = ledger.counts(authorization_id)
        if invoked < len(plan):
            failed_invocation = plan[invoked]
            with contextlib.suppress(K4CLedgerError):
                ledger.record(
                    authorization_id,
                    sequence=invoked + 1,
                    outcome={
                        **failed_invocation.to_dict(),
                        "sequence": invoked + 1,
                        "status": "FAILED",
                        "stable_code": failure["stable_code"],
                        "error_type": failure["error_type"],
                    },
                )
        invoked, not_called = ledger.counts(authorization_id)
        _emit(
            {
                "schema_version": K4C_R3_EVIDENCE_SCHEMA,
                "authorization_marker": K4C_R3_MARKER,
                "plan_sha256": k4c_r3_plan_sha256(samples),
                "authorization_id": authorization_id,
                "attempt_id": header.attempt_id,
                "invocation_budget": K4C_R3_INVOCATION_BUDGET,
                "invocation_count": invoked,
                "not_called_invocation_count": not_called,
                "failure": failure,
                "planned_objectives": [sample.to_dict() for sample in samples],
                "observed_objectives": [],
                "calendar_proofs": calendar_proofs,
                "representative_evidence_sufficient": False,
            }
        )
        raise

    comparison_payload = comparison_rows_payload(rows_by_role)
    comparison_text = json.dumps(
        comparison_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    comparison_path = attempt_directory / "artifacts" / "comparison-rows.json"
    comparison_path.write_text(comparison_text + "\n", encoding="utf-8", newline="\n")
    raw_shas: dict[int, str] = {}
    for sequence in range(1, K4C_R3_INVOCATION_BUDGET + 1):
        path = attempt_directory / "artifacts" / "raw" / f"{sequence}.json"
        if path.is_file():
            raw_shas[sequence] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_payload = artifact_manifest_payload(
        comparison_rows_sha256=hashlib.sha256(comparison_path.read_bytes()).hexdigest(),
        raw_shas=raw_shas,
        rows_by_role_count=rows_by_role_count,
    )
    manifest_path = attempt_directory / "artifacts" / "artifact-manifest.json"
    manifest_path.write_text(
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    r3_verifier = K4CVerifier(
        plan=plan,
        marker=K4C_R3_MARKER,
        plan_sha256=k4c_r3_plan_sha256(samples),
        invocation_budget=K4C_R3_INVOCATION_BUDGET,
        r3_samples=samples,
    )
    recomputed = r3_verifier.recompute(attempt_directory, calendar_proofs=calendar_proofs)
    if recomputed.reasons:
        _emit(
            {
                "schema_version": K4C_R3_EVIDENCE_SCHEMA,
                "authorization_marker": K4C_R3_MARKER,
                "plan_sha256": k4c_r3_plan_sha256(samples),
                "authorization_id": authorization_id,
                "attempt_id": header.attempt_id,
                "invocation_budget": K4C_R3_INVOCATION_BUDGET,
                "invocation_count": recomputed.recomputed["invocation_count"],
                "not_called_invocation_count": recomputed.recomputed["not_called_invocation_count"],
                "failure": {"error_type": "VERIFIER_FAILED", "stable_code": None},
                "planned_objectives": [],
                "observed_objectives": [],
                "calendar_proofs": calendar_proofs,
                "representative_evidence_sufficient": False,
                "verifier_reasons": list(recomputed.reasons),
            }
        )
        pytest.fail(f"K4C-R3 evidence failed verification: {recomputed.reasons}")

    evidence: dict[str, object] = {
        "schema_version": K4C_R3_EVIDENCE_SCHEMA,
        "authorization_marker": K4C_R3_MARKER,
        "plan_sha256": k4c_r3_plan_sha256(samples),
        "authorization_id": authorization_id,
        "attempt_id": header.attempt_id,
        "invocation_budget": K4C_R3_INVOCATION_BUDGET,
        "invocation_count": recomputed.recomputed["invocation_count"],
        "not_called_invocation_count": recomputed.recomputed["not_called_invocation_count"],
        "failure": None,
        "planned_objectives": [sample.to_dict() for sample in samples],
        "observed_objectives": recomputed.recomputed["observed_objectives"],
        "calendar_proofs": calendar_proofs,
        "representative_evidence_sufficient": recomputed.recomputed[
            "representative_evidence_sufficient"
        ],
        "cross_source": recomputed.recomputed["statistics"],
        "contract_checks": recomputed.recomputed["contract_checks"],
        "threshold_coverage": recomputed.recomputed["threshold_coverage"],
        "sufficiency_reasons": recomputed.recomputed["sufficiency_reasons"],
        "artifact_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "artifacts_root": str(attempt_directory),
        "application_retries": 0,
        "fail_fast": True,
        "writes_formal_data_root": False,
    }
    verified = r3_verifier.verify(attempt_directory=attempt_directory, evidence=evidence)
    if not verified.ok:
        pytest.fail(f"K4C-R3 evidence failed cross-check: {verified.reasons}")
    (attempt_directory / "evidence.json").write_text(
        json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _emit(evidence)
