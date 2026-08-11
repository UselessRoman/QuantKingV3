"""K2 DifferenceReport artifacts bound to one shared final candidate version."""

from __future__ import annotations

from pathlib import Path

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonRequest,
)
from quantking.data.comparison.thresholds import default_thresholds
from quantking.data.comparison.verifier import (
    DifferenceReportVerifier,
    VerifiedDifferenceReport,
)
from quantking.data.models import DatasetKind
from quantking.data.resolution.service import ResolutionKey, SourceCandidate
from quantking.data.schemas import descriptor_for


class K2DifferenceArtifactRecorder:
    """Write immutable pair reports now and expose verified summaries after resolution."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._writer = DifferenceReportArtifactWriter(artifacts_root=artifacts_root)
        self._verifier = DifferenceReportVerifier(artifacts_root=artifacts_root)
        self._report_ids: set[EntityId] = set()

    def record(
        self,
        *,
        primary: SourceCandidate,
        fallback: SourceCandidate,
        keys: tuple[ResolutionKey, ...],
        created_at: UtcTimestamp,
    ) -> EntityId:
        if (
            primary.table is None
            or fallback.table is None
            or primary.version_id is None
            or fallback.version_id is None
            or primary.version_id != fallback.version_id
            or primary.quality_run_id is None
            or fallback.quality_run_id is None
            or primary.batch_id is None
            or fallback.batch_id is None
            or primary.content_sha256 is None
            or fallback.content_sha256 is None
        ):
            raise ValueError("K2 comparison requires two landed shared-candidate inputs")
        descriptor = descriptor_for(DatasetKind.DAILY_BAR_UNADJUSTED.value)
        outcome = CrossSourceComparator().compare(
            request=CrossSourceComparisonRequest(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                schema_version=descriptor.schema_version,
                primary_source=primary.upstream,
                fallback_source=fallback.upstream,
                primary_version_id=primary.version_id,
                fallback_version_id=fallback.version_id,
                primary_batch_id=primary.batch_id,
                fallback_batch_id=fallback.batch_id,
                primary_content_sha256=primary.content_sha256,
                fallback_content_sha256=fallback.content_sha256,
                primary_quality_run_id=primary.quality_run_id,
                fallback_quality_run_id=fallback.quality_run_id,
                primary_table=primary.table,
                fallback_table=fallback.table,
                thresholds=default_thresholds(),
                sample_spec=("K2_AFFECTED_SCOPE",),
                created_at=created_at,
                comparison_primary_keys=tuple((item.symbol, item.trade_date) for item in keys),
                primary_empty_result=primary.empty_result,
                fallback_empty_result=fallback.empty_result,
            )
        )
        self._writer.write(outcome=outcome)
        self._report_ids.add(outcome.report_id)
        return outcome.report_id

    def verified_reports(self) -> tuple[VerifiedDifferenceReport, ...]:
        return tuple(
            self._verifier.verify(report_id=report_id)
            for report_id in sorted(self._report_ids, key=str)
        )


__all__ = ["K2DifferenceArtifactRecorder"]
