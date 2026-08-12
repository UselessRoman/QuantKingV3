"""Narrow Web-facing port for the framework-neutral data-center service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from quantking.data.center import DataCenterPage


class DataCenterReadPort(Protocol):
    def summary(self) -> Mapping[str, object]: ...
    def list_batches(
        self,
        *,
        dataset: str | None,
        upstream: str | None,
        status: str | None,
        started_from: str | None,
        started_to: str | None,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage: ...
    def list_versions(
        self,
        *,
        dataset: str | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage: ...
    def get_version(self, version_id: str) -> Mapping[str, object]: ...
    def get_version_lineage(self, version_id: str) -> Mapping[str, object]: ...
    def preview(
        self,
        *,
        version_id: str,
        fields: Sequence[str] | None,
        codes: Sequence[str] | None,
        start_date: str | None,
        end_date: str | None,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage: ...
    def list_quality_runs(
        self,
        *,
        version_id: str | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage: ...
    def get_quality_run(self, run_id: str) -> Mapping[str, object]: ...
    def list_difference_reports(
        self,
        *,
        version_id: str | None,
        primary_source: str | None,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage: ...
    def get_difference_report(self, report_id: str) -> Mapping[str, object]: ...
    def difference_rows(
        self, *, report_id: str, cursor: str | None, limit: int
    ) -> DataCenterPage: ...
    def list_resolution_runs(
        self,
        *,
        conflicts_only: bool,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage: ...
    def get_resolution_run(self, run_id: str) -> Mapping[str, object]: ...
    def resolution_decisions(
        self,
        *,
        run_id: str,
        conflicts: bool,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage: ...
    def list_failures(self, *, cursor: str | None, limit: int) -> DataCenterPage: ...
    def list_eod_runs(
        self, *, status: str | None, cursor: str | None, limit: int
    ) -> DataCenterPage: ...


__all__ = ["DataCenterPage", "DataCenterReadPort"]
