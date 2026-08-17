"""Strictly read-only verifier for the fixed S1-08 first release.

This module deliberately uses SQLite's ``mode=ro&immutable=1`` connection
instead of either production runtime builder.  Verification must never enable
WAL, create a sidecar, run a migration, or repair evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.common.config import S108_FIRST_RELEASE_SYMBOLS
from quantking.common.errors import InvalidIdError
from quantking.common.ids import parse_entity_id
from quantking.common.paths import resolve_under
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.readonly import readonly_data_uow_factory
from quantking.data.adapters.sources.baostock import BAOSTOCK_UNIT_POLICY_VERSION
from quantking.data.adapters.sources.baostock_worker import BAOSTOCK_DISTRIBUTION_VERSION
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    VolumeUnit,
)
from quantking.data.adapters.sources.registry import PRODUCTION_UPSTREAMS
from quantking.data.adapters.sources.units import (
    AKSHARE_UNIT_POLICY_VERSION,
    resolve_returned_units,
)
from quantking.data.adapters.warehouse.errors import ManifestIntegrityError
from quantking.data.adapters.warehouse.manifest import path_safe_id
from quantking.data.adapters.warehouse.publish_coordinator import compute_content_sha256
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.adapters.warehouse.verifier import (
    verify_canonical_directory,
    verify_raw_directory,
)
from quantking.data.center import (
    CanonicalPreviewPort,
    DataCenterQueryService,
    DifferenceVerifierPort,
)
from quantking.data.comparison.verifier import (
    DifferenceReportVerificationError,
    DifferenceReportVerifier,
)
from quantking.data.eod.fingerprint import eod_input_fingerprint
from quantking.data.eod.frozen import FrozenEodCommandError, parse_frozen_eod_command
from quantking.data.eod.release import (
    S107K4_CODE_VERSION,
    S107K4_IDEMPOTENCY_KEY,
    S108_TARGET_END,
    S108_TARGET_START,
)
from quantking.data.incremental import IncrementalPlanRequest, build_incremental_plan
from quantking.data.models import DatasetKind, DataUpstream
from quantking.data.quality.models import K4_DAILY_RULE_VERSION
from quantking.data.resolution.artifacts import (
    ResolutionArtifactVerificationError,
    ResolutionArtifactVerifier,
    verify_resolution_artifact,
)
from quantking.data.resolution.models import K4_RESOLUTION_RULE_VERSION

S108_RELEASE_REVISION = "20260810_0014"
S107K4_ADAPTER_VERSION = "baostock-akshare-daily-bar.k4.v1"
_DATASET = DatasetKind.DAILY_BAR_UNADJUSTED.value
_ENDPOINTS = {
    DataUpstream.BAOSTOCK.value: "query_history_k_data_plus",
    DataUpstream.TENCENT.value: "stock_zh_a_hist_tx",
    DataUpstream.SINA.value: "stock_zh_a_daily",
}
_PROVIDERS = {
    DataUpstream.BAOSTOCK.value: "BAOSTOCK",
    DataUpstream.TENCENT.value: "AKSHARE",
    DataUpstream.SINA.value: "AKSHARE",
}
_HTTP_PER_ENDPOINT = {
    DataUpstream.TENCENT.value: 3,
    DataUpstream.SINA.value: 2,
}


class S108ReleaseVerificationError(ValueError):
    """The explicit release is missing, ambiguous, or not integrity-safe."""


def _release_version_id(value: str) -> str:
    if not isinstance(value, str) or value.casefold() == "latest":
        raise S108ReleaseVerificationError("version_id must be an explicit data_version id")
    if "/" in value or "\\" in value:
        raise S108ReleaseVerificationError("version_id must not be a path")
    try:
        parsed = parse_entity_id(value)
    except (InvalidIdError, TypeError, ValueError) as error:
        raise S108ReleaseVerificationError(
            "version_id must be an explicit data_version id"
        ) from error
    if parsed.namespace != "data_version" or str(parsed) != value:
        raise S108ReleaseVerificationError("version_id must be a canonical data_version id")
    return value


@contextmanager
def _readonly_database(database: Path) -> Iterator[sqlite3.Connection]:
    if not database.is_file():
        raise S108ReleaseVerificationError("metadata database is missing")
    if any(database.with_name(f"{database.name}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise S108ReleaseVerificationError("metadata database has an active SQLite sidecar")
    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0)
    except sqlite3.Error as error:
        raise S108ReleaseVerificationError(
            "metadata database cannot be opened read-only"
        ) from error
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        if len(quick_check) != 1 or quick_check[0][0] != "ok":
            raise S108ReleaseVerificationError("metadata database quick_check failed")
        yield connection
    except sqlite3.Error as error:
        raise S108ReleaseVerificationError("metadata database verification failed") from error
    finally:
        connection.close()


def _rows(
    connection: sqlite3.Connection,
    statement: str,
    parameters: Sequence[object] = (),
) -> list[sqlite3.Row]:
    return list(connection.execute(statement, tuple(parameters)).fetchall())


def _one(
    connection: sqlite3.Connection,
    statement: str,
    parameters: Sequence[object] = (),
    *,
    label: str,
) -> sqlite3.Row:
    values = _rows(connection, statement, parameters)
    if len(values) != 1:
        raise S108ReleaseVerificationError(f"{label} must exist exactly once")
    return values[0]


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise S108ReleaseVerificationError(f"{label} is not JSON text")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as error:
        raise S108ReleaseVerificationError(f"{label} is invalid JSON") from error
    if not isinstance(result, dict):
        raise S108ReleaseVerificationError(f"{label} must be a JSON object")
    return result


def _json_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, str):
        raise S108ReleaseVerificationError(f"{label} is not JSON text")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as error:
        raise S108ReleaseVerificationError(f"{label} is invalid JSON") from error
    if not isinstance(result, list):
        raise S108ReleaseVerificationError(f"{label} must be a JSON array")
    return result


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _verify_quality_artifact(
    connection: sqlite3.Connection,
    data_root: Path,
    quality: sqlite3.Row,
) -> None:
    """Bind one terminal QualityRun to its immutable managed artifact."""
    run_id = str(quality["run_id"])
    directory = data_root / "artifacts" / "quality" / "data-quality" / path_safe_id(run_id)
    if not directory.is_dir() or directory.is_symlink():
        raise S108ReleaseVerificationError("QualityRun artifact directory is invalid")
    entries = {item.name for item in directory.iterdir()}
    expected_entries = {"manifest.json", "COMPLETE"}
    if quality["status"] == "FAILED":
        expected_entries.add("failure.json")
    if entries != expected_entries or any(item.is_symlink() for item in directory.iterdir()):
        raise S108ReleaseVerificationError("QualityRun artifact file set is invalid")

    manifest_path = directory / "manifest.json"
    complete_path = directory / "COMPLETE"
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest_value = json.loads(manifest_bytes)
        complete_value = json.loads(complete_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise S108ReleaseVerificationError("QualityRun artifact JSON is invalid") from error
    if not isinstance(manifest_value, dict) or not isinstance(complete_value, dict):
        raise S108ReleaseVerificationError("QualityRun artifact JSON shape is invalid")
    manifest = cast(dict[str, object], manifest_value)
    complete = cast(dict[str, object], complete_value)
    if manifest_bytes != _canonical_json_bytes(manifest):
        raise S108ReleaseVerificationError("QualityRun manifest is not canonical JSON")
    manifest_sha = manifest.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or len(manifest_sha) != 64:
        raise S108ReleaseVerificationError("QualityRun manifest SHA is invalid")
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    if (
        hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest() != manifest_sha
        or complete != {"manifest_sha256": manifest_sha}
        or complete_path.read_bytes() != _canonical_json_bytes(complete)
    ):
        raise S108ReleaseVerificationError("QualityRun artifact SHA binding is invalid")

    results = _rows(
        connection,
        "SELECT * FROM quality_results WHERE run_id = ? ORDER BY sequence_number",
        (run_id,),
    )
    database_results = [
        {
            "rule_code": str(result["rule_code"]),
            "status": "PASSED" if result["passed"] == 1 else "FAILED",
            "affected_count": result["affected_count"],
            "summary": result["summary"],
            "row_evidence_relative_path": result["report_relative_path"],
        }
        for result in results
    ]
    persisted_results = [
        {
            "rule_code": str(result["rule_code"]),
            "passed": bool(result["passed"]),
            "affected_count": result["affected_count"],
            "summary": result["summary"],
            "report_relative_path": result["report_relative_path"],
        }
        for result in results
    ]
    if (
        set(manifest)
        != {
            "run_id",
            "version_id",
            "rule_version",
            "schema_version",
            "content_sha256",
            "started_at",
            "ended_at",
            "status",
            "results",
            "manifest_sha256",
        }
        or manifest.get("run_id") != run_id
        or manifest.get("version_id") != quality["version_id"]
        or manifest.get("rule_version") != quality["rule_version"]
        or manifest.get("schema_version") != quality["schema_version"]
        or manifest.get("content_sha256") != quality["content_sha256"]
        or manifest.get("started_at") != quality["started_at"]
        or manifest.get("ended_at") != quality["ended_at"]
        or manifest.get("status") != quality["status"]
        or manifest.get("results") != database_results
        or _json_list(quality["results_json"], label="QualityRun results") != persisted_results
        or quality["summary_report_relative_path"] is not None
        or any(result["report_relative_path"] is not None for result in results)
    ):
        raise S108ReleaseVerificationError("QualityRun artifact does not bind metadata")
    if not results:
        raise S108ReleaseVerificationError("QualityRun artifact has no rule results")

    if quality["status"] == "FAILED":
        try:
            failure_value = json.loads((directory / "failure.json").read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise S108ReleaseVerificationError("QualityRun failure artifact is invalid") from error
        if not isinstance(failure_value, dict):
            raise S108ReleaseVerificationError("QualityRun failure artifact is invalid")
        database_failure = (
            {}
            if quality["failure_json"] is None
            else _json_object(quality["failure_json"], label="QualityRun database failure")
        )
        expected_failure = (
            {}
            if not database_failure
            else {
                "error_type": database_failure.get("error_type"),
                "message": database_failure.get("message"),
                "retryable": str(database_failure.get("retryable", False)).lower(),
            }
        )
        if failure_value != expected_failure or (
            directory / "failure.json"
        ).read_bytes() != _canonical_json_bytes(cast(dict[str, object], failure_value)):
            raise S108ReleaseVerificationError("QualityRun failure artifact does not bind metadata")
    elif quality["failure_json"] is not None:
        raise S108ReleaseVerificationError("passed QualityRun contains failure metadata")


def _expected_chunks() -> tuple[tuple[str, str, str, str, str], ...]:
    values: list[tuple[str, str, str, str, str]] = []
    for symbol in S108_FIRST_RELEASE_SYMBOLS:
        start = S108_TARGET_START
        while start <= S108_TARGET_END:
            end = min(start + timedelta(days=30), S108_TARGET_END)
            values.append(
                (
                    symbol,
                    start.isoformat(),
                    end.isoformat(),
                    (start - timedelta(days=15)).isoformat(),
                    end.isoformat(),
                )
            )
            start = end + timedelta(days=1)
    return tuple(values)


def _verify_table(directory: Path, manifest: Mapping[str, object]) -> dict[str, object]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise S108ReleaseVerificationError("warehouse manifest files are invalid")
    tables: list[pa.Table] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
            raise S108ReleaseVerificationError("warehouse manifest file entry is invalid")
        relative_path = str(item["relative_path"])
        if relative_path.endswith(".parquet"):
            # Read the file itself; ``pq.read_table`` would treat Hive-style
            # parent directories as a dataset and synthesize partition columns.
            tables.append(
                pq.ParquetFile(directory / Path(relative_path)).read()  # type: ignore[no-untyped-call]
            )
    if not tables:
        raise S108ReleaseVerificationError("published release contains no Parquet rows")
    table = pa.concat_tables(tables)
    if not table.schema.equals(canonical_schema(_DATASET), check_metadata=False):
        raise S108ReleaseVerificationError("published Parquet schema is invalid")
    for partition in tables:
        partition_keys = [(str(item["code"]), item["trade_date"]) for item in partition.to_pylist()]
        if partition_keys != sorted(partition_keys):
            raise S108ReleaseVerificationError("published partition keys are not sorted")
    values = table.to_pylist()
    keys = [(str(item["code"]), item["trade_date"]) for item in values]
    if len(keys) != len(set(keys)):
        raise S108ReleaseVerificationError("published primary keys are not unique")
    if {key[0] for key in keys} != set(S108_FIRST_RELEASE_SYMBOLS):
        raise S108ReleaseVerificationError("published security scope is not exact")
    for item in values:
        trade_date = item["trade_date"]
        if (
            not isinstance(trade_date, date)
            or not S108_TARGET_START <= trade_date <= S108_TARGET_END
        ):
            raise S108ReleaseVerificationError("published row is outside the target window")
        prices = [item[name] for name in ("open", "high", "low", "close", "pre_close")]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in prices
        ):
            raise S108ReleaseVerificationError("published price or pre_close is invalid")
        open_value, high, low, close, _pre_close = (float(value) for value in prices)
        if high < max(open_value, close) or low > min(open_value, close) or low > high:
            raise S108ReleaseVerificationError("published OHLC relationship is invalid")
        volume = item["volume"]
        amount = item["amount"]
        if (
            isinstance(volume, bool)
            or not isinstance(volume, int)
            or volume < 0
            or isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or float(amount) < 0
        ):
            raise S108ReleaseVerificationError("published volume or amount is invalid")
    counts = Counter(key[0] for key in keys)
    logical_table = pa.Table.from_pylist(
        sorted(values, key=lambda item: (str(item["code"]), item["trade_date"])),
        schema=canonical_schema(_DATASET),
    )
    return {
        "row_count": len(values),
        "security_count": len(counts),
        "coverage_start": min(key[1] for key in keys).isoformat(),
        "coverage_end": max(key[1] for key in keys).isoformat(),
        "rows_by_symbol": dict(sorted(counts.items())),
        "content_sha256": compute_content_sha256(
            (logical_table,),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        ),
    }


def _verify_log(
    data_root: Path,
    row: sqlite3.Row,
    *,
    planned_chunks: Mapping[str, int],
    raw_responses: Mapping[str, int],
    failed_batches: Mapping[str, int],
) -> dict[str, object]:
    log_id = row["log_id"]
    if not isinstance(log_id, str):
        raise S108ReleaseVerificationError("Job log id is invalid")
    try:
        path = resolve_under(data_root, log_id)
    except Exception as error:
        raise S108ReleaseVerificationError("Job log id is not managed") from error
    if not path.is_file():
        raise S108ReleaseVerificationError("Job structured log is missing")
    events: list[dict[str, object]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise S108ReleaseVerificationError("Job structured log is invalid") from error
    planned = [item for item in events if item.get("event") == "network_budget_planned"]
    summaries = [item for item in events if item.get("event") == "network_budget_summary"]
    if len(planned) != 1 or len(summaries) != 1:
        raise S108ReleaseVerificationError("Job network budget logs are incomplete")
    expected_planned = {
        "endpoint_limit": 12,
        "sina_endpoint_limit": 4,
        "estimated_akshare_http_request_limit": 20,
        "estimated_baostock_protocol_message_limit": 12,
        "application_retries": 0,
    }
    if any(planned[0].get(key) != expected for key, expected in expected_planned.items()):
        raise S108ReleaseVerificationError("Job planned network budget is invalid")
    budget = summaries[0].get("network_budget")
    if not isinstance(budget, dict):
        raise S108ReleaseVerificationError("Job network budget summary is invalid")
    actual = budget.get("endpoint_actual")
    logged_planned = budget.get("endpoint_planned")
    if not isinstance(actual, dict) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in actual.values()
    ):
        raise S108ReleaseVerificationError("Job actual network budget is invalid")
    upstreams = PRODUCTION_UPSTREAMS
    expected_keys = {upstream.value for upstream in upstreams}
    expected_limits: object = {
        "total": 12,
        "per_source": {upstream.value: 4 for upstream in PRODUCTION_UPSTREAMS},
    }
    if (
        budget.get("endpoint_limits") != expected_limits
        or set(actual) != expected_keys
        or logged_planned != dict(planned_chunks)
        or budget.get("endpoint_actual_total") != sum(actual.values())
        or budget.get("endpoint_planned_total") != sum(planned_chunks.values())
        or budget.get("application_retries") != 0
        or sum(actual.values()) > 12
        or any(actual.get(item.value, 0) > 4 for item in upstreams)
        or any(actual[key] > planned_chunks[key] for key in expected_keys)
        or any(actual[key] != raw_responses[key] + failed_batches[key] for key in expected_keys)
    ):
        raise S108ReleaseVerificationError("Job actual network budget is invalid")
    result = dict(budget)
    result["actual"] = dict(actual)
    return result


def _verify_data_center(
    *,
    data_root: Path,
    version_id: str,
    input_batch_ids: list[str],
) -> dict[str, object]:
    database = data_root / "metadata" / "quantking.db"
    with readonly_data_uow_factory(database) as uow_factory:
        preview_adapter = CanonicalQueryAdapter(
            data_root=data_root,
            uow_factory=uow_factory,
        )
        service = DataCenterQueryService(
            data_uow_factory=uow_factory,
            preview=cast(CanonicalPreviewPort, preview_adapter),
            difference_verifier=cast(
                DifferenceVerifierPort,
                DifferenceReportVerifier(artifacts_root=data_root / "artifacts"),
            ),
            resolution_verifier=ResolutionArtifactVerifier(artifacts_root=data_root / "artifacts"),
            jobs=cast(Any, object()),
        )
        lineage = service.get_version_lineage(version_id)
        page = service.preview(
            version_id=version_id,
            fields=None,
            codes=None,
            start_date=None,
            end_date=None,
            cursor=None,
            limit=1,
        )
        next_page = (
            None
            if page.next_cursor is None
            else service.preview(
                version_id=version_id,
                fields=None,
                codes=None,
                start_date=None,
                end_date=None,
                cursor=page.next_cursor,
                limit=1,
            )
        )
    version = lineage.get("version")
    batches = lineage.get("source_batches")
    if (
        not isinstance(version, Mapping)
        or version.get("version_id") != version_id
        or not isinstance(batches, list)
        or {str(item.get("batch_id")) for item in batches if isinstance(item, Mapping)}
        != set(input_batch_ids)
        or len(page.items) != 1
    ):
        raise S108ReleaseVerificationError("DataCenter explicit version read is invalid")
    first = page.items[0]
    if not isinstance(first, Mapping):
        raise S108ReleaseVerificationError("DataCenter preview row is invalid")
    if next_page is not None and (
        len(next_page.items) != 1
        or (
            str(next_page.items[0]["code"]),
            str(next_page.items[0]["trade_date"]),
        )
        <= (str(first["code"]), str(first["trade_date"]))
    ):
        raise S108ReleaseVerificationError("DataCenter preview pagination is invalid")
    serialized = json.dumps(lineage, ensure_ascii=False, sort_keys=True)
    if str(data_root) in serialized or "artifact_relative_path" in serialized:
        raise S108ReleaseVerificationError("DataCenter lineage exposes a managed path")
    return {
        "version_id": version_id,
        "lineage_batch_count": len(batches),
        "preview_first_key": [str(first["code"]), str(first["trade_date"])],
        "preview_has_next": page.next_cursor is not None,
    }


def _verify_release(
    connection: sqlite3.Connection, data_root: Path, version_id: str
) -> dict[str, object]:
    revision = _one(connection, "SELECT version_num FROM alembic_version", label="revision")
    if revision["version_num"] != S108_RELEASE_REVISION:
        raise S108ReleaseVerificationError("metadata revision is not S1-08 release revision")

    version = _one(
        connection,
        "SELECT * FROM dataset_versions WHERE version_id = ?",
        (version_id,),
        label="DatasetVersion",
    )
    if version["dataset"] != _DATASET or version["status"] != "PUBLISHED":
        raise S108ReleaseVerificationError("DatasetVersion is not the published daily release")
    if (
        version["code_version"] != S107K4_CODE_VERSION
        or version["rule_version"] != K4_DAILY_RULE_VERSION
    ):
        raise S108ReleaseVerificationError("DatasetVersion release versions are invalid")
    if version["failure_json"] is not None or not version["published_at"]:
        raise S108ReleaseVerificationError("DatasetVersion terminal metadata is invalid")

    resolution = _one(
        connection,
        "SELECT * FROM resolution_runs WHERE candidate_version_id = ?",
        (version_id,),
        label="ResolutionRun",
    )
    job = _one(
        connection,
        "SELECT * FROM job_runs WHERE job_id = ?",
        (resolution["created_job_id"],),
        label="Job",
    )
    if (
        job["status"] != "SUCCEEDED"
        or job["job_type"] != "data.eod.v1"
        or job["failure_json"] is not None
    ):
        raise S108ReleaseVerificationError("release Job is not SUCCEEDED")
    frozen = parse_frozen_eod_command(
        job_type=str(job["job_type"]),
        input_manifest=_json_object(job["input_manifest_json"], label="Job input manifest"),
        config_manifest=_json_object(job["config_manifest_json"], label="Job config manifest"),
        version_manifest=_json_object(job["version_manifest_json"], label="Job version manifest"),
    )
    expected_code_version = S107K4_CODE_VERSION
    expected_adapter_version = S107K4_ADAPTER_VERSION
    expected_idempotency_key = S107K4_IDEMPOTENCY_KEY
    expected_upstreams = PRODUCTION_UPSTREAMS
    if (
        frozen.request.start_date != S108_TARGET_START
        or frozen.request.end_date != S108_TARGET_END
        or tuple(item.dataset.value for item in frozen.datasets) != (_DATASET,)
        or frozen.datasets[0].scope != S108_FIRST_RELEASE_SYMBOLS
        or frozen.code_version != expected_code_version
        or frozen.adapter_version != expected_adapter_version
        or frozen.rule_version != K4_DAILY_RULE_VERSION
        or frozen.resolution_rule_version != K4_RESOLUTION_RULE_VERSION
        or job["idempotency_key"] != expected_idempotency_key
        or frozen.source_route_version != "BAO-TX-SINA.k4.v1"
        or frozen.baostock_version != BAOSTOCK_DISTRIBUTION_VERSION
        or frozen.baostock_unit_policy_version != BAOSTOCK_UNIT_POLICY_VERSION
        or frozen.akshare_unit_policy_version != AKSHARE_UNIT_POLICY_VERSION
    ):
        raise S108ReleaseVerificationError("Job is not the fixed S1-08 release request")
    expected_fingerprint = eod_input_fingerprint(frozen)
    if resolution["input_fingerprint"] != expected_fingerprint:
        raise S108ReleaseVerificationError("ResolutionRun input fingerprint is invalid")

    batch_rows = _rows(
        connection,
        "SELECT * FROM ingestion_batches WHERE job_run_id = ? ORDER BY batch_id",
        (job["job_id"],),
    )
    if not 2 <= len(batch_rows) <= 12:
        raise S108ReleaseVerificationError("release Job batch count is outside the fixed bound")
    input_batch_ids = [
        str(item)
        for item in _json_list(version["input_batch_ids_json"], label="version input batches")
    ]
    lineage_rows = _rows(
        connection,
        "SELECT batch_id, ordinal FROM dataset_version_batches "
        "WHERE version_id = ? ORDER BY ordinal",
        (version_id,),
    )
    if [row["batch_id"] for row in lineage_rows] != input_batch_ids:
        raise S108ReleaseVerificationError("DatasetVersion batch lineage is inconsistent")
    if set(input_batch_ids) != {row["batch_id"] for row in batch_rows}:
        raise S108ReleaseVerificationError("DatasetVersion batch lineage differs from release Job")

    raw_chunks: dict[str, list[tuple[str, str, str, str, str]]] = {
        upstream.value: [] for upstream in expected_upstreams
    }
    raw_response_count = Counter({upstream.value: 0 for upstream in expected_upstreams})
    planned_chunk_count = Counter({upstream.value: 0 for upstream in expected_upstreams})
    failed_batch_count = Counter({upstream.value: 0 for upstream in expected_upstreams})
    partial_batch_count = Counter({upstream.value: 0 for upstream in expected_upstreams})
    batch_upstream_by_id: dict[str, str] = {}
    for batch in batch_rows:
        if batch["dataset"] != _DATASET:
            raise S108ReleaseVerificationError("release Raw batch dataset is invalid")
        params = _json_object(batch["request_params_json"], label="Raw request parameters")
        upstream = params.get("upstream")
        if (
            not isinstance(upstream, str)
            or upstream not in _ENDPOINTS
            or upstream not in {item.value for item in expected_upstreams}
            or params.get("provider") != _PROVIDERS[upstream]
            or params.get("endpoint") != _ENDPOINTS[upstream]
            or params.get("lookback_days") != "0"
            or params.get("adapter_version") != expected_adapter_version
        ):
            raise S108ReleaseVerificationError("Raw provider/upstream/endpoint binding is invalid")
        batch_upstream_by_id[str(batch["batch_id"])] = upstream
        scope = params.get("scope")
        try:
            scope_values = json.loads(scope) if isinstance(scope, str) else None
        except json.JSONDecodeError as error:
            raise S108ReleaseVerificationError("Raw scope is invalid") from error
        if not isinstance(scope_values, list) or not set(scope_values).issubset(
            set(S108_FIRST_RELEASE_SYMBOLS)
        ):
            raise S108ReleaseVerificationError("Raw scope escapes the fixed release")
        try:
            plan = build_incremental_plan(
                IncrementalPlanRequest(
                    batch_id=parse_entity_id(str(batch["batch_id"])),
                    dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                    scope=tuple(str(item) for item in scope_values),
                    request_start=TradingDate.from_iso(str(params["request_start"])),
                    request_end=TradingDate.from_iso(str(params["request_end"])),
                    start_inclusive=True,
                    end_inclusive=True,
                    known_coverage=(),
                    lookback_days=0,
                    max_days_per_chunk=31,
                    max_items_per_chunk=1,
                    adapter_version=expected_adapter_version,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise S108ReleaseVerificationError("Raw incremental plan is invalid") from error
        library_key = "baostock_version" if upstream == "BAOSTOCK" else "akshare_version"
        library_version = "0.9.3" if upstream == "BAOSTOCK" else "1.18.83"
        lineage_keys = {"provider", "upstream", "endpoint", library_key}
        if (
            set(params) != set(plan.request_params) | lineage_keys
            or any(params[key] != value for key, value in plan.request_params.items())
            or params.get(library_key) != library_version
            or batch["request_params_sha256"]
            != hashlib.sha256(
                json.dumps(
                    params,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            or batch["adapter_version"] != expected_adapter_version
            or batch["source"] != upstream
        ):
            raise S108ReleaseVerificationError("Raw incremental plan binding is invalid")
        planned_chunk_count[upstream] += len(plan.chunks)
        raw_directory = (
            data_root / "raw" / upstream.lower() / _DATASET / path_safe_id(str(batch["batch_id"]))
        )
        if batch["status"] == "FAILED":
            failure_directory = raw_directory.with_name(f"{raw_directory.name}.incomplete")
            if raw_directory.exists() or not failure_directory.is_dir():
                raise S108ReleaseVerificationError("failed Raw attempt directory is invalid")
            if {item.name for item in failure_directory.iterdir()} != {
                "failure.json",
                "input_listing.json",
            }:
                raise S108ReleaseVerificationError("failed Raw attempt file set is invalid")
            failure = _json_object(
                (failure_directory / "failure.json").read_text(encoding="utf-8"),
                label="Raw failure artifact",
            )
            try:
                source_error = SourceAdapterError(SourceErrorCode(str(failure.get("error_code"))))
            except ValueError as error:
                raise S108ReleaseVerificationError("Raw failure code is invalid") from error
            database_failure = _json_object(batch["failure_json"], label="Raw database failure")
            if (
                failure.get("schema_version") != "raw-attempt-failure.v1"
                or failure.get("batch_id") != path_safe_id(str(batch["batch_id"]))
                or failure.get("source") != upstream.lower()
                or failure.get("dataset") != _DATASET
                or failure.get("code_version") != expected_code_version
                or failure.get("adapter_version") != expected_adapter_version
                or database_failure.get("error_type") != source_error.failure_kind.value
                or database_failure.get("retryable") is not source_error.retryable
                or _json_list(batch["manifest_files_json"], label="Raw failed files")
                or batch["row_count"] is not None
                or batch["security_count"] is not None
            ):
                raise S108ReleaseVerificationError("Raw failure artifact binding is invalid")
            failed_batch_count[upstream] += 1
            continue
        if batch["status"] not in {"SUCCEEDED", "PARTIAL"}:
            raise S108ReleaseVerificationError("release Raw batch status is invalid")
        if (batch["status"] == "SUCCEEDED") != (batch["failure_json"] is None):
            raise S108ReleaseVerificationError("release Raw failure status is invalid")
        verified_raw = verify_raw_directory(raw_directory, data_root=data_root)
        raw_params = verified_raw.manifest.get("request_params")
        if (
            verified_raw.manifest.get("batch_id") != path_safe_id(str(batch["batch_id"]))
            or verified_raw.manifest.get("adapter_version") != expected_adapter_version
            or not isinstance(raw_params, dict)
            or raw_params.get("provider") != _PROVIDERS[upstream]
            or raw_params.get("upstream") != upstream
            or raw_params.get("endpoint") != _ENDPOINTS[upstream]
            or raw_params.get("lookback_days") != "15"
            or (
                batch["status"] == "SUCCEEDED"
                and (
                    raw_params.get("target_start") != params.get("request_start")
                    or raw_params.get("target_end") != params.get("request_end")
                )
            )
        ):
            raise S108ReleaseVerificationError("Raw manifest does not match metadata")
        files = verified_raw.manifest.get("files")
        if not isinstance(files, list):
            raise S108ReleaseVerificationError("Raw file list is invalid")
        metadata_files = [
            {
                "relative_path": (
                    Path("raw")
                    / upstream.lower()
                    / _DATASET
                    / path_safe_id(str(batch["batch_id"]))
                    / str(item["relative_path"])
                ).as_posix(),
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in files
            if isinstance(item, dict)
        ]
        if _json_list(batch["manifest_files_json"], label="Raw manifest files") != metadata_files:
            raise S108ReleaseVerificationError("Raw file inventory differs from metadata")
        response_payloads: list[bytes] = []
        canonical_rows = 0
        canonical_symbols: set[str] = set()
        response_completeness: list[bool] = []
        raw_response_count[upstream] += len(files)
        for sequence, item in enumerate(files):
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                raise S108ReleaseVerificationError("Raw response entry is invalid")
            if sequence >= len(plan.chunks):
                raise S108ReleaseVerificationError("Raw response count exceeds generated plan")
            expected_chunk = plan.chunks[sequence]
            relative_path = str(item["relative_path"])
            if relative_path != f"responses/chunk-{sequence:05d}.json":
                raise S108ReleaseVerificationError("Raw response order is invalid")
            payload = (raw_directory / Path(relative_path)).read_bytes()
            try:
                response = SupplierResponse.from_raw_payload(
                    payload,
                    expected_chunk=expected_chunk,
                )
            except (SourceAdapterError, TypeError, ValueError) as error:
                raise S108ReleaseVerificationError("Raw SupplierResponse is invalid") from error
            expected_units = (
                None
                if upstream == DataUpstream.BAOSTOCK.value
                else resolve_returned_units(
                    DataUpstream(upstream),
                    expected_chunk.scope[0],
                    "1.18.83",
                )
            )
            if (
                response.contract.lineage.provider.value != _PROVIDERS[upstream]
                or response.contract.lineage.upstream.value != upstream
                or response.contract.lineage.endpoint != _ENDPOINTS[upstream]
                or response.contract.lineage.library_version != library_version
                or response.contract.volume_unit
                is not (VolumeUnit.SHARE if expected_units is None else expected_units.volume_unit)
                or response.contract.amount_unit is not AmountUnit.CNY_YUAN
            ):
                raise S108ReleaseVerificationError("Raw SupplierResponse contract is invalid")
            response_completeness.append(response.complete)
            binding = (
                expected_chunk.scope[0],
                response.request_window.target_start.isoformat(),
                response.request_window.target_end.isoformat(),
                response.request_window.upstream_start.isoformat(),
                response.request_window.upstream_end.isoformat(),
            )
            target_start = response.request_window.target_start
            target_end = response.request_window.target_end
            upstream_start = response.request_window.upstream_start
            if (
                binding[0] not in S108_FIRST_RELEASE_SYMBOLS
                or not S108_TARGET_START <= target_start <= target_end <= S108_TARGET_END
                or (target_end - target_start).days >= 31
                or upstream_start != target_start - timedelta(days=15)
                or binding[4] != binding[2]
                or response.request_window.lookback_days != 15
            ):
                raise S108ReleaseVerificationError("Raw target/upstream window is invalid")
            response_payloads.append(payload)
            canonical_rows += len(response.records)
            canonical_symbols.update(str(item["stock_code"]) for item in response.records)
            raw_chunks[upstream].append(binding)
        if (
            (
                batch["status"] == "SUCCEEDED"
                and (len(files) != len(plan.chunks) or not all(response_completeness))
            )
            or (
                batch["status"] == "PARTIAL"
                and (
                    not files
                    or len(files) > len(plan.chunks)
                    or response_completeness[:-1] != [True] * (len(files) - 1)
                    or response_completeness[-1] is not False
                )
            )
            or raw_params.get("content_sha256")
            != hashlib.sha256(b"".join(response_payloads)).hexdigest()
            or int(str(raw_params.get("canonical_row_count"))) != canonical_rows
            or batch["row_count"] != canonical_rows
            or batch["security_count"] != len(canonical_symbols)
        ):
            raise S108ReleaseVerificationError("Raw response totals are invalid")
        if batch["status"] == "PARTIAL":
            first_binding = raw_chunks[upstream][-len(files)]
            last_binding = raw_chunks[upstream][-1]
            if (
                raw_params.get("request_start") != first_binding[1]
                or raw_params.get("target_start") != first_binding[1]
                or raw_params.get("upstream_start") != first_binding[3]
                or raw_params.get("request_end") != last_binding[2]
                or raw_params.get("target_end") != last_binding[2]
                or raw_params.get("upstream_end") != last_binding[4]
            ):
                raise S108ReleaseVerificationError(
                    "Raw partial manifest does not bind the completed prefix"
                )
            database_failure = _json_object(
                batch["failure_json"],
                label="Raw partial failure",
            )
            if (
                database_failure.get("error_type")
                not in {
                    "NETWORK_FAILURE",
                    "PARTIAL_SOURCE_FAILURE",
                }
                or database_failure.get("retryable") is not True
            ):
                raise S108ReleaseVerificationError("Raw partial failure binding is invalid")
            partial_batch_count[upstream] += 1
    expected_chunks = sorted(_expected_chunks())
    primary_upstreams = (DataUpstream.BAOSTOCK.value, DataUpstream.TENCENT.value)
    for upstream in primary_upstreams:
        if planned_chunk_count[upstream] != 4:
            raise S108ReleaseVerificationError("A/B planned inventory is not four chunks")
        call_failed_count = failed_batch_count[upstream] + partial_batch_count[upstream]
        if call_failed_count:
            if (
                call_failed_count != 1
                or not set(raw_chunks[upstream]).issubset(set(expected_chunks))
                or len(raw_chunks[upstream]) != raw_response_count[upstream]
            ):
                raise S108ReleaseVerificationError("failed A/B Raw inventory is invalid")
        elif sorted(raw_chunks[upstream]) != expected_chunks or raw_response_count[upstream] != 4:
            raise S108ReleaseVerificationError("successful A/B Raw inventory is invalid")
    exceptional_upstreams = (DataUpstream.SINA.value,)
    if any(planned_chunk_count[item] > 4 for item in exceptional_upstreams):
        raise S108ReleaseVerificationError("exception endpoint inventory exceeds four chunks")

    final_quality = _one(
        connection,
        "SELECT * FROM quality_runs WHERE run_id = ?",
        (version["quality_run_id"],),
        label="final QualityRun",
    )
    if (
        final_quality["version_id"] != version_id
        or final_quality["status"] != "PASSED"
        or final_quality["rule_version"] != K4_DAILY_RULE_VERSION
        or final_quality["schema_version"] != version["schema_version"]
        or final_quality["content_sha256"] != version["content_sha256"]
    ):
        raise S108ReleaseVerificationError("final QualityRun does not bind the published version")
    final_results = _rows(
        connection,
        "SELECT passed FROM quality_results WHERE run_id = ?",
        (final_quality["run_id"],),
    )
    if not final_results or any(row["passed"] != 1 for row in final_results):
        raise S108ReleaseVerificationError("final QualityRun contains a blocking result")

    warehouse_directory = data_root / "warehouse" / _DATASET / path_safe_id(version_id)
    verified_version = verify_canonical_directory(warehouse_directory, data_root=data_root)
    manifest = verified_version.manifest
    if (
        manifest.get("version_id") != version_id
        or verified_version.manifest_sha != version["manifest_sha256"]
        or manifest.get("content_sha256") != version["content_sha256"]
        or manifest.get("input_batch_ids") != input_batch_ids
    ):
        raise S108ReleaseVerificationError("published manifest does not bind DatasetVersion")
    table_summary = _verify_table(warehouse_directory, manifest)
    if (
        table_summary["row_count"] != version["row_count"]
        or table_summary["security_count"] != version["security_count"]
        or table_summary["coverage_start"] != version["coverage_start"]
        or table_summary["coverage_end"] != version["coverage_end"]
        or table_summary["content_sha256"] != version["content_sha256"]
    ):
        raise S108ReleaseVerificationError("published logical content does not bind metadata")
    database_coverage = _json_object(
        version["coverage_summary_json"],
        label="DatasetVersion coverage summary",
    )
    if (
        manifest.get("coverage_start") != version["coverage_start"]
        or manifest.get("coverage_end") != version["coverage_end"]
        or manifest.get("coverage_summary") != database_coverage
    ):
        raise S108ReleaseVerificationError("published coverage does not bind metadata")

    report_rows = _rows(
        connection,
        "SELECT * FROM difference_reports WHERE version_id = ? ORDER BY report_id",
        (version_id,),
    )
    if not 1 <= len(report_rows) <= 9:
        raise S108ReleaseVerificationError("DifferenceReport count is outside the fixed bound")
    difference_verifier = DifferenceReportVerifier(artifacts_root=data_root / "artifacts")
    report_ids: list[str] = []
    report_bindings: list[dict[str, str]] = []
    source_quality_ids: set[str] = set()
    source_content_by_batch: dict[str, str] = {}
    for report in report_rows:
        report_id = str(report["report_id"])
        verified_report = difference_verifier.verify(report_id=parse_entity_id(report_id))
        if (
            verified_report.report_relative_path != report["report_relative_path"]
            or verified_report.report_sha256 != report["report_sha256"]
            or str(verified_report.primary_batch_id) != report["primary_batch_id"]
            or str(verified_report.fallback_batch_id) != report["fallback_batch_id"]
            or str(verified_report.primary_version_id) != version_id
            or str(verified_report.fallback_version_id) != version_id
        ):
            raise S108ReleaseVerificationError("DifferenceReport artifact binding is invalid")
        report_bindings.append(
            {
                "report_id": report_id,
                "artifact_relative_path": verified_report.report_relative_path,
                "manifest_sha256": verified_report.manifest_sha256,
            }
        )
        for side in ("primary", "fallback"):
            quality = _one(
                connection,
                "SELECT * FROM quality_runs WHERE run_id = ?",
                (report[f"{side}_quality_run_id"],),
                label=f"DifferenceReport {side} QualityRun",
            )
            if (
                quality["status"] != "PASSED"
                or quality["version_id"] != version_id
                or quality["content_sha256"] != report[f"{side}_content_sha256"]
            ):
                raise S108ReleaseVerificationError(
                    "DifferenceReport source quality binding is invalid"
                )
            source_quality_ids.add(str(quality["run_id"]))
            batch_id = str(report[f"{side}_batch_id"])
            content_sha = str(report[f"{side}_content_sha256"])
            existing_sha = source_content_by_batch.setdefault(batch_id, content_sha)
            if existing_sha != content_sha:
                raise S108ReleaseVerificationError(
                    "DifferenceReport batch content binding is inconsistent"
                )
        report_ids.append(report_id)

    quality_rows = _rows(
        connection,
        "SELECT * FROM quality_runs WHERE version_id = ? ORDER BY run_id",
        (version_id,),
    )
    expected_passed_quality_ids = source_quality_ids | {str(final_quality["run_id"])}
    quality_by_id = {str(row["run_id"]): row for row in quality_rows}
    extra_quality_ids = set(quality_by_id) - expected_passed_quality_ids
    if not expected_passed_quality_ids.issubset(quality_by_id) or any(
        quality_by_id[run_id]["status"] != "PASSED" for run_id in expected_passed_quality_ids
    ):
        raise S108ReleaseVerificationError("release quality lineage is incomplete")
    if any(quality_by_id[run_id]["status"] != "FAILED" for run_id in extra_quality_ids):
        raise S108ReleaseVerificationError("release source quality failure is invalid")
    for quality in quality_rows:
        _verify_quality_artifact(connection, data_root, quality)
    failed_quality_contents = {
        str(quality_by_id[run_id]["content_sha256"]) for run_id in extra_quality_ids
    }

    if (
        resolution["rule_version"] != frozen.resolution_rule_version
        or resolution["candidate_version_id"] != version_id
        or resolution["created_job_id"] != job["job_id"]
        or resolution["input_fingerprint"] != expected_fingerprint
        or resolution["conflict_count"] != 0
        or resolution["blocker_count"] != 0
    ):
        raise S108ReleaseVerificationError("ResolutionRun is not publishable")
    verified_resolution = verify_resolution_artifact(
        data_root / "artifacts" / str(resolution["artifact_relative_path"]),
        artifacts_root=data_root / "artifacts",
    )
    summary = verified_resolution.summary
    difference_report_ids = summary.get("difference_report_ids")
    if not isinstance(difference_report_ids, list) or any(
        not isinstance(item, str) for item in difference_report_ids
    ):
        raise S108ReleaseVerificationError("ResolutionRun report binding is invalid")
    if (
        str(verified_resolution.run_id) != resolution["run_id"]
        or verified_resolution.manifest_sha256 != resolution["manifest_sha256"]
        or summary.get("candidate_version_id") != version_id
        or set(difference_report_ids) != set(report_ids)
        or summary.get("created_job_id") != job["job_id"]
        or summary.get("input_fingerprint") != expected_fingerprint
        or summary.get("total_decisions") != resolution["total_decisions"]
        or summary.get("exception_count") != resolution["exception_count"]
        or summary.get("conflict_count") != resolution["conflict_count"]
        or summary.get("blocker_count") != resolution["blocker_count"]
    ):
        raise S108ReleaseVerificationError("ResolutionRun artifact binding is invalid")
    expected_resolution_manifest = {
        "run_id": str(resolution["run_id"]),
        "artifact_relative_path": str(resolution["artifact_relative_path"]),
        "manifest_sha256": str(resolution["manifest_sha256"]),
        "rule_version": str(resolution["rule_version"]),
        "threshold_rule_version": str(resolution["threshold_rule_version"]),
        "input_fingerprint": expected_fingerprint,
        "candidate_version_id": version_id,
        "created_job_id": str(job["job_id"]),
        "difference_reports": report_bindings,
    }
    if manifest.get("resolution") != expected_resolution_manifest:
        raise S108ReleaseVerificationError("Warehouse resolution binding is invalid")
    input_bindings = summary.get("input_bindings")
    if not isinstance(input_bindings, list):
        raise S108ReleaseVerificationError("ResolutionRun input bindings are invalid")
    bound_batch_ids: set[str] = set()
    used_failed_quality_contents: set[str] = set()
    for binding in input_bindings:
        if not isinstance(binding, dict):
            raise S108ReleaseVerificationError("ResolutionRun input binding is invalid")
        binding_batch_id = binding.get("batch_id")
        if binding_batch_id is None:
            if binding.get("status") != "NOT_CALLED" or binding.get("content_sha256") is not None:
                raise S108ReleaseVerificationError("ResolutionRun absent input is invalid")
            continue
        batch_text = str(binding_batch_id)
        status = binding.get("status")
        database_batch = next(
            (row for row in batch_rows if row["batch_id"] == batch_text),
            None,
        )
        if (
            database_batch is None
            or binding.get("upstream") != batch_upstream_by_id.get(batch_text)
            or status not in {"AVAILABLE", "EMPTY", "CALL_FAILED", "QUALITY_FAILED"}
        ):
            raise S108ReleaseVerificationError("ResolutionRun source binding is invalid")
        if status == "CALL_FAILED":
            valid_binding = (
                database_batch["status"] in {"FAILED", "PARTIAL"}
                and binding.get("content_sha256") is None
            )
        elif status == "QUALITY_FAILED":
            valid_binding = (
                database_batch["status"] == "SUCCEEDED"
                and binding.get("content_sha256") in failed_quality_contents
            )
            if valid_binding:
                used_failed_quality_contents.add(str(binding["content_sha256"]))
        else:
            valid_binding = database_batch["status"] == "SUCCEEDED" and source_content_by_batch.get(
                batch_text
            ) == binding.get("content_sha256")
        if not valid_binding:
            raise S108ReleaseVerificationError("ResolutionRun source binding is invalid")
        bound_batch_ids.add(batch_text)
    if bound_batch_ids != set(input_batch_ids):
        raise S108ReleaseVerificationError("ResolutionRun source lineage is incomplete")
    if used_failed_quality_contents != failed_quality_contents:
        raise S108ReleaseVerificationError("ResolutionRun quality-failure lineage is incomplete")
    default_decision = summary.get("default_decision")
    if default_decision is not None and (
        not isinstance(default_decision, dict)
        or not isinstance(default_decision.get("agreeing_upstreams"), list)
        or len(default_decision["agreeing_upstreams"]) < 2
        or default_decision.get("selected_upstream") not in default_decision["agreeing_upstreams"]
    ):
        raise S108ReleaseVerificationError("ResolutionRun default decision lacks consensus")
    if any(
        len(decision.agreeing_upstreams) < 2
        or decision.selected_upstream not in decision.agreeing_upstreams
        or decision.conflict
        for decision in verified_resolution.exceptions
    ):
        raise S108ReleaseVerificationError("ResolutionRun exception lacks two-source consensus")
    decisions = (*verified_resolution.exceptions, *verified_resolution.conflicts)
    traces = [trace for decision in decisions for trace in decision.request_traces]
    if any(
        item.symbol not in S108_FIRST_RELEASE_SYMBOLS
        or not S108_TARGET_START <= item.start_date <= item.end_date <= S108_TARGET_END
        or item.upstream.value not in exceptional_upstreams
        for item in traces
    ):
        raise S108ReleaseVerificationError("ResolutionRun exception range escapes the release")
    unique_traces = {
        (item.upstream.value, item.symbol, item.start_date, item.end_date) for item in traces
    }
    expected_exception_chunks: dict[str, list[tuple[str, str, str, str, str]]] = {
        upstream: [] for upstream in exceptional_upstreams
    }
    for upstream, symbol, trace_start, trace_end in sorted(unique_traces):
        trace_plan = build_incremental_plan(
            IncrementalPlanRequest(
                batch_id=parse_entity_id("data_batch:00000000-0000-4000-8000-000000000000"),
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                scope=(symbol,),
                request_start=TradingDate(trace_start),
                request_end=TradingDate(trace_end),
                start_inclusive=True,
                end_inclusive=True,
                known_coverage=(),
                lookback_days=0,
                max_days_per_chunk=31,
                max_items_per_chunk=1,
                adapter_version=expected_adapter_version,
            )
        )
        expected_exception_chunks[upstream].extend(
            (
                symbol,
                chunk.start.value.isoformat(),
                chunk.end.value.isoformat(),
                (chunk.start.value - timedelta(days=15)).isoformat(),
                chunk.end.value.isoformat(),
            )
            for chunk in trace_plan.chunks
        )
    for upstream in exceptional_upstreams:
        expected_chunks_for_source = sorted(expected_exception_chunks[upstream])
        actual_chunks_for_source = sorted(raw_chunks[upstream])
        has_incomplete = bool(failed_batch_count[upstream] or partial_batch_count[upstream])
        if (
            len(expected_chunks_for_source) > 4
            or len(expected_chunks_for_source) != planned_chunk_count[upstream]
            or (
                has_incomplete
                and not set(actual_chunks_for_source).issubset(set(expected_chunks_for_source))
            )
            or (not has_incomplete and actual_chunks_for_source != expected_chunks_for_source)
            or raw_response_count[upstream] != len(actual_chunks_for_source)
        ):
            raise S108ReleaseVerificationError(
                "ResolutionRun exception ranges differ from Raw inventory"
            )

    dangling = _rows(
        connection,
        "SELECT version_id FROM dataset_versions "
        "WHERE dataset = ? AND status IN ('BUILDING','VALIDATED')",
        (_DATASET,),
    )
    if dangling:
        raise S108ReleaseVerificationError("a non-terminal daily candidate is dangling")
    budget_log = _verify_log(
        data_root,
        job,
        planned_chunks=dict(planned_chunk_count),
        raw_responses=dict(raw_response_count),
        failed_batches=dict(failed_batch_count),
    )
    endpoint_actual_value = budget_log.get("actual")
    if not isinstance(endpoint_actual_value, dict):
        raise S108ReleaseVerificationError("Job actual network budget is invalid")
    endpoint_actual = {str(key): int(value) for key, value in endpoint_actual_value.items()}
    estimated_http = sum(
        endpoint_actual[key] * _HTTP_PER_ENDPOINT[key]
        for key in endpoint_actual
        if key in _HTTP_PER_ENDPOINT
    )
    if estimated_http > 20:
        raise S108ReleaseVerificationError("locked-source HTTP upper bound exceeds twenty")
    data_center_read = _verify_data_center(
        data_root=data_root,
        version_id=version_id,
        input_batch_ids=input_batch_ids,
    )
    return {
        "schema_version": "s1-08-release-verification.v1",
        "status": "VERIFIED",
        "revision": S108_RELEASE_REVISION,
        "version_id": version_id,
        "job_id": str(job["job_id"]),
        "dataset": _DATASET,
        "scope": list(S108_FIRST_RELEASE_SYMBOLS),
        "target_window": {
            "start": S108_TARGET_START.isoformat(),
            "end": S108_TARGET_END.isoformat(),
        },
        "upstream_window": {
            "start": (S108_TARGET_START - timedelta(days=15)).isoformat(),
            "end": S108_TARGET_END.isoformat(),
        },
        "coverage": table_summary,
        "quality_run_id": str(final_quality["run_id"]),
        "difference_report_ids": report_ids,
        "resolution_run_id": str(resolution["run_id"]),
        "endpoint_invocations": dict(sorted(endpoint_actual.items())),
        "estimated_http_request_upper_bound": 20,
        "estimated_http_requests_for_actual_inventory": estimated_http,
        "network_budget_log": budget_log,
        "data_center_explicit_version_read": data_center_read,
    }


def verify_s108_release(*, data_root: Path, version_id: str) -> dict[str, object]:
    """Verify one explicit fixed S1-08 release without mutating any evidence."""
    canonical_id = _release_version_id(version_id)
    if not isinstance(data_root, Path) or not data_root.is_absolute():
        raise S108ReleaseVerificationError("data_root must be an absolute Path")
    try:
        with _readonly_database(data_root / "metadata" / "quantking.db") as connection:
            return _verify_release(connection, data_root, canonical_id)
    except S108ReleaseVerificationError:
        raise
    except (
        DifferenceReportVerificationError,
        FrozenEodCommandError,
        ManifestIntegrityError,
        OSError,
        ResolutionArtifactVerificationError,
        pa.ArrowInvalid,
    ) as error:
        raise S108ReleaseVerificationError(
            "release evidence integrity verification failed"
        ) from error


__all__ = [
    "S108_RELEASE_REVISION",
    "S108ReleaseVerificationError",
    "verify_s108_release",
]
