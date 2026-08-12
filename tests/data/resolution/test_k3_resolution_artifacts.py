from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION
from quantking.data.models import DatasetKind, DataUpstream
from quantking.data.resolution.artifacts import (
    ResolutionArtifactVerificationError,
    ResolutionArtifactWriter,
    verify_resolution_artifact,
)
from quantking.data.resolution.models import (
    K3_RESOLUTION_RULE_VERSION,
    ResolutionDecision,
    ResolutionReasonCode,
    ResolutionRequestTrace,
    ResolutionRun,
    ResolutionSourceBinding,
    ResolutionSourceStatus,
)

_DAY = date(2026, 8, 7)
_CREATED = UtcTimestamp.from_iso("2026-08-12T03:00:00Z")


def _id(namespace: str, suffix: int) -> EntityId:
    return parse_entity_id(f"{namespace}:00000000-0000-4000-8000-{suffix:012d}")


def _binding(upstream: DataUpstream, suffix: int) -> ResolutionSourceBinding:
    return ResolutionSourceBinding(
        upstream=upstream,
        status=ResolutionSourceStatus.AVAILABLE,
        batch_id=_id("data_batch", suffix),
        content_sha256=f"{suffix:x}" * 64,
    )


def _run() -> ResolutionRun:
    bindings = tuple(
        _binding(upstream, suffix)
        for upstream, suffix in zip(
            (
                DataUpstream.BAOSTOCK,
                DataUpstream.EASTMONEY,
                DataUpstream.TENCENT,
                DataUpstream.SINA,
            ),
            (1, 2, 3, 4),
            strict=True,
        )
    )
    reports = tuple(_id("data_diff", suffix) for suffix in range(11, 17))
    conflict = ResolutionDecision(
        symbol="600000.SH",
        trade_date=_DAY,
        source_bindings=bindings,
        compared_upstreams=(
            DataUpstream.BAOSTOCK,
            DataUpstream.EASTMONEY,
            DataUpstream.TENCENT,
            DataUpstream.SINA,
        ),
        agreeing_upstreams=(),
        selected_upstream=None,
        selected_row_present=None,
        reason_code=ResolutionReasonCode.NO_TWO_SOURCE_CONSENSUS,
        rule_version=K3_RESOLUTION_RULE_VERSION,
        difference_report_ids=reports,
        created_job_id=_id("job", 21),
        created_at=_CREATED,
        request_traces=(
            ResolutionRequestTrace(
                upstream=DataUpstream.TENCENT,
                symbol="600000.SH",
                start_date=_DAY,
                end_date=_DAY,
                trigger_reason="AB_AFFECTED_RANGE",
            ),
            ResolutionRequestTrace(
                upstream=DataUpstream.SINA,
                symbol="600000.SH",
                start_date=_DAY,
                end_date=_DAY,
                trigger_reason="ABC_NO_CONSENSUS",
            ),
        ),
    )
    return ResolutionRun(
        run_id=_id("data_resolution", 31),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_job_id=_id("job", 21),
        created_at=_CREATED,
        rule_version=K3_RESOLUTION_RULE_VERSION,
        threshold_rule_version=THRESHOLD_RULE_VERSION,
        input_fingerprint="f" * 64,
        candidate_version_id=None,
        input_bindings=bindings,
        difference_report_ids=reports,
        default_summary=None,
        run_blockers=(),
        exceptions=(),
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


def _resign_all(
    directory: Path,
    *,
    decision: dict[str, object],
    manifest: dict[str, object],
) -> None:
    decision_bytes = _canonical(decision) + b"\n"
    (directory / "conflicts.jsonl").write_bytes(decision_bytes)
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    files = unsigned["files"]
    assert isinstance(files, dict)
    files["conflicts.jsonl"] = {
        "sha256": hashlib.sha256(decision_bytes).hexdigest(),
        "size": len(decision_bytes),
        "row_count": 1,
    }
    digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
    (directory / "manifest.json").write_bytes(_canonical({**unsigned, "manifest_sha256": digest}))
    (directory / "COMPLETE").write_bytes(_canonical({"manifest_sha256": digest}))


def test_k3_v2_artifact_roundtrip_is_deterministic_and_has_no_legacy_c_request(
    tmp_path: Path,
) -> None:
    run = _run()
    writer = ResolutionArtifactWriter(artifacts_root=tmp_path)
    directory = writer.write(run)
    first = {item.name: item.read_bytes() for item in directory.iterdir()}

    verified = verify_resolution_artifact(directory, artifacts_root=tmp_path)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    decision = json.loads((directory / "conflicts.jsonl").read_bytes())
    assert manifest["schema_version"] == "resolution-artifact.v2"
    assert manifest["rule_version"] == K3_RESOLUTION_RULE_VERSION
    assert "request_traces" in decision
    assert "c_request" not in decision
    assert verified.conflicts == run.conflicts
    assert writer.write(run) == directory
    assert {item.name: item.read_bytes() for item in directory.iterdir()} == first


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "rule",
        "source_order",
        "trace_upstream",
        "trace_range",
        "not_called_trace",
        "xtquant_binding",
    ],
)
def test_k3_full_resign_tampering_is_rejected(tmp_path: Path, mutation: str) -> None:
    directory = ResolutionArtifactWriter(artifacts_root=tmp_path).write(_run())
    manifest = json.loads((directory / "manifest.json").read_bytes())
    decision = json.loads((directory / "conflicts.jsonl").read_bytes())
    if mutation == "schema":
        manifest["schema_version"] = "resolution-artifact.v1"
    elif mutation == "rule":
        manifest["rule_version"] = "source-resolution.daily-bar-unadjusted.k2.v1"
    elif mutation == "source_order":
        bindings = decision["source_bindings"]
        bindings[0], bindings[1] = bindings[1], bindings[0]
    elif mutation == "trace_upstream":
        decision["request_traces"][0]["upstream"] = "EASTMONEY"
    elif mutation == "trace_range":
        decision["request_traces"][0]["start_date"] = "2026-08-08"
    elif mutation == "not_called_trace":
        c_binding = decision["source_bindings"][2]
        c_binding.update({"status": "NOT_CALLED", "batch_id": None, "content_sha256": None})
    else:
        decision["source_bindings"][0]["upstream"] = "XTQUANT"
    _resign_all(directory, decision=decision, manifest=manifest)

    with pytest.raises(ResolutionArtifactVerificationError):
        verify_resolution_artifact(directory, artifacts_root=tmp_path)


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_k3_full_resign_summary_field_tampering_is_rejected(tmp_path: Path, mutation: str) -> None:
    directory = ResolutionArtifactWriter(artifacts_root=tmp_path).write(_run())
    summary = json.loads((directory / "summary.json").read_bytes())
    if mutation == "extra":
        summary["unexpected"] = True
    else:
        summary.pop("input_bindings")
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

    with pytest.raises(ResolutionArtifactVerificationError):
        verify_resolution_artifact(directory, artifacts_root=tmp_path)
