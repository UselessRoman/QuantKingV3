"""Real S1-07K4C-R2 representative three-source probe executor.

The retired v1 probe (marker ``K4C-REPRESENTATIVE-2026-08-14-v1``) was
rejected by independent acceptance and must never run again. This executor
only runs with the frozen R2 marker AND a fresh authorization_id supplied in
the environment; the file-backed attempt ledger rejects a consumed
authorization_id before the first gateway call. Without the marker the test
skips immediately, so the default pytest run and ``scripts/quality.ps1``
never touch the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantking.common.time import TradingDate
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
from quantking.data.adapters.sources.normalization import normalize_market_data_response
from quantking.data.adapters.sources.units import (
    AKSHARE_UNIT_POLICY_VERSION,
    BAOSTOCK_UNIT_POLICY_VERSION,
)
from quantking.data.comparison.k4c_evidence import (
    K4C_R2_EVIDENCE_SCHEMA,
    K4C_R2_INVOCATION_BUDGET,
    K4C_R2_MARKER,
    K4C_R2_OBJECTIVES,
    K4CAttemptLedger,
    K4CLedgerError,
    K4CVerifier,
    artifact_manifest_payload,
    comparison_rows_payload,
    k4c_r2_plan_sha256,
)
from quantking.data.comparison.k4c_probe import (
    K4CProbeInvocation,
    fixed_k4c_probe_plan,
)
from quantking.data.incremental import FetchChunk
from quantking.data.models import DataProvider, DatasetKind

_AKSHARE_LOCKED_LIBRARY_VERSION = "1.18.83"
_EVIDENCE_PREFIX = "K4C_R2_EVIDENCE_JSON "
_ENV_MARKER = "QUANTKING_K4C_R2_PROBE_AUTHORIZATION"
_ENV_AUTHORIZATION_ID = "QUANTKING_K4C_R2_AUTHORIZATION_ID"
_ENV_OUTPUT_DIR = "QUANTKING_K4C_R2_OUTPUT_DIR"
_ROLES = ("A", "B", "C")


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


@pytest.mark.real_k4c_probe
def test_fixed_bounded_k4c_r2_representative_probe() -> None:
    """Execute the frozen R2 plan under a one-shot file-backed authorization.

    The attempt header is created (exclusively) before the first gateway
    call; every invocation result is appended and flushed to the journal
    immediately; all compared rows and raw envelopes are written as canonical
    artifacts; the offline verifier then recomputes every summary value and
    the evidence JSON is only emitted after that recomputation passes.
    """
    if os.environ.get(_ENV_MARKER) != K4C_R2_MARKER:
        pytest.skip("K4C-R2 probe requires the frozen marker plus a fresh authorization id")
    authorization_id = os.environ.get(_ENV_AUTHORIZATION_ID)
    if not authorization_id:
        pytest.skip("K4C-R2 probe requires QUANTKING_K4C_R2_AUTHORIZATION_ID")
    output_dir = os.environ.get(_ENV_OUTPUT_DIR)
    if not output_dir:
        pytest.skip("K4C-R2 probe requires QUANTKING_K4C_R2_OUTPUT_DIR")

    plan = fixed_k4c_probe_plan()
    assert len(plan) == K4C_R2_INVOCATION_BUDGET
    ledger = K4CAttemptLedger(Path(output_dir))
    header = ledger.begin(
        authorization_id=authorization_id,
        marker=K4C_R2_MARKER,
        plan_sha256=k4c_r2_plan_sha256(),
        invocation_budget=K4C_R2_INVOCATION_BUDGET,
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
            table = normalize_market_data_response(DatasetKind.DAILY_BAR_UNADJUSTED, response)
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
                "schema_version": K4C_R2_EVIDENCE_SCHEMA,
                "authorization_marker": K4C_R2_MARKER,
                "plan_sha256": k4c_r2_plan_sha256(),
                "authorization_id": authorization_id,
                "attempt_id": header.attempt_id,
                "invocation_budget": K4C_R2_INVOCATION_BUDGET,
                "invocation_count": invoked,
                "not_called_invocation_count": not_called,
                "failure": failure,
                "planned_objectives": [item.to_dict() for item in K4C_R2_OBJECTIVES],
                "observed_objectives": [],
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
    for sequence in range(1, K4C_R2_INVOCATION_BUDGET + 1):
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

    recomputed = K4CVerifier().recompute(attempt_directory)
    if recomputed.reasons:
        _emit(
            {
                "schema_version": K4C_R2_EVIDENCE_SCHEMA,
                "authorization_marker": K4C_R2_MARKER,
                "plan_sha256": k4c_r2_plan_sha256(),
                "authorization_id": authorization_id,
                "attempt_id": header.attempt_id,
                "invocation_budget": K4C_R2_INVOCATION_BUDGET,
                "invocation_count": recomputed.recomputed["invocation_count"],
                "not_called_invocation_count": recomputed.recomputed["not_called_invocation_count"],
                "failure": {"error_type": "VERIFIER_FAILED", "stable_code": None},
                "planned_objectives": [],
                "observed_objectives": [],
                "representative_evidence_sufficient": False,
                "verifier_reasons": list(recomputed.reasons),
            }
        )
        pytest.fail(f"K4C-R2 evidence failed verification: {recomputed.reasons}")

    evidence: dict[str, object] = {
        "schema_version": K4C_R2_EVIDENCE_SCHEMA,
        "authorization_marker": K4C_R2_MARKER,
        "plan_sha256": k4c_r2_plan_sha256(),
        "authorization_id": authorization_id,
        "attempt_id": header.attempt_id,
        "invocation_budget": K4C_R2_INVOCATION_BUDGET,
        "invocation_count": recomputed.recomputed["invocation_count"],
        "not_called_invocation_count": recomputed.recomputed["not_called_invocation_count"],
        "failure": None,
        "planned_objectives": [item.to_dict() for item in K4C_R2_OBJECTIVES],
        "observed_objectives": recomputed.recomputed["observed_objectives"],
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
    verified = K4CVerifier().verify(attempt_directory=attempt_directory, evidence=evidence)
    if not verified.ok:
        pytest.fail(f"K4C-R2 evidence failed cross-check: {verified.reasons}")
    _emit(evidence)
