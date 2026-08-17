from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from quantking.common.ids import parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION, default_thresholds
from quantking.data.models import DatasetKind, DataUpstream
from quantking.data.resolution import (
    ResolutionArtifactVerificationError,
    ResolutionArtifactVerifier,
    ResolutionArtifactWriter,
    ResolutionDecision,
    ResolutionDefaultSummary,
    ResolutionReasonCode,
    ResolutionRun,
    ResolutionRunRecord,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
    verify_resolution_artifact,
)
from quantking.data.resolution.models import K4_RESOLUTION_RULE_VERSION, ResolutionRequestTrace

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64

_A, _B, _C = (
    DataUpstream.BAOSTOCK,
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)


def _id(namespace: str, suffix: int) -> object:
    return parse_entity_id(f"{namespace}:00000000-0000-4000-8000-{suffix:012d}")


def _binding(
    upstream: DataUpstream,
    status: ResolutionSourceStatus,
    suffix: int | None,
    sha: str | None,
) -> ResolutionSourceBinding:
    return ResolutionSourceBinding(
        upstream=upstream,
        status=status,
        batch_id=None if suffix is None else _id("data_batch", suffix),  # type: ignore[arg-type]
        content_sha256=sha,
    )


def _trace(symbol: str, day: date) -> ResolutionRequestTrace:
    return ResolutionRequestTrace(
        upstream=_C,
        symbol=symbol,
        start_date=day,
        end_date=day,
        trigger_reason="AB_AFFECTED_RANGE",
    )


def _run() -> ResolutionRun:
    a = _binding(_A, ResolutionSourceStatus.AVAILABLE, 1, _SHA_A)
    b = _binding(_B, ResolutionSourceStatus.AVAILABLE, 2, _SHA_B)
    c = _binding(_C, ResolutionSourceStatus.AVAILABLE, 3, _SHA_C)
    default_c = _binding(_C, ResolutionSourceStatus.NOT_CALLED, None, None)
    reports = tuple(_id("data_diff", item) for item in (11, 12, 13))
    job = _id("job", 21)
    created = UtcTimestamp.from_iso("2026-08-10T01:02:03Z")
    exception = ResolutionDecision(
        symbol="600000.SH",
        trade_date=date(2026, 8, 7),
        source_bindings=(a, b, c),
        compared_upstreams=(_A, _B, _C),
        agreeing_upstreams=(_A, _C),
        selected_upstream=_A,
        selected_row_present=True,
        reason_code=ResolutionReasonCode.AC_AGREE_A_SELECTED,
        rule_version=K4_RESOLUTION_RULE_VERSION,
        difference_report_ids=reports,
        created_job_id=job,  # type: ignore[arg-type]
        created_at=created,
        request_traces=(_trace("600000.SH", date(2026, 8, 7)),),
    )
    conflict = ResolutionDecision(
        symbol="600000.SH",
        trade_date=date(2026, 8, 8),
        source_bindings=(a, b, c),
        compared_upstreams=(_A, _B, _C),
        agreeing_upstreams=(),
        selected_upstream=None,
        selected_row_present=None,
        reason_code=ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS,
        rule_version=K4_RESOLUTION_RULE_VERSION,
        difference_report_ids=reports,
        created_job_id=job,  # type: ignore[arg-type]
        created_at=created,
        request_traces=(_trace("600000.SH", date(2026, 8, 8)),),
    )
    return ResolutionRun(
        run_id=_id("data_resolution", 31),  # type: ignore[arg-type]
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_job_id=job,  # type: ignore[arg-type]
        created_at=created,
        rule_version=K4_RESOLUTION_RULE_VERSION,
        threshold_rule_version=THRESHOLD_RULE_VERSION,
        input_fingerprint="f" * 64,
        candidate_version_id=None,
        input_bindings=(a, b, c),
        difference_report_ids=reports,  # type: ignore[arg-type]
        default_summary=ResolutionDefaultSummary(
            source_bindings=(a, b, default_c),
            compared_upstreams=(_A, _B),
            agreeing_upstreams=(_A, _B),
            selected_upstream=_A,
            reason_code=ResolutionReasonCode.AB_AGREE_DEFAULT,
            decision_count=2,
        ),
        run_blockers=(),
        exceptions=(exception,),
        conflicts=(conflict,),
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _resign(directory: Path, summary: dict[str, object]) -> None:
    summary_bytes = _canonical(summary)
    (directory / "summary.json").write_bytes(summary_bytes)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["files"]["summary.json"] = {
        "sha256": hashlib.sha256(summary_bytes).hexdigest(),
        "size": len(summary_bytes),
        "row_count": 1,
    }
    manifest.pop("manifest_sha256")
    digest = hashlib.sha256(_canonical(manifest)).hexdigest()
    (directory / "manifest.json").write_bytes(_canonical({**manifest, "manifest_sha256": digest}))
    (directory / "COMPLETE").write_bytes(_canonical({"manifest_sha256": digest}))


def test_k1_thresholds_are_daily_only_and_zero_tolerance() -> None:
    thresholds = default_thresholds()

    assert thresholds.rule_version == "comparison-thresholds.daily-bar-unadjusted.k1.v1"
    daily = thresholds.for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED)
    assert set(daily) == {"open", "high", "low", "close", "pre_close", "volume", "amount"}
    assert all(item.absolute == 0 and item.relative == 0 for item in daily.values())
    assert all(item.exceeds(1.0, 1.0000000001) for item in daily.values())
    with pytest.raises(ValueError, match="daily_bar_unadjusted only"):
        thresholds.for_dataset(DatasetKind.ADJUSTMENT_FACTOR)


