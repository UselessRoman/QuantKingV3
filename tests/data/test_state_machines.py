"""State machine and domain contract tests for Stage 1 data objects."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from quantking.common.errors import (
    DatasetVersionImmutableError,
    DatasetVersionNotFoundError,
    InvalidDataTransitionError,
    OverrideNotApplicableError,
)
from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.models import (
    BatchFailureInfo,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    SourceOverride,
    SourceOverrideStatus,
    transition_batch,
    transition_dataset_version,
    transition_override,
)

_NOW = UtcTimestamp(datetime(2026, 7, 16, 1, 0, tzinfo=UTC))
_LATER = UtcTimestamp(datetime(2026, 7, 16, 2, 0, tzinfo=UTC))
_TRADE_DATE = TradingDate(date(2026, 7, 16))


def _running_batch() -> IngestionBatch:
    return IngestionBatch.running(
        batch_id=new_entity_id("data_batch"),
        source=DataSource.XTQUANT,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        adapter_version="xtquant-probe-1",
        request_params={"code": "000001.SZ", "start": "2026-01-01"},
        started_at=_NOW,
    )


def _building_version() -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="abc123",
        parent_version_id=None,
        input_batch_ids=(new_entity_id("data_batch"),),
        created_at=_NOW,
    )


def _pending_override() -> SourceOverride:
    return SourceOverride.pending(
        override_id=new_entity_id("data_override"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        primary_source=DataSource.XTQUANT,
        fallback_source=DataSource.BAOSTOCK,
        scope_codes=("000001.SZ",),
        scope_dates=(_TRADE_DATE,),
        fields=("close",),
        reason="missing bar on primary",
        created_at=_NOW,
    )


class TestIngestionBatchStateMachine:
    def test_running_to_succeeded(self) -> None:
        batch = transition_batch(
            _running_batch(),
            IngestionBatchStatus.SUCCEEDED,
            at=_LATER,
            row_count=100,
            security_count=1,
            manifest_files=(
                ManifestFileEntry(
                    relative_path="raw/xtquant/daily/000001.SZ.parquet", size=1024, sha256="a" * 64
                ),
            ),
        )
        assert batch.status is IngestionBatchStatus.SUCCEEDED
        assert batch.row_count == 100
        assert batch.security_count == 1
        assert batch.ended_at == _LATER

    def test_running_to_partial(self) -> None:
        batch = transition_batch(
            _running_batch(),
            IngestionBatchStatus.PARTIAL,
            at=_LATER,
            row_count=50,
            security_count=1,
            failure=BatchFailureInfo(error_type="PartialError", message="partial", retryable=True),
        )
        assert batch.status is IngestionBatchStatus.PARTIAL
        assert batch.failure is not None

    def test_running_to_failed_requires_failure(self) -> None:
        with pytest.raises(InvalidDataTransitionError):
            transition_batch(_running_batch(), IngestionBatchStatus.FAILED, at=_LATER)

    def test_failed_cannot_become_succeeded(self) -> None:
        failed = transition_batch(
            _running_batch(),
            IngestionBatchStatus.FAILED,
            at=_LATER,
            failure=BatchFailureInfo(error_type="Err", message="boom", retryable=False),
        )
        with pytest.raises(InvalidDataTransitionError):
            transition_batch(failed, IngestionBatchStatus.SUCCEEDED, at=_LATER)

    def test_partial_cannot_become_succeeded(self) -> None:
        partial = transition_batch(
            _running_batch(),
            IngestionBatchStatus.PARTIAL,
            at=_LATER,
            row_count=50,
            security_count=1,
            failure=BatchFailureInfo(error_type="P", message="p", retryable=True),
        )
        with pytest.raises(InvalidDataTransitionError):
            transition_batch(partial, IngestionBatchStatus.SUCCEEDED, at=_LATER)

    @pytest.mark.parametrize("terminal", list(IngestionBatchStatus.__members__.values()))
    def test_terminal_states_are_immutable(self, terminal: IngestionBatchStatus) -> None:
        if terminal is IngestionBatchStatus.RUNNING:
            return
        failure = BatchFailureInfo(error_type="E", message="e", retryable=False)
        if terminal is IngestionBatchStatus.SUCCEEDED:
            batch = transition_batch(
                _running_batch(), terminal, at=_LATER, row_count=1, security_count=1
            )
        elif terminal is IngestionBatchStatus.PARTIAL:
            batch = transition_batch(
                _running_batch(),
                terminal,
                at=_LATER,
                row_count=1,
                security_count=1,
                failure=failure,
            )
        else:
            batch = transition_batch(_running_batch(), terminal, at=_LATER, failure=failure)
        with pytest.raises(InvalidDataTransitionError):
            transition_batch(batch, IngestionBatchStatus.RUNNING, at=_LATER)


class TestDatasetVersionStateMachine:
    def test_building_to_validated(self) -> None:
        version = transition_dataset_version(
            _building_version(),
            DatasetVersionStatus.VALIDATED,
            at=_LATER,
            manifest_sha256="b" * 64,
            security_count=10,
            row_count=1000,
        )
        assert version.status is DatasetVersionStatus.VALIDATED
        assert version.manifest_sha256 == "b" * 64

    def test_building_to_failed(self) -> None:
        version = transition_dataset_version(
            _building_version(),
            DatasetVersionStatus.FAILED,
            at=_LATER,
            failure=BatchFailureInfo(error_type="Q", message="bad", retryable=False),
        )
        assert version.status is DatasetVersionStatus.FAILED

    def test_validated_to_published(self) -> None:
        validated = transition_dataset_version(
            _building_version(),
            DatasetVersionStatus.VALIDATED,
            at=_NOW,
            manifest_sha256="b" * 64,
            security_count=10,
            row_count=1000,
        )
        published = transition_dataset_version(
            validated,
            DatasetVersionStatus.PUBLISHED,
            at=_LATER,
        )
        assert published.status is DatasetVersionStatus.PUBLISHED
        assert published.published_at == _LATER

    def test_published_is_immutable(self) -> None:
        validated = transition_dataset_version(
            _building_version(),
            DatasetVersionStatus.VALIDATED,
            at=_NOW,
            manifest_sha256="b" * 64,
            security_count=10,
            row_count=1000,
        )
        published = transition_dataset_version(
            validated,
            DatasetVersionStatus.PUBLISHED,
            at=_LATER,
        )
        with pytest.raises(DatasetVersionImmutableError):
            transition_dataset_version(published, DatasetVersionStatus.BUILDING, at=_LATER)
        with pytest.raises(DatasetVersionImmutableError):
            transition_dataset_version(published, DatasetVersionStatus.FAILED, at=_LATER)

    def test_failed_is_immutable(self) -> None:
        failed = transition_dataset_version(
            _building_version(),
            DatasetVersionStatus.FAILED,
            at=_LATER,
            failure=BatchFailureInfo(error_type="Q", message="bad", retryable=False),
        )
        with pytest.raises(InvalidDataTransitionError):
            transition_dataset_version(failed, DatasetVersionStatus.VALIDATED, at=_LATER)

    def test_building_cannot_skip_to_published(self) -> None:
        with pytest.raises(InvalidDataTransitionError):
            transition_dataset_version(
                _building_version(),
                DatasetVersionStatus.PUBLISHED,
                at=_LATER,
            )


class TestSourceOverrideStateMachine:
    def test_pending_to_approved(self) -> None:
        override = transition_override(
            _pending_override(),
            SourceOverrideStatus.APPROVED,
            at=_LATER,
            actor="analyst",
        )
        assert override.status is SourceOverrideStatus.APPROVED
        assert override.approved_at == _LATER

    def test_pending_to_rejected(self) -> None:
        override = transition_override(
            _pending_override(),
            SourceOverrideStatus.REJECTED,
            at=_LATER,
            actor="analyst",
        )
        assert override.status is SourceOverrideStatus.REJECTED

    def test_only_approved_can_be_applied(self) -> None:
        with pytest.raises(OverrideNotApplicableError):
            transition_override(
                _pending_override(),
                SourceOverrideStatus.APPLIED,
                at=_LATER,
                actor="a",
                applied_version_id=new_entity_id("data_version"),
            )

    def test_approved_to_applied_requires_new_version_id(self) -> None:
        approved = transition_override(
            _pending_override(),
            SourceOverrideStatus.APPROVED,
            at=_NOW,
            actor="analyst",
        )
        with pytest.raises(OverrideNotApplicableError):
            transition_override(approved, SourceOverrideStatus.APPLIED, at=_LATER, actor="a")

    def test_applied_is_immutable(self) -> None:
        approved = transition_override(
            _pending_override(),
            SourceOverrideStatus.APPROVED,
            at=_NOW,
            actor="analyst",
        )
        applied = transition_override(
            approved,
            SourceOverrideStatus.APPLIED,
            at=_LATER,
            actor="a",
            applied_version_id=new_entity_id("data_version"),
        )
        with pytest.raises(OverrideNotApplicableError):
            transition_override(applied, SourceOverrideStatus.PENDING, at=_LATER, actor="a")

    def test_rejected_is_immutable(self) -> None:
        rejected = transition_override(
            _pending_override(),
            SourceOverrideStatus.REJECTED,
            at=_NOW,
            actor="analyst",
        )
        with pytest.raises(InvalidDataTransitionError):
            transition_override(rejected, SourceOverrideStatus.APPROVED, at=_LATER, actor="a")


class TestManagedRelativePathsOnly:
    def test_manifest_entry_rejects_absolute_path(self) -> None:
        with pytest.raises(ValueError, match="relative"):
            ManifestFileEntry(
                relative_path="E:/data/raw/file.parquet",
                size=10,
                sha256="a" * 64,
            )

    def test_manifest_entry_rejects_parent_traversal(self) -> None:
        with pytest.raises(ValueError, match="relative"):
            ManifestFileEntry(relative_path="../escape.parquet", size=10, sha256="a" * 64)

    def test_request_params_canonical_json_is_deterministic(self) -> None:
        batch_a = IngestionBatch.running(
            batch_id=new_entity_id("data_batch"),
            source=DataSource.XTQUANT,
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            adapter_version="v1",
            request_params={"b": "2", "a": "1"},
            started_at=_NOW,
        )
        batch_b = IngestionBatch.running(
            batch_id=new_entity_id("data_batch"),
            source=DataSource.XTQUANT,
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            adapter_version="v1",
            request_params={"a": "1", "b": "2"},
            started_at=_NOW,
        )
        assert batch_a.request_params_json == batch_b.request_params_json


class TestErrorCodes:
    def test_data_transition_error_code_is_stable(self) -> None:
        error = InvalidDataTransitionError("nope")
        assert error.code.value == "invalid_data_transition"

    def test_immutable_error_code_is_stable(self) -> None:
        error = DatasetVersionImmutableError("nope")
        assert error.code.value == "dataset_version_immutable"

    def test_override_not_applicable_code_is_stable(self) -> None:
        error = OverrideNotApplicableError("nope")
        assert error.code.value == "override_not_applicable"

    def test_not_found_code_is_stable(self) -> None:
        error = DatasetVersionNotFoundError("nope")
        assert error.code.value == "dataset_version_not_found"
