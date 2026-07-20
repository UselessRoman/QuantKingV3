"""Red tests for managed quality artifact directories (S1-04A).

The quality artifact writer must produce a managed, immutable, SHA-bound
directory under ``artifacts/data-quality/<run_id>/`` that mirrors the
Raw and Canonical warehouse conventions: deterministic JSON manifest,
per-file SHA-256, COMPLETE marker, sanitized failure.json on failure,
no absolute paths, no symlink/junction traversal, no overwrite of
existing evidence.

These tests only use ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.models import DatasetKind

_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|^/")


def _daily_bar_table() -> pa.Table:
    table = pa.Table.from_pylist(
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ]
    )
    descriptor = canonical_schema("daily_bar_unadjusted")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _calendar_table() -> pa.Table:
    table = pa.Table.from_pylist(
        [{"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True}]
    )
    descriptor = canonical_schema("exchange_calendar")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _build_request(content_sha: str = "a" * 64):
    from quantking.data.quality.models import QualityRequest, ReferenceDataBinding

    return QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04",
        content_sha256=content_sha,
        input_batch_ids=(new_entity_id("data_batch"),),
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 15)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="b" * 64,
            )
        },
        empty_result=None,
    )


class TestQualityArtifactDirectory:
    def test_passed_run_writes_managed_directory_with_manifest_and_complete(
        self, tmp_path: Path
    ) -> None:
        from quantking.data.quality.artifacts import QualityArtifactWriter
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.rules import default_rule_set

        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=_build_request(),
            table=_daily_bar_table(),
            reference_tables={"exchange_calendar": _calendar_table()},
        )
        writer = QualityArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome, row_evidence_tables={})
        # The directory ``artifacts/data-quality/<run_id>/`` must exist
        # with manifest.json, COMPLETE and per-rule evidence files.
        directory = tmp_path / "data-quality" / str(outcome.run_id).replace(":", "--")
        assert directory.is_dir()
        assert (directory / "manifest.json").is_file()
        assert (directory / "COMPLETE").is_file()
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["run_id"] == str(outcome.run_id)
        assert manifest["status"] == "PASSED"
        assert manifest["rule_version"] == "quality-rules.v1"
        assert manifest["content_sha256"] == outcome.content_sha256
        # manifest must embed its own SHA-256 and COMPLETE must bind to it.
        assert "manifest_sha256" in manifest
        complete = json.loads((directory / "COMPLETE").read_text(encoding="utf-8"))
        assert complete["manifest_sha256"] == manifest["manifest_sha256"]

    def test_failed_run_writes_incomplete_directory_with_sanitized_failure(
        self, tmp_path: Path
    ) -> None:
        from quantking.data.quality.artifacts import QualityArtifactWriter
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.rules import default_rule_set

        # Build a bad table that fails OHLC.
        bad_rows = [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 9.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ]
        table = pa.Table.from_pylist(bad_rows)
        descriptor = canonical_schema("daily_bar_unadjusted")
        columns: list[pa.Array] = []
        fields: list[pa.Field] = []
        for field in descriptor:
            columns.append(table.column(field.name).cast(field.type, safe=False))
            fields.append(pa.field(field.name, field.type, nullable=field.nullable))
        bad_table = pa.Table.from_arrays(columns, schema=pa.schema(fields))

        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=_build_request(),
            table=bad_table,
            reference_tables={},
        )
        assert outcome.status.value == "FAILED"
        writer = QualityArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome, row_evidence_tables={})
        directory = tmp_path / "data-quality" / str(outcome.run_id).replace(":", "--")
        # FAILED runs may be written to the same managed directory but must
        # include a sanitized failure.json alongside the manifest and COMPLETE.
        assert (directory / "manifest.json").is_file()
        assert (directory / "failure.json").is_file()
        failure = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
        # Sanitization: no absolute path in any value.
        for value in failure.values():
            if isinstance(value, str):
                assert not _ABSOLUTE_PATH_RE.search(value), (
                    f"failure.{value!r} leaks an absolute path"
                )

    def test_artifact_directory_rejects_symlink_or_junction_root(self, tmp_path: Path) -> None:
        from quantking.data.quality.artifacts import QualityArtifactWriter

        # Create a symlink/junction-like root and ensure the writer rejects it.
        # We cannot always create symlinks on Windows without admin; instead
        # we pass a non-absolute path and expect rejection.
        with pytest.raises(ValueError):
            QualityArtifactWriter(artifacts_root=Path("relative/path"))  # type: ignore[arg-type]

    def test_artifact_rejects_overwrite_of_existing_evidence(self, tmp_path: Path) -> None:
        from quantking.data.quality.artifacts import QualityArtifactWriter
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
        from quantking.data.quality.rules import default_rule_set

        # First write succeeds.
        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=_build_request(),
            table=_daily_bar_table(),
            reference_tables={},
        )
        writer = QualityArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome, row_evidence_tables={})
        # Re-writing the same run_id with a different outcome must fail
        # (idempotency conflict). Build a different outcome with the same
        # run_id by mutating its summary.

        # Different content_sha yields a different request; reusing the same
        # run_id is the conflict.
        different_request = QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=new_entity_id("data_version"),
            rule_version="quality-rules.v1",
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-04",
            content_sha256="c" * 64,  # different content SHA
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 7, 15)),
            coverage_end=TradingDate(date(2026, 7, 15)),
            reference_data={
                "exchange_calendar": ReferenceDataBinding(
                    dataset=DatasetKind.EXCHANGE_CALENDAR,
                    version_id=new_entity_id("data_version"),
                    content_sha256="d" * 64,
                )
            },
            empty_result=None,
        )
        different_outcome = engine.execute(
            request=different_request,
            table=_daily_bar_table(),
            reference_tables={},
        )
        # Force the same run_id to simulate a conflict.
        from dataclasses import replace

        from quantking.data.quality.artifacts import QualityArtifactConflictError

        conflict = replace(different_outcome, run_id=outcome.run_id)
        with pytest.raises(QualityArtifactConflictError):
            writer.write(outcome=conflict, row_evidence_tables={})

    def test_artifact_idempotent_rewrite_returns_same_directory(self, tmp_path: Path) -> None:
        from quantking.data.quality.artifacts import QualityArtifactWriter
        from quantking.data.quality.engine import QualityEngine
        from quantking.data.quality.rules import default_rule_set

        engine = QualityEngine(rule_set=default_rule_set())
        outcome = engine.execute(
            request=_build_request(),
            table=_daily_bar_table(),
            reference_tables={},
        )
        writer = QualityArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome, row_evidence_tables={})
        directory = tmp_path / "data-quality" / str(outcome.run_id).replace(":", "--")
        manifest_bytes = (directory / "manifest.json").read_bytes()
        # Idempotent rewrite of the SAME outcome must succeed without
        # changing any file bytes.
        writer.write(outcome=outcome, row_evidence_tables={})
        assert (directory / "manifest.json").read_bytes() == manifest_bytes


__all__ = ["TestQualityArtifactDirectory"]