def test_artifact_is_deterministic_and_strictly_reconstructable(tmp_path: Path) -> None:
    run = _run()
    writer = ResolutionArtifactWriter(artifacts_root=tmp_path)

    directory = writer.write(run)
    first = {item.name: item.read_bytes() for item in directory.iterdir()}
    assert writer.write(run) == directory
    assert {item.name: item.read_bytes() for item in directory.iterdir()} == first

    verified = verify_resolution_artifact(directory, artifacts_root=tmp_path)
    assert verified.run_id == run.run_id
    assert verified.summary == run.summary_dict()
    assert verified.exceptions == run.exceptions
    assert verified.conflicts == run.conflicts
    assert verified.summary["default_decision"]["source_bindings"][2] == {
        "upstream": "SINA",
        "status": "NOT_CALLED",
        "batch_id": None,
        "content_sha256": None,
    }


@pytest.mark.parametrize(
    "mutation",
    ["sha", "path", "run", "job", "fingerprint", "counts"],
)
def test_record_verifier_binds_every_sqlite_summary_credential(
    tmp_path: Path,
    mutation: str,
) -> None:
    run = _run()
    artifact = verify_resolution_artifact(
        ResolutionArtifactWriter(artifacts_root=tmp_path).write(run),
        artifacts_root=tmp_path,
    )
    record = ResolutionRunRecord(
        run_id=run.run_id,
        dataset=run.dataset,
        created_job_id=run.created_job_id,
        candidate_version_id=run.candidate_version_id,
        created_at=run.created_at,
        rule_version=run.rule_version,
        threshold_rule_version=run.threshold_rule_version,
        input_fingerprint=run.input_fingerprint,
        artifact_relative_path=artifact.relative_path,
        manifest_sha256=artifact.manifest_sha256,
        total_decisions=run.total_decisions,
        default_decision_count=run.default_decision_count,
        exception_count=len(run.exceptions),
        conflict_count=len(run.conflicts),
        blocker_count=len(run.run_blockers),
    )
    verifier = ResolutionArtifactVerifier(artifacts_root=tmp_path)

    assert verifier.verify(record) == artifact
    tampered = {
        "sha": replace(record, manifest_sha256="9" * 64),
        "path": replace(record, artifact_relative_path="data-resolution/missing"),
        "run": replace(record, run_id=_id("data_resolution", 99)),
        "job": replace(record, created_job_id=_id("job", 99)),
        "fingerprint": replace(record, input_fingerprint="8" * 64),
        "counts": replace(record, total_decisions=5, exception_count=2),
    }[mutation]
    with pytest.raises(ResolutionArtifactVerificationError, match="record"):
        verifier.verify(tampered)


