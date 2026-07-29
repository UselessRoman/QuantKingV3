"""Offline Fake-runner builders for validation trust-boundary tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.validation_content import (
    build_fake_exchange_calendar_parquet,
)
from quantking.data.adapters.warehouse.manifest import RawManifestFile
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher, RawBatchPublishRequest
from quantking.data.assessment.models import EvidenceKind, SourceCandidateAssessment
from quantking.data.models import DatasetKind, QualityRunStatus
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.models import (
    QualityOutcomeResult,
    QualityRunArtifact,
    RuleOutcomeStatus,
)
from quantking.data.validation.artifacts import ValidationEvidenceVerifier, ValidationEvidenceWriter
from quantking.data.validation.authorization import (
    FakeRunnerTrustStore,
    ResultAttestation,
)
from quantking.data.validation.models import (
    _VERIFICATION_SEAL,
    AssessmentGates,
    ValidationRunEvidence,
    ValidationVersions,
    VerifiedAssessmentEvidence,
    VerifiedValidationRun,
)
from quantking.data.validation.request import (
    ValidationRequestManifest,
    canonical_json,
    compute_run_fingerprint,
    sha256_json,
)
from quantking.data.validation.samples import load_packaged_fixed_sample
from quantking.data.validation.service import assess_verified_runs


def _id(namespace: str, index: int, *, lane: int) -> EntityId:
    return EntityId(namespace, UUID(f"00000000-0000-4{lane:03x}-8000-{index:012d}"))


def _collect(directory: Path, *, prefix: str) -> dict[str, tuple[bytes, int]]:
    result: dict[str, tuple[bytes, int]] = {}
    for path in directory.rglob("*"):
        if path.is_file():
            result[f"{prefix}/{path.relative_to(directory).as_posix()}"] = (path.read_bytes(), 0)
    return result


@dataclass(frozen=True, slots=True)
class BuiltFakeRun:
    directory: Path
    verifier: ValidationEvidenceVerifier


def build_deep_fake_run(
    *,
    root: Path,
    run_index: int,
    attack: str | None = None,
    assessment_id: EntityId | None = None,
    version_drift: str | None = None,
    request_drift: bool = False,
    invocation_id: EntityId | None = None,
    session_id: EntityId | None = None,
    started_at: UtcTimestamp | None = None,
    ended_at: UtcTimestamp | None = None,
) -> BuiltFakeRun:
    """Build structurally real-looking evidence without any external access."""
    sample = load_packaged_fixed_sample()
    source = "XTQUANT"
    dataset = DatasetKind.EXCHANGE_CALENDAR
    versions = ValidationVersions(
        adapter_version="fake-adapter.v1",
        package_version="fake-package.v1",
        code_version="fake-code.v1",
        schema_version="exchange-calendar.v1",
        rule_version="quality-rules.v1",
        threshold_version="comparison-thresholds.v1",
        sample_spec_version=sample.sample_spec_version,
        config_version="validation.v1",
    )
    if version_drift is not None:
        versions = replace(versions, **{version_drift: f"{version_drift}.drift"})
    request = ValidationRequestManifest.for_controlled_sample(
        source=source, dataset=dataset, sample=sample
    )
    request_bytes = request.to_bytes()
    request_sha = request.request_sha256
    if request_drift or attack == "request_missing_case":
        parsed = json.loads(request_bytes)
        parsed["cases"] = parsed["cases"][:-1]
        unsigned = dict(parsed)
        unsigned.pop("request_sha256")
        parsed["request_sha256"] = sha256_json(unsigned)
        request_bytes = canonical_json(parsed)
        request_sha = str(parsed["request_sha256"])
    sample_sha = "a" * 64 if attack == "sample_sha_drift" else sample.sample_spec_sha256
    fingerprint = compute_run_fingerprint(
        source=source,
        dataset=dataset,
        request_sha256=request_sha,
        sample_sha256=sample_sha,
        versions=versions.to_dict(),
    )
    run_id = _id("data_validation", run_index, lane=1)
    assessment = assessment_id or _id("data_assessment", 2, lane=2)
    invocation = invocation_id or _id("data_invocation", run_index, lane=3)
    session = session_id or _id("data_session", run_index, lane=4)
    authorization = _id("data_authorization", run_index, lane=5)
    start = started_at or UtcTimestamp(datetime(2026, 7, 29, 0, 0, run_index * 2, tzinfo=UTC))
    end = ended_at or UtcTimestamp(start.value + timedelta(seconds=1))
    trust = FakeRunnerTrustStore()

    cases = request.cases
    rows = sorted(
        {
            (str(case["exchange"]), day, True)
            for case in cases
            for day in (
                date.fromisoformat(str(case["start_date"])) + timedelta(days=offset)
                for offset in range(
                    (
                        date.fromisoformat(str(case["end_date"]))
                        - date.fromisoformat(str(case["start_date"]))
                    ).days
                    + 1
                )
            )
        }
    )
    parquet_bytes, content_sha, row_count = build_fake_exchange_calendar_parquet(tuple(rows))
    files: dict[str, tuple[bytes, int]] = {
        "request/manifest.json": (request_bytes, len(cases)),
        "canonical/content.parquet": (parquet_bytes, row_count),
    }

    raw_root = root / f"_fake-raw-{run_index}"
    raw_dir = RawBatchPublisher(data_root=raw_root).publish(
        RawBatchPublishRequest(
            source=source,
            dataset=dataset.value,
            batch_id=f"run-{run_index}",
            adapter_version=versions.adapter_version,
            code_version=versions.code_version,
            request_params={"request_sha256": request_sha, "run_id": str(run_id)},
            files=(RawManifestFile("payload.json", b'{"fake":true}', row_count),),
            created_at=start.to_iso(),
        )
    )
    files.update(_collect(raw_dir, prefix=f"raw-root/{raw_dir.relative_to(raw_root).as_posix()}"))

    quality_root = root / f"_fake-quality-{run_index}"
    quality = QualityRunArtifact(
        run_id=_id("data_quality", run_index, lane=6),
        version_id=_id("data_version", run_index, lane=7),
        rule_version=versions.rule_version,
        schema_version=versions.schema_version,
        content_sha256=content_sha,
        started_at=start,
        ended_at=end,
        status=QualityRunStatus.PASSED,
        results=(
            QualityOutcomeResult(
                rule_code="FAKE_OFFLINE_CONTRACT",
                status=RuleOutcomeStatus.PASSED,
                affected_count=0,
                summary="offline fake runner contract passed",
                row_evidence_relative_path=None,
            ),
        ),
        failure=None,
    )
    quality_dir = QualityArtifactWriter(artifacts_root=quality_root).write(
        outcome=quality, row_evidence_tables={}
    )
    files.update(
        _collect(
            quality_dir,
            prefix=f"quality-root/{quality_dir.relative_to(quality_root).as_posix()}",
        )
    )

    raw_manifest_key = next(
        key for key in files if key.startswith("raw-root/") and key.endswith("/manifest.json")
    )
    raw_manifest_sha = str(json.loads(files[raw_manifest_key][0])["manifest_sha256"])
    quality_manifest_key = next(
        key for key in files if key.startswith("quality-root/") and key.endswith("/manifest.json")
    )
    quality_manifest = json.loads(files[quality_manifest_key][0])
    quality_manifest_sha = str(quality_manifest["manifest_sha256"])
    quality_run_id = parse_entity_id(str(quality_manifest["run_id"]))
    inventory = [
        {
            "relative_path": relative,
            "row_count": item[1],
            "sha256": hashlib.sha256(item[0]).hexdigest(),
            "size": len(item[0]),
        }
        for relative, item in sorted(files.items())
    ]
    inventory_sha = hashlib.sha256(canonical_json(inventory)).hexdigest()
    attestation = trust.sign(
        ResultAttestation(
            signer_key_id=trust.key_id,
            invocation_id=invocation,
            session_id=session,
            authorization_id=authorization,
            run_id=run_id,
            source=source,
            dataset=dataset,
            request_sha256=request_sha,
            run_fingerprint=fingerprint,
            raw_manifest_sha256=raw_manifest_sha,
            canonical_content_sha256=content_sha,
            quality_manifest_sha256=quality_manifest_sha,
            quality_run_id=quality_run_id,
            difference_report_id=None,
            difference_report_sha256=None,
            evidence_inventory_sha256=inventory_sha,
            versions=versions.to_dict(),
            started_at=start,
            ended_at=end,
            signature="0" * 64,
        )
    )
    files["runner/result-attestation.json"] = (attestation.to_bytes(), 1)

    difference_id = (
        _id("data_diff", run_index, lane=8) if attack == "difference_claim_without_report" else None
    )
    if attack == "arbitrary_payloads":
        files["canonical/content.parquet"] = (b"arbitrary canonical bytes", 1)
        for key in tuple(files):
            if key.startswith("raw-root/") and key.endswith("payload.json"):
                files[key] = (b"arbitrary raw bytes", 1)
    if attack == "shallow_quality":
        files = {key: value for key, value in files.items() if not key.startswith("quality-root/")}
        files["quality/result.json"] = (b'{"status":"PASSED"}', 1)
    if attack == "missing_request":
        files.pop("request/manifest.json")
    if attack == "replace_raw_after_attestation":
        replacement_root = root / f"_fake-raw-replacement-{run_index}"
        replacement_dir = RawBatchPublisher(data_root=replacement_root).publish(
            RawBatchPublishRequest(
                source=source,
                dataset=dataset.value,
                batch_id=f"run-{run_index}",
                adapter_version=versions.adapter_version,
                code_version=versions.code_version,
                request_params={"request_sha256": request_sha, "run_id": str(run_id)},
                files=(RawManifestFile("payload.json", b'{"fake":"replacement"}', row_count),),
                created_at=start.to_iso(),
            )
        )
        files = {key: value for key, value in files.items() if not key.startswith("raw-root/")}
        files.update(
            _collect(
                replacement_dir,
                prefix=(f"raw-root/{replacement_dir.relative_to(replacement_root).as_posix()}"),
            )
        )
    elif attack == "replace_canonical_after_attestation":
        replacement_rows = tuple(
            (*row[:2], False) if index == len(rows) - 1 else row for index, row in enumerate(rows)
        )
        replacement_bytes, _replacement_sha, replacement_count = (
            build_fake_exchange_calendar_parquet(replacement_rows)
        )
        files["canonical/content.parquet"] = (replacement_bytes, replacement_count)
    elif attack == "replace_quality_after_attestation":
        replacement_root = root / f"_fake-quality-replacement-{run_index}"
        replacement_quality = replace(quality, run_id=_id("data_quality", run_index, lane=9))
        replacement_dir = QualityArtifactWriter(artifacts_root=replacement_root).write(
            outcome=replacement_quality, row_evidence_tables={}
        )
        files = {key: value for key, value in files.items() if not key.startswith("quality-root/")}
        files.update(
            _collect(
                replacement_dir,
                prefix=(f"quality-root/{replacement_dir.relative_to(replacement_root).as_posix()}"),
            )
        )

    envelope = ValidationRunEvidence(
        run_id=run_id,
        assessment_id=assessment,
        invocation_id=invocation,
        authorization_id=authorization,
        source=source,
        dataset=dataset,
        versions=versions,
        request_sha256=request_sha,
        sample_spec_sha256=sample_sha,
        run_fingerprint=fingerprint,
        started_at=start,
        ended_at=end,
        difference_report_id=difference_id,
        failure=None,
    )
    writer = ValidationEvidenceWriter(validation_root=root, runner_attestation_verifier=trust)
    directory = writer.write(run=envelope, files=files)
    return BuiltFakeRun(
        directory=directory,
        verifier=ValidationEvidenceVerifier(
            validation_root=root, runner_attestation_verifier=trust
        ),
    )


def build_deep_fake_assessment(
    *,
    root: Path,
    drift: str | None = None,
    independence_attack: str | None = None,
) -> VerifiedAssessmentEvidence:
    assessment_id = _id("data_assessment", 2, lane=2)
    common_invocation = _id("data_invocation", 99, lane=3)
    common_session = _id("data_session", 99, lane=4)
    common_start = UtcTimestamp(datetime(2026, 7, 29, 0, 1, tzinfo=UTC))
    common_end = UtcTimestamp(common_start.value + timedelta(seconds=1))
    built: list[BuiltFakeRun] = []
    for index in (1, 2, 3):
        version_drift = drift if index == 3 and drift != "request" else None
        built.append(
            build_deep_fake_run(
                root=root,
                run_index=index,
                assessment_id=assessment_id,
                version_drift=version_drift,
                request_drift=index == 3 and drift == "request",
                invocation_id=(
                    common_invocation if independence_attack == "same_invocation" else None
                ),
                session_id=(common_session if independence_attack == "same_session" else None),
                started_at=(common_start if independence_attack == "same_timestamps" else None),
                ended_at=(common_end if independence_attack == "same_timestamps" else None),
            )
        )
    writer = ValidationEvidenceWriter(
        validation_root=root,
        runner_attestation_verifier=FakeRunnerTrustStore(),
    )
    return writer.finalize_assessment(assessment_directory=built[0].directory.parent)


def build_synthetic_evidence_bundle(
    *, root: Path, run_text: str | None = None, run_index: int = 1
) -> tuple[Path, VerifiedAssessmentEvidence]:
    attack = "arbitrary_payloads" if run_text == "different" else None
    built = build_deep_fake_run(root=root, run_index=run_index, attack=attack)
    untrusted = ValidationEvidenceVerifier(validation_root=root)
    return built.directory, untrusted.verify(directory=built.directory)


def verified_synthetic_assessment(
    assessment: SourceCandidateAssessment,
) -> VerifiedAssessmentEvidence:
    """Repository-only synthetic wrapper with deterministic blocked evidence."""
    if assessment.evidence_kind is not EvidenceKind.SYNTHETIC:
        raise ValueError("testing helper cannot construct REAL evidence")
    sample = load_packaged_fixed_sample()
    run = VerifiedValidationRun(
        run_id=_id("data_validation", 11, lane=1),
        assessment_id=assessment.assessment_id,
        invocation_id=_id("data_invocation", 11, lane=3),
        session_id=_id("data_session", 11, lane=4),
        authorization_id=_id("data_authorization", 11, lane=5),
        source=assessment.source,
        dataset=assessment.dataset,
        versions=ValidationVersions(
            adapter_version="fixture.v1",
            package_version="not-applicable",
            code_version="s1-07a-r1.v1",
            schema_version="fixture-schema.v1",
            rule_version=assessment.rule_version,
            threshold_version="fixture-thresholds.v1",
            sample_spec_version=sample.sample_spec_version,
            config_version=assessment.assessment_config_version,
        ),
        request_sha256="1" * 64,
        sample_spec_sha256=sample.sample_spec_sha256,
        run_fingerprint="2" * 64,
        evidence_kind=EvidenceKind.SYNTHETIC,
        gates=AssessmentGates.none_passed(),
        raw_manifest_relative_path=None,
        raw_manifest_sha256=None,
        canonical_content_sha256=None,
        quality_run_id=None,
        quality_manifest_sha256=None,
        difference_report_id=None,
        difference_report_sha256=None,
        sample_eligibility=sample.eligibility,
        started_at=assessment.created_at,
        ended_at=UtcTimestamp(assessment.created_at.value + timedelta(microseconds=1)),
        failure=None,
        verification_reason="SYNTHETIC_FIXTURE",
        _seal=_VERIFICATION_SEAL,
    )
    derived = assess_verified_runs(
        verified_runs=(run,),
        evidence_relative_path=assessment.evidence_relative_path,
        evidence_sha256=assessment.evidence_sha256,
        created_at=assessment.created_at,
        sample_eligibility=sample.eligibility,
    )
    return VerifiedAssessmentEvidence(
        assessment=derived,
        verified_runs=(run,),
        evidence_manifest_sha256=assessment.evidence_sha256,
        evidence_relative_path=assessment.evidence_relative_path,
        common_run_fingerprint=run.run_fingerprint,
        sample_eligibility=sample.eligibility,
        _seal=_VERIFICATION_SEAL,
    )


__all__ = [
    "BuiltFakeRun",
    "build_deep_fake_assessment",
    "build_deep_fake_run",
    "build_synthetic_evidence_bundle",
    "verified_synthetic_assessment",
]
