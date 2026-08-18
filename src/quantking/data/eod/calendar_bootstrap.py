"""S1-08R3-C1 formal exchange-calendar bootstrap (BaoStock role-A, bounded).

The formal daily-bar release hard-depends on a PUBLISHED
``exchange_calendar`` version bound by id and content SHA. The I0 inventory
(2026-08-17) proved the formal data root has none and that no production
path publishes one. This module is that path, built to run exactly once
under the separately authorized S1-08R3-C1 marker:

* one ``query_trade_dates`` endpoint invocation in one isolated subprocess
  (login/query/logout protocol messages counted and budget-guarded);
* zero application retries, fail fast, hard parent-enforced timeout;
* the provider response lands as an immutable raw batch
  (``raw/baostock/exchange_calendar/<batch_id>/``) before any canonical
  write, preserving the append-only raw contract;
* the SH/SZ projection is built from the single BaoStock trading-day
  series (both exchanges follow the same CSRC holiday schedule), quality
  checked with the default rule set, and published through the same
  ``CanonicalPublishCoordinator`` as every other canonical version;
* a single-use attempt ledger consumes the authorization before the first
  provider call; failure and completion facts are append-only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from quantking.common.ids import EntityId
from quantking.common.time import TradingDate, UtcTimestamp, utc_now
from quantking.data.adapters.calendar_projection import (
    project_exchange_calendar_table as build_exchange_calendar_table,
)
from quantking.data.adapters.canonical_content import canonical_content_sha256
from quantking.data.adapters.sources.baostock import (
    BAOSTOCK_CALENDAR_ADAPTER_VERSION,
    BAOSTOCK_CALENDAR_ENDPOINT,
    BaostockCalendarResult,
    BaostockCalendarRunner,
)
from quantking.data.adapters.sources.baostock_worker import BAOSTOCK_DISTRIBUTION_VERSION
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.manifest import RawManifestFile, path_safe_id
from quantking.data.adapters.warehouse.publish_coordinator import (
    CanonicalPublishCoordinator,
    CanonicalPublishCoordinatorRequest,
)
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchPublisher,
    RawBatchPublishRequest,
)
from quantking.data.adapters.warehouse.verifier import verify_raw_directory
from quantking.data.eod.formal_authorization import (
    FormalAttemptLedger,
    FormalAuthorizationError,
    compute_code_tree_sha256,
)
from quantking.data.models import (
    DataProvider,
    DatasetKind,
    DatasetVersion,
    DataUpstream,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    transition_batch,
)
from quantking.data.quality.artifacts import QualityArtifactWriter
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import QualityRequest, RuleOutcomeStatus
from quantking.data.quality.rules import default_rule_set

CALENDAR_BOOTSTRAP_PLAN_SCHEMA_VERSION = "s1-08r3-calendar-bootstrap-plan.v1"
CALENDAR_BOOTSTRAP_RULE_VERSION = "quality-rules.v1"
CALENDAR_BOOTSTRAP_CODE_VERSION = "s1-08r3-calendar.v1"
CALENDAR_BOOTSTRAP_SOURCE = "baostock"
CALENDAR_BOOTSTRAP_EXCHANGES: tuple[str, ...] = ("SH", "SZ")
CALENDAR_BOOTSTRAP_ENDPOINT_MAX = 1
CALENDAR_BOOTSTRAP_PROTOCOL_MAX = 4
CALENDAR_BOOTSTRAP_TIMEOUT_SECONDS = 20.0
CALENDAR_BOOTSTRAP_NETWORK_SCOPE = "BaoStock query_trade_dates only; zero HTTP; zero retries"
CALENDAR_BOOTSTRAP_DATASET = DatasetKind.EXCHANGE_CALENDAR.value
CALENDAR_SCHEMA_VERSION = "exchange_calendar.v1"
CALENDAR_RAW_RESPONSE_FILE = "responses/query-trade-dates.json"

_SAFE_MARKER = re.compile(r"S1-08R3-C1-[A-Z0-9-]{4,64}\Z")
_SAFE_AUTH_ID = re.compile(r"S1-08R3-C1-[a-f0-9]{12}\Z")
_SAFE_IDEMPOTENCY_KEY = re.compile(r"s1-08r3-c1-[a-z0-9][a-z0-9.-]{5,127}\Z")
_SAFE_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class CalendarBootstrapError(ValueError):
    """Stable fail-closed violation of the C1 calendar bootstrap contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, message: str) -> None:
    raise CalendarBootstrapError(code, message)


