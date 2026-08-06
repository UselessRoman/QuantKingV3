"""Offline builders for S1-07C0A v3 contract and attack tests.

They only use the permanently SYNTHETIC fake signer.  No helper creates a
REAL identity, imports a provider SDK, or accesses a runtime data root.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.validation_content import (
    build_fake_exchange_calendar_parquet,
    build_fake_exchange_calendar_table,
)
from quantking.data.adapters.warehouse.manifest import RawManifestFile
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher, RawBatchPublishRequest
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import CrossSourceComparator, CrossSourceComparisonRequest
from quantking.data.comparison.thresholds import default_thresholds
from quantking.data.models import DatasetKind, DataSource, QualityRunStatus
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.models import (
    QualityOutcomeResult,
    QualityRunArtifact,
    RuleOutcomeStatus,
)
from quantking.data.schemas import descriptor_for
from quantking.data.validation.artifacts_v3 import (
    V3EvidenceTrustContext,
    ValidationEvidenceVerifierV3,
    ValidationEvidenceWriterV3,
    ValidationRunEnvelopeV3,
)
from quantking.data.validation.contracts_v3 import (
    AttemptOutcomeV3,
    FakeV3RunnerTrustStore,
    PromotionProfileV3,
    ResultAttestationV3,
    SourceDatasetContractV3,
    SuccessEvidenceBindingV3,
    ValidationAttemptIdentityV3,
    ValidationRequestManifestV3,
    canonical_json,
    compute_run_fingerprint_v3,
)
from quantking.data.validation.models import ValidationFailure, ValidationVersions
from quantking.data.validation.quality import expected_quality_rule_codes
from quantking.data.validation.samples import load_fixed_sample_v4


def _id(namespace: str, index: int, *, lane: int) -> EntityId:
    return EntityId(namespace, UUID(f"10000000-0000-4{lane:03x}-8000-{index:012d}"))


def _collect(directory: Path, *, prefix: str) -> dict[str, tuple[bytes, int]]:
    return {
        f"{prefix}/{path.relative_to(directory).as_posix()}": (path.read_bytes(), 0)
        for path in directory.rglob("*")
        if path.is_file()
    }


def build_v3_context() -> V3EvidenceTrustContext:
    """An in-memory controlled context, not a production trust release."""
    sample = load_fixed_sample_v4()
    dataset = DatasetKind.EXCHANGE_CALENDAR
    return V3EvidenceTrustContext(
        sample=sample,
        source_dataset_contract=SourceDatasetContractV3(
            source="XTQUANT",
            dataset=dataset,
            contract_version="xtquant-exchange-calendar-contract.v1",
            semantic_rule_version="semantic-contract-rules.v1",
            unit_rule_version="unit-contract-rules.v1",
            coverage_rule_version="coverage-rules.v1",
            expected_schema_version=descriptor_for(dataset.value).schema_version,
        ),
        # Two cases are deliberately required.  A selected approved-style
        # case may exercise an attempt but cannot claim dataset readiness.
        promotion_profile=PromotionProfileV3(
            source="XTQUANT",
            dataset=dataset,
            promotion_profile_version="xtquant-exchange-calendar-promotion.v1",
            promotion_config_version="real-validation-config.v2",
            required_case_ids=("calendar_bj", "calendar_sh"),
            reference_bundle_id="st-603377-authority-v3",
            reference_bundle_manifest_sha256="b" * 64,
        ),
    )


@dataclass(frozen=True, slots=True)
class BuiltV3Run:
    directory: Path
    context: V3EvidenceTrustContext
    verifier: ValidationEvidenceVerifierV3
    envelope: ValidationRunEnvelopeV3


def build_v3_run(
    *,
    root: Path,
    run_index: int,
    outcome: AttemptOutcomeV3 = AttemptOutcomeV3.SUCCEEDED,
    selected_case_ids: tuple[str, ...] = ("calendar_sh",),
    failure: ValidationFailure | None = None,
    context: V3EvidenceTrustContext | None = None,
) -> BuiltV3Run:
    """Build a complete synthetic v3 success or a credential-free failure."""
    trust_context = context or build_v3_context()
    signer = FakeV3RunnerTrustStore()
    sample = trust_context.sample
    start = UtcTimestamp(datetime(2026, 8, 6, 0, 0, run_index * 2, tzinfo=UTC))
    end = UtcTimestamp(start.value + timedelta(seconds=1))
    identity = ValidationAttemptIdentityV3(
        assessment_id=_id("data_assessment", 1, lane=1),
        authorization_id=_id("data_authorization", run_index, lane=2),
        invocation_id=_id("data_invocation", run_index, lane=3),
        session_id=_id("data_session", run_index, lane=4),
        run_id=_id("data_validation", run_index, lane=5),
    )
    versions = ValidationVersions(
        adapter_version="xtquant-readonly.s1-07c.v1",
        package_version="1.0.0",
        code_version="s1-07c0a.contract.v1",
        schema_version=trust_context.source_dataset_contract.expected_schema_version,
        rule_version="quality-rules.v1",
        threshold_version="comparison-thresholds.v1",
        sample_spec_version=sample.sample_spec_version,
        config_version=trust_context.promotion_profile.promotion_config_version,
    )
    request = ValidationRequestManifestV3.for_controlled_sample(
        identity=identity,
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        sample=sample,
        selected_case_ids=selected_case_ids,
        source_dataset_contract=trust_context.source_dataset_contract,
        promotion_profile=trust_context.promotion_profile,
    )
    fingerprint = compute_run_fingerprint_v3(request=request, versions=versions)
    envelope = ValidationRunEnvelopeV3(
        identity=identity,
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        versions=versions,
        request_sha256=request.request_sha256,
        run_fingerprint=fingerprint,
        selected_case_ids=selected_case_ids,
        sample_spec_version=sample.sample_spec_version,
        sample_spec_sha256=sample.sample_spec_sha256,
        source_dataset_contract_version=trust_context.source_dataset_contract.contract_version,
        source_dataset_contract_sha256=trust_context.source_dataset_contract.sha256,
        promotion_profile_version=trust_context.promotion_profile.promotion_profile_version,
        promotion_profile_sha256=trust_context.promotion_profile.sha256,
        reference_bundle_id=trust_context.promotion_profile.reference_bundle_id,
        reference_bundle_manifest_sha256=(
            trust_context.promotion_profile.reference_bundle_manifest_sha256
        ),
        outcome=outcome,
        failure=(
            failure or ValidationFailure("CAPABILITY_UNSUPPORTED", False)
            if outcome is AttemptOutcomeV3.FAILED
            else None
        ),
        started_at=start,
        ended_at=end,
    )
    if outcome is AttemptOutcomeV3.FAILED:
        return _write_failed_run(
            root=root, envelope=envelope, request=request, signer=signer, context=trust_context
        )
    return _write_successful_run(
        root=root,
        envelope=envelope,
        request=request,
        signer=signer,
        context=trust_context,
    )


def _write_failed_run(
    *,
    root: Path,
    envelope: ValidationRunEnvelopeV3,
    request: ValidationRequestManifestV3,
    signer: FakeV3RunnerTrustStore,
    context: V3EvidenceTrustContext,
) -> BuiltV3Run:
    failure = envelope.failure
    assert failure is not None
    attestation = signer.sign(
        ResultAttestationV3(
            signer_key_id=signer.key_id,
            identity=envelope.identity,
            source=envelope.source,
            dataset=envelope.dataset,
            request_sha256=envelope.request_sha256,
            run_fingerprint=envelope.run_fingerprint,
            selected_case_ids=envelope.selected_case_ids,
            sample_spec_version=envelope.sample_spec_version,
            sample_spec_sha256=envelope.sample_spec_sha256,
            source_dataset_contract_version=envelope.source_dataset_contract_version,
            source_dataset_contract_sha256=envelope.source_dataset_contract_sha256,
            promotion_profile_version=envelope.promotion_profile_version,
            promotion_profile_sha256=envelope.promotion_profile_sha256,
            reference_bundle_id=envelope.reference_bundle_id,
            reference_bundle_manifest_sha256=envelope.reference_bundle_manifest_sha256,
            versions=envelope.versions.to_dict(),
            outcome=AttemptOutcomeV3.FAILED,
            failure=failure,
            success_evidence=None,
            started_at=envelope.started_at,
            ended_at=envelope.ended_at,
            signature="0" * 64,
        )
    )
    files = {
        "request/manifest.json": (request.to_bytes(), len(request.selected_case_ids)),
        "runner/result-attestation.v2.json": (attestation.to_bytes(), 1),
        "failure/attempt.json": (
            canonical_json(
                {
                    "attempt_outcome": "FAILED",
                    "failure": {"category": failure.category, "retryable": failure.retryable},
                    "identity": envelope.identity.to_dict(),
                    "request_sha256": envelope.request_sha256,
                }
            ),
            0,
        ),
    }
    return _write(root=root, envelope=envelope, files=files, context=context, signer=signer)


def _write_successful_run(
    *,
    root: Path,
    envelope: ValidationRunEnvelopeV3,
    request: ValidationRequestManifestV3,
    signer: FakeV3RunnerTrustStore,
    context: V3EvidenceTrustContext,
) -> BuiltV3Run:
    cases = request.selected_cases
    rows = tuple(
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
    )
    parquet_bytes, content_sha, row_count = build_fake_exchange_calendar_parquet(rows)
    files: dict[str, tuple[bytes, int]] = {
        "request/manifest.json": (request.to_bytes(), len(cases)),
        "canonical/content.parquet": (parquet_bytes, row_count),
    }
    raw_root = root / f"_v3-raw-{envelope.identity.run_id.value.hex}"
    raw_dir = RawBatchPublisher(data_root=raw_root).publish(
        RawBatchPublishRequest(
            source=envelope.source,
            dataset=envelope.dataset.value,
            batch_id=f"v3-run-{envelope.identity.run_id.value.hex}",
            adapter_version=envelope.versions.adapter_version,
            code_version=envelope.versions.code_version,
            request_params={
                "request_sha256": envelope.request_sha256,
                "run_id": str(envelope.identity.run_id),
            },
            files=(RawManifestFile("payload.json", b'{"synthetic":true}', row_count),),
            created_at=envelope.started_at.to_iso(),
        )
    )
    files.update(_collect(raw_dir, prefix=f"raw-root/{raw_dir.relative_to(raw_root).as_posix()}"))
    quality_root = root / f"_v3-quality-{envelope.identity.run_id.value.hex}"
    quality = QualityRunArtifact(
        run_id=_id("data_quality", envelope.identity.run_id.value.int % 1_000_000, lane=6),
        version_id=_id("data_version", envelope.identity.run_id.value.int % 1_000_000, lane=7),
        rule_version=envelope.versions.rule_version,
        schema_version=envelope.versions.schema_version,
        content_sha256=content_sha,
        started_at=envelope.started_at,
        ended_at=envelope.ended_at,
        status=QualityRunStatus.PASSED,
        results=tuple(
            QualityOutcomeResult(
                rule_code=rule_code,
                status=RuleOutcomeStatus.PASSED,
                affected_count=0,
                summary="synthetic v3 contract fixture passed",
                row_evidence_relative_path=None,
            )
            for rule_code in expected_quality_rule_codes(envelope.dataset)
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
    table = build_fake_exchange_calendar_table(rows)
    difference_root = root / f"_v3-difference-{envelope.identity.run_id.value.hex}"
    outcome = CrossSourceComparator().compare(
        request=CrossSourceComparisonRequest(
            dataset=envelope.dataset,
            schema_version=envelope.versions.schema_version,
            primary_source=DataSource.XTQUANT,
            fallback_source=DataSource.BAOSTOCK,
            primary_version_id=_id("data_version", 10, lane=8),
            fallback_version_id=_id("data_version", 11, lane=8),
            primary_batch_id=_id("data_batch", 10, lane=9),
            fallback_batch_id=_id("data_batch", 11, lane=9),
            primary_content_sha256=content_sha,
            fallback_content_sha256=content_sha,
            primary_quality_run_id=quality.run_id,
            fallback_quality_run_id=_id("data_quality", 12, lane=6),
            primary_table=table,
            fallback_table=table,
            thresholds=default_thresholds(),
            sample_spec=envelope.selected_case_ids,
            created_at=envelope.ended_at,
        )
    )
    difference_dir = DifferenceReportArtifactWriter(artifacts_root=difference_root).write(
        outcome=outcome
    )
    files.update(
        _collect(
            difference_dir,
            prefix=f"difference-root/{difference_dir.relative_to(difference_root).as_posix()}",
        )
    )
    semantic = canonical_json(
        {
            "canonical_content_sha256": content_sha,
            "dataset": envelope.dataset.value,
            "semantic_evidence_schema_version": "source-validation-semantic-evidence.v1",
            "semantic_rule_version": context.source_dataset_contract.semantic_rule_version,
            "selected_case_ids": list(envelope.selected_case_ids),
            "source": envelope.source,
            "source_dataset_contract_sha256": context.source_dataset_contract.sha256,
            "violations": [],
        }
    )
    observed_units = {
        case.case_id: dict(case.expected_units)
        for case in context.sample.cases
        if case.case_id in envelope.selected_case_ids
    }
    schema_unit = canonical_json(
        {
            "canonical_content_sha256": content_sha,
            "dataset": envelope.dataset.value,
            "observed_units_by_case": observed_units,
            "schema_unit_evidence_schema_version": "source-validation-schema-unit-evidence.v1",
            "schema_version": envelope.versions.schema_version,
            "selected_case_ids": list(envelope.selected_case_ids),
            "source": envelope.source,
            "source_dataset_contract_sha256": context.source_dataset_contract.sha256,
            "unit_deviations": [],
            "unit_rule_version": context.source_dataset_contract.unit_rule_version,
        }
    )
    files["semantic/result.json"] = (semantic, 0)
    files["schema-unit/result.json"] = (schema_unit, 0)
    raw_manifest_sha = str(
        json.loads(
            next(
                payload
                for key, (payload, _count) in files.items()
                if key.startswith("raw-root/") and key.endswith("/manifest.json")
            )
        )["manifest_sha256"]
    )
    quality_manifest = json.loads(
        next(
            payload
            for key, (payload, _count) in files.items()
            if key.startswith("quality-root/") and key.endswith("/manifest.json")
        )
    )
    difference_manifest = json.loads(
        next(
            payload
            for key, (payload, _count) in files.items()
            if key.startswith("difference-root/") and key.endswith("/manifest.json")
        )
    )
    inventory = [
        {
            "relative_path": relative,
            "row_count": row_count,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        for relative, (payload, row_count) in sorted(files.items())
    ]
    success = SuccessEvidenceBindingV3(
        raw_manifest_sha256=raw_manifest_sha,
        canonical_content_sha256=content_sha,
        quality_manifest_sha256=str(quality_manifest["manifest_sha256"]),
        quality_run_id=parse_entity_id(str(quality_manifest["run_id"])),
        difference_manifest_sha256=str(difference_manifest["manifest_sha256"]),
        difference_report_id=outcome.report_id,
        semantic_evidence_sha256=hashlib.sha256(semantic).hexdigest(),
        schema_unit_evidence_sha256=hashlib.sha256(schema_unit).hexdigest(),
        evidence_inventory_sha256=hashlib.sha256(canonical_json(inventory)).hexdigest(),
    )
    attestation = signer.sign(
        ResultAttestationV3(
            signer_key_id=signer.key_id,
            identity=envelope.identity,
            source=envelope.source,
            dataset=envelope.dataset,
            request_sha256=envelope.request_sha256,
            run_fingerprint=envelope.run_fingerprint,
            selected_case_ids=envelope.selected_case_ids,
            sample_spec_version=envelope.sample_spec_version,
            sample_spec_sha256=envelope.sample_spec_sha256,
            source_dataset_contract_version=envelope.source_dataset_contract_version,
            source_dataset_contract_sha256=envelope.source_dataset_contract_sha256,
            promotion_profile_version=envelope.promotion_profile_version,
            promotion_profile_sha256=envelope.promotion_profile_sha256,
            reference_bundle_id=envelope.reference_bundle_id,
            reference_bundle_manifest_sha256=envelope.reference_bundle_manifest_sha256,
            versions=envelope.versions.to_dict(),
            outcome=AttemptOutcomeV3.SUCCEEDED,
            failure=None,
            success_evidence=success,
            started_at=envelope.started_at,
            ended_at=envelope.ended_at,
            signature="0" * 64,
        )
    )
    files["runner/result-attestation.v2.json"] = (attestation.to_bytes(), 1)
    return _write(root=root, envelope=envelope, files=files, context=context, signer=signer)


def _write(
    *,
    root: Path,
    envelope: ValidationRunEnvelopeV3,
    files: dict[str, tuple[bytes, int]],
    context: V3EvidenceTrustContext,
    signer: FakeV3RunnerTrustStore,
) -> BuiltV3Run:
    directory = ValidationEvidenceWriterV3(validation_root=root).write(
        envelope=envelope, files=files
    )
    verifier = ValidationEvidenceVerifierV3(
        validation_root=root,
        context=context,
        runner_attestation_verifier=signer,
    )
    return BuiltV3Run(
        directory=directory,
        context=context,
        verifier=verifier,
        envelope=envelope,
    )


__all__ = ["BuiltV3Run", "build_v3_context", "build_v3_run"]