def test_record_verifier_binds_resolved_candidate_id(tmp_path: Path) -> None:
    run = replace(
        _run(),
        candidate_version_id=_id("data_version", 41),
        conflicts=(),
    )
    artifact = verify_resolution_artifact(
        ResolutionArtifactWriter(artifacts_root=tmp_path).write(run),
        artifacts_root=tmp_path,
    )
    record = ResolutionRunRecord(
        run_id=run.run_id,
        dataset=run.dataset,
        created_job_id=run.created_job_id,
        candidate_version_id=run.candidate_version_id,
        created_at=run.created_at,
        rule_version=run.rule_version,
        threshold_rule_version=run.threshold_rule_version,
        input_fingerprint=run.input_fingerprint,
        artifact_relative_path=artifact.relative_path,
        manifest_sha256=artifact.manifest_sha256,
        total_decisions=run.total_decisions,
        default_decision_count=run.default_decision_count,
        exception_count=len(run.exceptions),
        conflict_count=0,
        blocker_count=0,
    )
    verifier = ResolutionArtifactVerifier(artifacts_root=tmp_path)

    assert verifier.verify(record) == artifact
    with pytest.raises(ResolutionArtifactVerificationError, match="record"):
        verifier.verify(replace(record, candidate_version_id=_id("data_version", 42)))


@pytest.mark.parametrize("mutation", ["extra", "missing", "default_count", "default_source"])
def test_resigned_semantic_summary_tampering_is_rejected(
    tmp_path: Path,
    mutation: str,
) -> None:
    directory = ResolutionArtifactWriter(artifacts_root=tmp_path).write(_run())
    summary = json.loads((directory / "summary.json").read_bytes())
    if mutation == "extra":
        summary["unexpected"] = True
    elif mutation == "missing":
        summary.pop("rule_version")
    elif mutation == "default_count":
        summary["default_decision"]["decision_count"] = 9
    else:
        summary["default_decision"]["source_bindings"][2]["content_sha256"] = "d" * 64
    _resign(directory, summary)

    with pytest.raises(ResolutionArtifactVerificationError):
        verify_resolution_artifact(directory, artifacts_root=tmp_path)


def test_not_called_and_reason_combinations_reject_fabricated_or_malformed_state() -> None:
    with pytest.raises(ValueError, match="NOT_CALLED"):
        _binding(_C, ResolutionSourceStatus.NOT_CALLED, 3, _SHA_C)

    run = _run()
    decision = run.exceptions[0]
    with pytest.raises(ValueError, match="reason_code"):
        ResolutionDecision(
            symbol=decision.symbol,
            trade_date=decision.trade_date,
            source_bindings=decision.source_bindings,
            compared_upstreams=decision.compared_upstreams,
            agreeing_upstreams=(_B, _C),
            selected_upstream=_B,
            selected_row_present=True,
            reason_code=ResolutionReasonCode.AC_AGREE_A_SELECTED,
            rule_version=decision.rule_version,
            difference_report_ids=decision.difference_report_ids,
            created_job_id=decision.created_job_id,
            created_at=decision.created_at,
            request_traces=decision.request_traces,
        )


def test_run_rejects_unbound_duplicate_or_cross_job_decisions() -> None:
    run = _run()
    decision = run.exceptions[0]

    with pytest.raises(ValueError, match="run job"):
        replace(
            run,
            exceptions=(replace(decision, created_job_id=_id("job", 99)),),
        )
    with pytest.raises(ValueError, match=r"exceptional source|absent from run inputs"):
        replace(run, input_bindings=run.input_bindings[:2])
    duplicate = replace(
        run.conflicts[0],
        symbol=decision.symbol,
        trade_date=decision.trade_date,
        request_traces=decision.request_traces,
    )
    with pytest.raises(ValueError, match="globally unique"):
        replace(run, conflicts=(duplicate,))


def test_c_trace_must_cover_exact_decision_symbol_and_date() -> None:
    decision = _run().exceptions[0]
    trace = decision.request_traces[0]

    with pytest.raises(ValueError, match="cover"):
        replace(decision, request_traces=(replace(trace, symbol="000001.SZ"),))
    with pytest.raises(ValueError, match="cover"):
        replace(
            decision,
            request_traces=(
                replace(
                    trace,
                    start_date=date(2026, 8, 6),
                    end_date=date(2026, 8, 6),
                ),
            ),
        )


def test_input_binding_sort_is_total_over_content_sha() -> None:
    run = _run()
    a = run.input_bindings[0]
    other_a = replace(a, content_sha256="d" * 64)

    with pytest.raises(ValueError, match="sorted and unique"):
        replace(run, input_bindings=(other_a, a, *run.input_bindings[1:]))