def build_calendar_bootstrap_plan(
    *,
    marker: str,
    authorization_id: str,
    start: date,
    end: date,
    code_tree_sha256: str,
    data_root_baseline_sha256: str,
    idempotency_key: str = "s1-08r3-c1-calendar-bootstrap-20260817-v1",
    exchanges: tuple[str, ...] = CALENDAR_BOOTSTRAP_EXCHANGES,
    protocol_max: int = CALENDAR_BOOTSTRAP_PROTOCOL_MAX,
    timeout_seconds: float = CALENDAR_BOOTSTRAP_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Build the zero-I/O C1 bootstrap plan without touching the filesystem."""
    if not isinstance(marker, str) or _SAFE_MARKER.fullmatch(marker) is None:
        _fail("PLAN_FIELD_INVALID", "marker must be an S1-08R3-C1 marker")
    if not isinstance(authorization_id, str) or _SAFE_AUTH_ID.fullmatch(authorization_id) is None:
        _fail("PLAN_FIELD_INVALID", "authorization_id must be S1-08R3-C1-<12 hex>")
    if (
        not isinstance(idempotency_key, str)
        or _SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
    ):
        _fail("PLAN_FIELD_INVALID", "idempotency_key must be a lowercase s1-08r3-c1 key")
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        _fail("PLAN_FIELD_INVALID", "window must be ordered dates")
    if (end - start).days > 400:
        _fail("PLAN_FIELD_INVALID", "window must stay bounded (<= 400 days)")
    for name, value in (
        ("code_tree_sha256", code_tree_sha256),
        ("data_root_baseline_sha256", data_root_baseline_sha256),
    ):
        if not isinstance(value, str) or _SAFE_SHA.fullmatch(value) is None:
            _fail("PLAN_FIELD_INVALID", f"{name} must be a lowercase SHA-256")
    if tuple(exchanges) != CALENDAR_BOOTSTRAP_EXCHANGES:
        _fail("PLAN_FIELD_INVALID", "exchanges must be exactly SH and SZ")
    if (
        isinstance(protocol_max, bool)
        or not isinstance(protocol_max, int)
        or not 3 <= protocol_max <= CALENDAR_BOOTSTRAP_PROTOCOL_MAX
    ):
        _fail("PLAN_FIELD_INVALID", "protocol_max must be 3..4")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds != CALENDAR_BOOTSTRAP_TIMEOUT_SECONDS
    ):
        _fail("PLAN_FIELD_INVALID", "timeout must stay at the frozen 20s hard limit")
    return {
        "schema_version": CALENDAR_BOOTSTRAP_PLAN_SCHEMA_VERSION,
        "authorization_marker": marker,
        "authorization_id": authorization_id,
        "idempotency_key": idempotency_key,
        "provider": "BAOSTOCK",
        "upstream": "BAOSTOCK",
        "endpoint": BAOSTOCK_CALENDAR_ENDPOINT,
        "adapter_version": BAOSTOCK_CALENDAR_ADAPTER_VERSION,
        "baostock_version": BAOSTOCK_DISTRIBUTION_VERSION,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "exchanges": list(CALENDAR_BOOTSTRAP_EXCHANGES),
        "budgets": {
            "endpoint_max": CALENDAR_BOOTSTRAP_ENDPOINT_MAX,
            "protocol_max": protocol_max,
            "http_max": 0,
            "application_retries": 0,
        },
        "hard_timeouts": {"session_seconds": CALENDAR_BOOTSTRAP_TIMEOUT_SECONDS},
        "fail_fast": True,
        "dataset": CALENDAR_BOOTSTRAP_DATASET,
        "schema_version_calendar": CALENDAR_SCHEMA_VERSION,
        "code_version": CALENDAR_BOOTSTRAP_CODE_VERSION,
        "rule_version": CALENDAR_BOOTSTRAP_RULE_VERSION,
        "allowed_write_paths": [
            {"relative_path": "raw/baostock/exchange_calendar/<batch_id>", "max_items": 1},
            {"relative_path": "warehouse/exchange_calendar/<version_id>", "max_items": 1},
            {"relative_path": "artifacts/quality/<quality_run_id>", "max_items": 1},
            {"relative_path": "metadata (dataset_versions + quality runs)", "max_items": 1},
            {
                "relative_path": "raw/baostock/exchange_calendar/<batch_id>.incomplete",
                "max_items": 1,
                "role": "failure-evidence-only",
            },
        ],
        "reads_legacy_root": False,
        "accesses_backup_root": False,
        "other_datasets_touched": False,
        "network_scope": CALENDAR_BOOTSTRAP_NETWORK_SCOPE,
        "code_tree_sha256": code_tree_sha256,
        "data_root_baseline_sha256": data_root_baseline_sha256,
        "requires_new_real_authorization": True,
    }


def calendar_bootstrap_plan_sha256(plan: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(plan),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_FROZEN_ALLOWED_WRITE_PATHS: tuple[dict[str, object], ...] = (
    {"relative_path": "raw/baostock/exchange_calendar/<batch_id>", "max_items": 1},
    {"relative_path": "warehouse/exchange_calendar/<version_id>", "max_items": 1},
    {"relative_path": "artifacts/quality/<quality_run_id>", "max_items": 1},
    {"relative_path": "metadata (dataset_versions + quality runs)", "max_items": 1},
    {
        "relative_path": "raw/baostock/exchange_calendar/<batch_id>.incomplete",
        "max_items": 1,
        "role": "failure-evidence-only",
    },
)
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "authorization_marker",
        "authorization_id",
        "idempotency_key",
        "provider",
        "upstream",
        "endpoint",
        "adapter_version",
        "baostock_version",
        "window",
        "exchanges",
        "budgets",
        "hard_timeouts",
        "fail_fast",
        "dataset",
        "schema_version_calendar",
        "code_version",
        "rule_version",
        "allowed_write_paths",
        "reads_legacy_root",
        "accesses_backup_root",
        "other_datasets_touched",
        "network_scope",
        "code_tree_sha256",
        "data_root_baseline_sha256",
        "requires_new_real_authorization",
    }
)


@dataclass(frozen=True, slots=True)
class CheckedCalendarBootstrapPlan:
    """Strongly typed, fully validated C1 bootstrap plan (ADR-028).

    ``parse_calendar_bootstrap_plan`` rejects unknown fields and any drift
    from the frozen template before the authorization token is checked, so a
    plan whose content changed can never execute under an old token.
    """

    marker: str
    authorization_id: str
    idempotency_key: str
    start: date
    end: date
    code_tree_sha256: str
    data_root_baseline_sha256: str
    protocol_max: int
    plan_sha256: str


def parse_calendar_bootstrap_plan(plan: Mapping[str, object]) -> CheckedCalendarBootstrapPlan:
    """Validate every C1 plan field against the frozen contract, fail closed."""

    if not isinstance(plan, Mapping):
        _fail("CALENDAR_PLAN_INVALID", "plan must be a JSON object")
    if set(plan) != _PLAN_FIELDS:
        missing = sorted(_PLAN_FIELDS - set(plan))
        extra = sorted(set(plan) - _PLAN_FIELDS)
        _fail(
            "CALENDAR_PLAN_INVALID",
            f"plan fields mismatch (missing={missing}, extra={extra})",
        )
    if plan["schema_version"] != CALENDAR_BOOTSTRAP_PLAN_SCHEMA_VERSION:
        _fail("CALENDAR_PLAN_INVALID", "plan schema version mismatch")
    marker = plan["authorization_marker"]
    authorization_id = plan["authorization_id"]
    idempotency_key = plan["idempotency_key"]
    if not isinstance(marker, str) or _SAFE_MARKER.fullmatch(marker) is None:
        _fail("CALENDAR_PLAN_INVALID", "marker must be an S1-08R3-C1 marker")
    if not isinstance(authorization_id, str) or _SAFE_AUTH_ID.fullmatch(authorization_id) is None:
        _fail("CALENDAR_PLAN_INVALID", "authorization_id must be S1-08R3-C1-<12 hex>")
    if (
        not isinstance(idempotency_key, str)
        or _SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
    ):
        _fail("CALENDAR_PLAN_INVALID", "idempotency_key must be a lowercase s1-08r3-c1 key")
    for name, expected in (
        ("provider", "BAOSTOCK"),
        ("upstream", "BAOSTOCK"),
        ("endpoint", BAOSTOCK_CALENDAR_ENDPOINT),
        ("adapter_version", BAOSTOCK_CALENDAR_ADAPTER_VERSION),
        ("baostock_version", BAOSTOCK_DISTRIBUTION_VERSION),
        ("dataset", CALENDAR_BOOTSTRAP_DATASET),
        ("schema_version_calendar", CALENDAR_SCHEMA_VERSION),
        ("code_version", CALENDAR_BOOTSTRAP_CODE_VERSION),
        ("rule_version", CALENDAR_BOOTSTRAP_RULE_VERSION),
        ("network_scope", CALENDAR_BOOTSTRAP_NETWORK_SCOPE),
    ):
        if plan[name] != expected:
            _fail("CALENDAR_PLAN_INVALID", f"{name} must equal the frozen C1 value")
    window = plan["window"]
    if not isinstance(window, Mapping) or set(window) != {"start", "end"}:
        _fail("CALENDAR_PLAN_INVALID", "window must contain exactly start and end")
    checked_window = cast("Mapping[str, object]", window)
    try:
        start = date.fromisoformat(str(checked_window["start"]))
        end = date.fromisoformat(str(checked_window["end"]))
    except ValueError:
        _fail("CALENDAR_PLAN_INVALID", "window dates must be ISO dates")
    if start > end or (end - start).days > 400:
        _fail("CALENDAR_PLAN_INVALID", "window must be ordered and bounded (<= 400 days)")
    if plan["exchanges"] != list(CALENDAR_BOOTSTRAP_EXCHANGES):
        _fail("CALENDAR_PLAN_INVALID", "exchanges must be exactly SH and SZ")
    budgets = plan["budgets"]
    if not isinstance(budgets, Mapping) or set(budgets) != {
        "endpoint_max",
        "protocol_max",
        "http_max",
        "application_retries",
    }:
        _fail("CALENDAR_PLAN_INVALID", "budgets fields are invalid")
    checked_budgets = cast("Mapping[str, object]", budgets)
    if (
        checked_budgets["endpoint_max"] != CALENDAR_BOOTSTRAP_ENDPOINT_MAX
        or checked_budgets["http_max"] != 0
        or checked_budgets["application_retries"] != 0
        or isinstance(checked_budgets["protocol_max"], bool)
        or not isinstance(checked_budgets["protocol_max"], int)
        or not 3 <= checked_budgets["protocol_max"] <= CALENDAR_BOOTSTRAP_PROTOCOL_MAX
    ):
        _fail("CALENDAR_PLAN_INVALID", "budgets must equal the frozen C1 values")
    if plan["hard_timeouts"] != {"session_seconds": CALENDAR_BOOTSTRAP_TIMEOUT_SECONDS}:
        _fail("CALENDAR_PLAN_INVALID", "hard timeouts must equal the frozen 20s session limit")
    if plan["fail_fast"] is not True:
        _fail("CALENDAR_PLAN_INVALID", "fail_fast must be true")
    allowed = plan["allowed_write_paths"]
    if not isinstance(allowed, list) or [
        dict(item) if isinstance(item, Mapping) else None for item in allowed
    ] != [dict(item) for item in _FROZEN_ALLOWED_WRITE_PATHS]:
        _fail("CALENDAR_PLAN_INVALID", "allowed write paths must equal the frozen template")
    for name in ("reads_legacy_root", "accesses_backup_root", "other_datasets_touched"):
        if plan[name] is not False:
            _fail("CALENDAR_PLAN_INVALID", f"{name} must be false")
    for name in ("code_tree_sha256", "data_root_baseline_sha256"):
        value = plan[name]
        if not isinstance(value, str) or _SAFE_SHA.fullmatch(value) is None:
            _fail("CALENDAR_PLAN_INVALID", f"{name} must be a lowercase SHA-256")
    if plan["requires_new_real_authorization"] is not True:
        _fail("CALENDAR_PLAN_INVALID", "plan must require new real authorization")
    return CheckedCalendarBootstrapPlan(
        marker=cast("str", marker),
        authorization_id=cast("str", authorization_id),
        idempotency_key=cast("str", idempotency_key),
        start=start,
        end=end,
        code_tree_sha256=str(plan["code_tree_sha256"]),
        data_root_baseline_sha256=str(plan["data_root_baseline_sha256"]),
        protocol_max=cast("int", checked_budgets["protocol_max"]),
        plan_sha256=calendar_bootstrap_plan_sha256(plan),
    )


def calendar_bootstrap_token(checked: CheckedCalendarBootstrapPlan) -> str:
    """ADR-028 content-bound C1 token: marker, id and the plan SHA."""

    return f"{checked.marker}:{checked.authorization_id}:{checked.plan_sha256}"


def execute_calendar_bootstrap(
    *,
    plan: Mapping[str, object],
    authorization: str | None,
    runtime_factory: Callable[[], Any],
    runner: BaostockCalendarRunner,
    attempts_root: Path,
    code_root: Path,
    version_id: EntityId,
    batch_id: EntityId,
    clock: Callable[[], UtcTimestamp] = utc_now,
) -> dict[str, object]:
    """Run the whole authorized C1 bootstrap exactly once, fail closed.

    ``runtime_factory`` is only invoked after the authorization token, the
    code-tree SHA and the single-use ledger have been verified, so an
    unauthorized call never constructs (or touches) the formal data root.
    """
    checked = parse_calendar_bootstrap_plan(plan)
    marker = checked.marker
    authorization_id = checked.authorization_id
    start = checked.start
    end = checked.end
    # ADR-028 content binding: the token carries the SHA of the exact plan
    # content, so any field change invalidates previously issued tokens.
    if not isinstance(authorization, str) or authorization != calendar_bootstrap_token(checked):
        _fail("CALENDAR_AUTHORIZATION_REQUIRED", "the C1 authorization token is missing")
    try:
        actual_tree = compute_code_tree_sha256(code_root / "src" / "quantking")
    except (OSError, FormalAuthorizationError):
        _fail("CALENDAR_CODE_TREE_UNREADABLE", "the production code tree cannot be hashed")
    if actual_tree != checked.code_tree_sha256:
        _fail("CALENDAR_CODE_TREE_DRIFT", "the production code tree drifted from the plan")

    ledger = FormalAttemptLedger(attempts_root)
    plan_sha = checked.plan_sha256
    try:
        ledger.begin(
            envelope_sha256=plan_sha,
            marker=marker,
            authorization_id=authorization_id,
            idempotency_key=checked.idempotency_key,
            now_iso=clock().to_iso(),
        )
    except FormalAuthorizationError as error:
        if error.code == "AUTHORIZATION_ALREADY_CONSUMED":
            _fail("CALENDAR_AUTHORIZATION_ALREADY_CONSUMED", "this C1 authorization was used")
        raise CalendarBootstrapError(
            "CALENDAR_LEDGER_INVALID",
            "the attempt ledger rejected the C1 plan credentials",
        ) from error

    def _record_failed(detail: dict[str, object]) -> None:
        with contextlib.suppress(FormalAuthorizationError):
            ledger.record(
                authorization_id,
                status="FAILED",
                now_iso=clock().to_iso(),
                detail=detail,
            )

    def _failure_detail(code: str, error: BaseException) -> dict[str, object]:
        # Stable evidence contract: bounded, exception-derived facts only —
        # never provider payloads, absolute foreign paths or secrets.
        return {
            "code": code,
            "error_type": type(error).__name__,
            "message": str(error)[:200],
        }

    try:
        try:
            runtime = runtime_factory()
        except Exception as error:
            _record_failed(_failure_detail("CALENDAR_RUNTIME_UNAVAILABLE", error))
            raise CalendarBootstrapError(
                "CALENDAR_RUNTIME_UNAVAILABLE",
                "the formal runtime could not be constructed",
            ) from error
        data_root = runtime.settings.paths.data_root
        uow_factory = runtime.data_uow_factory
        try:
            result: BaostockCalendarResult = runner.run(start_date=start, end_date=end)
        except Exception as error:
            _record_failed(_failure_detail("CALENDAR_PROVIDER_FAILED", error))
            raise CalendarBootstrapError(
                "CALENDAR_PROVIDER_FAILED",
                "the BaoStock calendar session failed; evidence is preserved",
            ) from error
        protocol_max = checked.protocol_max
        if result.protocol_messages > protocol_max:
            _record_failed(
                {
                    "code": "CALENDAR_BUDGET_EXCEEDED",
                    "protocol_messages": result.protocol_messages,
                    "protocol_max": protocol_max,
                }
            )
            raise CalendarBootstrapError(
                "CALENDAR_BUDGET_EXCEEDED",
                "BaoStock protocol messages exceeded the frozen budget",
            )
        request_payload = json.dumps(
            {
                "mode": "calendar",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        response_payload = json.dumps(
            {
                "days": [[day, flag] for day, flag in result.days],
                "response_metadata": {
                    "distribution_version": BAOSTOCK_DISTRIBUTION_VERSION,
                    "login_messages": result.login_messages,
                    "query_messages": result.query_messages,
                    "logout_messages": result.logout_messages,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        raw_publisher = RawBatchPublisher(data_root=data_root)
        raw_directory = raw_publisher.publish(
            RawBatchPublishRequest(
                source=CALENDAR_BOOTSTRAP_SOURCE,
                dataset=CALENDAR_BOOTSTRAP_DATASET,
                batch_id=path_safe_id(str(batch_id)),
                adapter_version=BAOSTOCK_CALENDAR_ADAPTER_VERSION,
                request_params={
                    "provider": "BAOSTOCK",
                    "upstream": "BAOSTOCK",
                    "endpoint": BAOSTOCK_CALENDAR_ENDPOINT,
                    "baostock_version": BAOSTOCK_DISTRIBUTION_VERSION,
                    "request_sha256": hashlib.sha256(request_payload).hexdigest(),
                },
                files=(
                    RawManifestFile(CALENDAR_RAW_RESPONSE_FILE, response_payload, len(result.days)),
                ),
                created_at=clock().to_iso(),
                code_version=CALENDAR_BOOTSTRAP_CODE_VERSION,
            )
        )
        verified = verify_raw_directory(raw_directory, data_root=data_root)
        prefix = raw_directory.relative_to(data_root).as_posix()
        manifest_entries = tuple(
            ManifestFileEntry(
                relative_path=f"{prefix}/{item.relative_path}",
                size=item.size,
                sha256=item.sha256,
            )
            for item in verified.files
        )
        batch = IngestionBatch.running(
            batch_id=batch_id,
            provider=DataProvider.BAOSTOCK,
            upstream=DataUpstream.BAOSTOCK,
            endpoint=BAOSTOCK_CALENDAR_ENDPOINT,
            library_version=BAOSTOCK_DISTRIBUTION_VERSION,
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            adapter_version=BAOSTOCK_CALENDAR_ADAPTER_VERSION,
            request_params={
                "provider": "BAOSTOCK",
                "upstream": "BAOSTOCK",
                "endpoint": BAOSTOCK_CALENDAR_ENDPOINT,
                "baostock_version": BAOSTOCK_DISTRIBUTION_VERSION,
                "request_sha256": hashlib.sha256(request_payload).hexdigest(),
            },
            started_at=clock(),
            job_run_id=None,
        )
        with uow_factory() as uow:
            uow.data.save_batch(batch)
            uow.commit()
        succeeded = transition_batch(
            batch,
            IngestionBatchStatus.SUCCEEDED,
            at=clock(),
            row_count=len(result.days),
            security_count=0,
            manifest_files=manifest_entries,
        )
        with uow_factory() as uow:
            uow.data.save_batch(succeeded)
            uow.commit()
        # Raw evidence is now durable; any candidate-shape failure below is a
        # quality failure that must preserve it.
        try:
            table = build_exchange_calendar_table(result.days)
            content_sha = canonical_content_sha256(DatasetKind.EXCHANGE_CALENDAR, [table])
        except ValueError as error:
            _record_failed({"code": "CALENDAR_QUALITY_FAILED"})
            raise CalendarBootstrapError(
                "CALENDAR_QUALITY_FAILED",
                "the calendar candidate failed canonical validation",
            ) from error
        engine = QualityEngine(rule_set=default_rule_set())
        quality_request = QualityRequest(
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            version_id=version_id,
            rule_version=CALENDAR_BOOTSTRAP_RULE_VERSION,
            schema_version=CALENDAR_SCHEMA_VERSION,
            code_version=CALENDAR_BOOTSTRAP_CODE_VERSION,
            content_sha256=content_sha,
            input_batch_ids=(batch_id,),
            expected_securities=(),
            expected_exchanges=CALENDAR_BOOTSTRAP_EXCHANGES,
            coverage_start=TradingDate(start),
            coverage_end=TradingDate(end),
            reference_data={},
            empty_result=None,
        )
        outcome = engine.execute(request=quality_request, table=table, reference_tables={})
        if outcome.status is not QualityRunStatus.PASSED:
            _record_failed(
                {
                    "code": "CALENDAR_QUALITY_FAILED",
                    "quality_run_id": str(outcome.run_id),
                }
            )
            raise CalendarBootstrapError(
                "CALENDAR_QUALITY_FAILED",
                "the calendar candidate failed the default quality rule set",
            )
        QualityArtifactWriter(artifacts_root=data_root / "artifacts" / "quality").write(
            outcome=outcome, row_evidence_tables={}
        )
        run = QualityRun(
            run_id=outcome.run_id,
            version_id=version_id,
            status=QualityRunStatus.PASSED,
            rule_version=CALENDAR_BOOTSTRAP_RULE_VERSION,
            schema_version=CALENDAR_SCHEMA_VERSION,
            content_sha256=content_sha,
            started_at=outcome.started_at,
            ended_at=outcome.ended_at,
            results=tuple(
                QualityResult(
                    rule_code=item.rule_code,
                    passed=item.status is RuleOutcomeStatus.PASSED,
                    affected_count=item.affected_count,
                    summary=item.summary,
                    report_relative_path=item.row_evidence_relative_path,
                )
                for item in outcome.results
                if item.status is not RuleOutcomeStatus.SKIPPED
            ),
            summary_report_relative_path=None,
            failure=None,
        )
        version = DatasetVersion.building(
            version_id=version_id,
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            schema_version=CALENDAR_SCHEMA_VERSION,
            code_version=CALENDAR_BOOTSTRAP_CODE_VERSION,
            rule_version=CALENDAR_BOOTSTRAP_RULE_VERSION,
            parent_version_id=None,
            input_batch_ids=(batch_id,),
            created_at=clock(),
        )
        with uow_factory() as uow:
            uow.data.save_version(version)
            uow.data.save_quality_run(run)
            uow.commit()
        coordinator = CanonicalPublishCoordinator(
            warehouse=CanonicalWarehouse(data_root=data_root),
            uow_factory=uow_factory,
        )
        published = coordinator.coordinate(
            CanonicalPublishCoordinatorRequest(
                version=version,
                tables=(table,),
                code_version=CALENDAR_BOOTSTRAP_CODE_VERSION,
                rule_version=CALENDAR_BOOTSTRAP_RULE_VERSION,
                quality_run_id=run.run_id,
                empty_result=None,
            )
        )
        ledger.record(
            authorization_id,
            status="COMPLETED",
            now_iso=clock().to_iso(),
            detail={"version_id": str(published.version.version_id)},
        )
        return {
            "status": "SUCCEEDED",
            "authorization_id": authorization_id,
            "version_id": str(published.version.version_id),
            "content_sha256": content_sha,
            "batch_id": str(batch_id),
            "raw_directory": raw_directory.relative_to(data_root).as_posix(),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "exchanges": list(CALENDAR_BOOTSTRAP_EXCHANGES),
            "endpoint_invocations": 1,
            "protocol_messages": result.protocol_messages,
            "login_messages": result.login_messages,
            "query_messages": result.query_messages,
            "logout_messages": result.logout_messages,
            "rows": table.num_rows,
            "open_days": sum(1 for _day, flag in result.days if flag == "1"),
        }
    except CalendarBootstrapError:
        raise
    except Exception as error:
        _record_failed(_failure_detail("CALENDAR_EXECUTION_FAILED", error))
        raise CalendarBootstrapError(
            "CALENDAR_EXECUTION_FAILED",
            "the authorized calendar bootstrap failed; evidence is preserved",
        ) from error


__all__ = [
    "CALENDAR_BOOTSTRAP_CODE_VERSION",
    "CALENDAR_BOOTSTRAP_DATASET",
    "CALENDAR_BOOTSTRAP_ENDPOINT_MAX",
    "CALENDAR_BOOTSTRAP_EXCHANGES",
    "CALENDAR_BOOTSTRAP_NETWORK_SCOPE",
    "CALENDAR_BOOTSTRAP_PLAN_SCHEMA_VERSION",
    "CALENDAR_BOOTSTRAP_PROTOCOL_MAX",
    "CALENDAR_BOOTSTRAP_RULE_VERSION",
    "CALENDAR_BOOTSTRAP_SOURCE",
    "CALENDAR_BOOTSTRAP_TIMEOUT_SECONDS",
    "CALENDAR_SCHEMA_VERSION",
    "CalendarBootstrapError",
    "CheckedCalendarBootstrapPlan",
    "build_calendar_bootstrap_plan",
    "build_exchange_calendar_table",
    "calendar_bootstrap_plan_sha256",
    "calendar_bootstrap_token",
    "execute_calendar_bootstrap",
    "parse_calendar_bootstrap_plan",
]
