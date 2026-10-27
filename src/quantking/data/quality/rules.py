"""Versioned single-source quality rule catalogue (S1-04A).

A rule is a framework-free dataclass that declares its ``code``, the
datasets it applies to, the reference data it requires, and a callable
``evaluate`` that takes the request, the candidate table, and the
reference tables, and returns a :class:`QualityOutcomeResult`.

Rules must NOT import SQLAlchemy, DuckDB, vendor SDKs or FastAPI. They
MAY import :mod:`pyarrow` because they live under ``data.quality`` which
is allowed to consume the canonical schema adapter. The executor is the
only component that calls ``evaluate``; rules are pure functions of
their inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Protocol, cast

import pyarrow as pa

from quantking.data.adapters.canonical_schema import (
    validate_table,
)
from quantking.data.models import DatasetKind
from quantking.data.quality.models import (
    DAILY_BAR_QUALITY_RULE_VERSION,
    S108_DAILY_RULE_VERSION,
    QualityOutcomeResult,
    QualityRequest,
    RuleOutcomeStatus,
)
from quantking.data.schemas import descriptor_for

RULE_VERSION = "quality-rules.v1"


# ---------------------------------------------------------------------------
# Rule protocol and dataclass
# ---------------------------------------------------------------------------


class RuleEvaluate(Protocol):
    """Callable signature for a rule evaluation."""

    def __call__(
        self,
        *,
        request: QualityRequest,
        table: pa.Table,
        reference_tables: Mapping[str, pa.Table],
    ) -> QualityOutcomeResult: ...


@dataclass(frozen=True, slots=True)
class QualityRule:
    """One versioned single-source quality rule."""

    code: str
    applies_to: frozenset[DatasetKind]
    requires_reference: frozenset[str]
    blocking: bool
    evaluate: RuleEvaluate


@dataclass(frozen=True, slots=True)
class QualityRuleSet:
    """A versioned set of quality rules."""

    rule_version: str
    rules: tuple[QualityRule, ...]

    def for_dataset(self, dataset: DatasetKind) -> tuple[QualityRule, ...]:
        """Return the rules that apply to ``dataset``, preserving catalogue order."""
        return tuple(rule for rule in self.rules if dataset in rule.applies_to)


# ---------------------------------------------------------------------------
# Helpers shared by rules
# ---------------------------------------------------------------------------


def _passed(rule_code: str, *, summary: str, affected: int = 0) -> QualityOutcomeResult:
    return QualityOutcomeResult(
        rule_code=rule_code,
        status=RuleOutcomeStatus.PASSED,
        affected_count=affected,
        summary=summary,
        row_evidence_relative_path=None,
    )


def _failed(rule_code: str, *, summary: str, affected: int) -> QualityOutcomeResult:
    return QualityOutcomeResult(
        rule_code=rule_code,
        status=RuleOutcomeStatus.FAILED,
        affected_count=affected,
        summary=summary,
        row_evidence_relative_path=None,
    )


def _skipped(rule_code: str, *, summary: str) -> QualityOutcomeResult:
    return QualityOutcomeResult(
        rule_code=rule_code,
        status=RuleOutcomeStatus.SKIPPED,
        affected_count=0,
        summary=summary,
        row_evidence_relative_path=None,
    )


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


def _evaluate_schema_conformance(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    result = validate_table(request.dataset.value, table, empty_result=request.empty_result)
    if result.ok:
        return _passed("schema_conformance", summary="schema, types and value invariants verified")
    return _failed(
        "schema_conformance",
        summary=f"{len(result.issues)} schema issue(s); first: {result.issues[0].rule}",
        affected=len(result.issues),
    )


def _evaluate_primary_key_uniqueness(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    descriptor = descriptor_for(request.dataset.value)
    pk = descriptor.primary_key
    columns = [table.column(name).to_pylist() for name in pk]
    seen: set[tuple[object, ...]] = set()
    duplicates = 0
    for index in range(table.num_rows):
        key = tuple(column[index] for column in columns)
        if key in seen:
            duplicates += 1
        seen.add(key)
    if duplicates == 0:
        return _passed("primary_key_uniqueness", summary=f"primary key {pk} is unique")
    return _failed(
        "primary_key_uniqueness",
        summary=f"{duplicates} duplicate primary key row(s) on {pk}",
        affected=duplicates,
    )


def _evaluate_expected_scope_presence(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    del reference_tables
    if table.num_rows == 0 and request.empty_result is not None:
        return _passed(
            "expected_scope_presence",
            summary="legal empty result carries no security rows",
        )
    if "code" not in table.column_names:
        return _failed(
            "expected_scope_presence",
            summary="candidate has no code column",
            affected=1,
        )
    actual = {str(item) for item in table.column("code").to_pylist() if item is not None}
    expected = set(request.expected_securities)
    mismatch = expected ^ actual
    if not mismatch:
        return _passed(
            "expected_scope_presence",
            summary="candidate security scope exactly matches the frozen request",
        )
    return _failed(
        "expected_scope_presence",
        summary="candidate security scope does not match the frozen request",
        affected=len(mismatch),
    )


def _evaluate_ohlc_consistency(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    if request.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED and (
        request.dataset is not DatasetKind.BENCHMARK_INDEX_DAILY
    ):
        return _skipped(
            "ohlc_consistency", summary="OHLC relationship rule does not apply to this dataset"
        )
    opens = table.column("open").to_pylist()
    highs = table.column("high").to_pylist()
    lows = table.column("low").to_pylist()
    closes = table.column("close").to_pylist()
    bad = 0
    for index in range(table.num_rows):
        o, h, low, c = opens[index], highs[index], lows[index], closes[index]
        if any(value is None for value in (o, h, low, c)):
            continue
        if h < max(o, c, low) or low > min(o, c, h):
            bad += 1
    if bad == 0:
        return _passed(
            "ohlc_consistency",
            summary="high >= max(open,close,low) and low <= min(open,close,high)",
        )
    return _failed(
        "ohlc_consistency",
        summary=f"{bad} row(s) violate high/low OHLC relationships",
        affected=bad,
    )


def _evaluate_positive_prices(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    if request.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
        return _skipped("positive_prices", summary="positive price rule does not apply")
    bad = 0
    for name in ("open", "high", "low", "close", "pre_close"):
        if name not in table.column_names:
            continue
        for value in table.column(name).to_pylist():
            if value is None:
                continue
            if not (isinstance(value, (int, float)) and value > 0):
                bad += 1
    if bad == 0:
        return _passed("positive_prices", summary="all OHLC/pre_close prices are positive")
    return _failed(
        "positive_prices",
        summary=f"{bad} non-positive price value(s)",
        affected=bad,
    )


def _evaluate_non_negative_volume_amount(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    # benchmark_index_daily has nullable volume/amount; the rule still
    # applies but accepts nulls. daily_bar_unadjusted has non-nullable
    # volume/amount that must be non-negative.
    bad = 0
    for name in ("volume", "amount"):
        if name not in table.column_names:
            continue
        for value in table.column(name).to_pylist():
            if value is None:
                # Nullable columns (benchmark_index_daily) accept nulls.
                continue
            if not (isinstance(value, (int, float)) and value >= 0):
                bad += 1
    if bad == 0:
        return _passed(
            "non_negative_volume_amount",
            summary="volume and amount are non-negative (nulls allowed where contract permits)",
        )
    return _failed(
        "non_negative_volume_amount",
        summary=f"{bad} negative volume/amount value(s)",
        affected=bad,
    )


def _evaluate_pre_close_consistency(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    """Require pre_close == prior row close per code; the first row per code is exempt.

    The first target row's pre_close derives from the 15-day lookback context
    outside the canonical table, so only subsequent rows can be verified.
    """
    del reference_tables
    if request.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
        return _skipped("pre_close_consistency", summary="rule applies to daily bars only")
    if table.num_rows == 0 and request.empty_result is not None:
        return _passed("pre_close_consistency", summary="legal empty result has no rows")
    rows = table.to_pylist()
    rows.sort(key=lambda row: (str(row["code"]), row["trade_date"]))
    previous: dict[str, float] = {}
    bad = 0
    for row in rows:
        code = str(row["code"])
        close = row["close"]
        pre_close = row["pre_close"]
        if code not in previous:
            previous[code] = float(close)
            continue
        if pre_close != previous[code]:
            bad += 1
        previous[code] = float(close)
    if bad == 0:
        return _passed(
            "pre_close_consistency",
            summary="pre_close equals the prior unadjusted close for every verified row",
        )
    return _failed(
        "pre_close_consistency",
        summary=f"{bad} row(s) whose pre_close differs from the prior unadjusted close",
        affected=bad,
    )


def _open_sessions(calendar_table: pa.Table, request: QualityRequest) -> dict[str, list[date]]:
    """Return per-exchange sorted open session dates inside the coverage window."""
    sessions: dict[str, list[date]] = {}
    for row in calendar_table.to_pylist():
        trade_date = row["trade_date"]
        if (
            row["is_open"]
            and request.coverage_start.value <= trade_date <= request.coverage_end.value
        ):
            sessions.setdefault(str(row["exchange"]), []).append(trade_date)
    return {exchange: sorted(dates) for exchange, dates in sessions.items()}


def _evaluate_calendar_coverage(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    """Require exactly one calendar record per expected exchange per natural day.

    A truncated calendar must block every calendar-bound rule: missing dates
    or missing exchanges are reference defects, never implicit holidays, and
    ``security_exceptions`` cannot mask them.
    """
    del table
    if request.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
        return _skipped("calendar_coverage", summary="rule applies to daily bars only")
    calendar_table = reference_tables.get("exchange_calendar")
    if calendar_table is None:
        return _failed(
            "calendar_coverage",
            summary="exchange_calendar reference table is missing",
            affected=1,
        )
    counts: dict[tuple[str, date], int] = {}
    for row in calendar_table.to_pylist():
        key = (str(row["exchange"]), row["trade_date"])
        counts[key] = counts.get(key, 0) + 1
    missing = 0
    day = request.coverage_start.value
    while day <= request.coverage_end.value:
        for exchange in request.expected_exchanges:
            if counts.get((exchange, day), 0) != 1:
                missing += 1
        day += timedelta(days=1)
    if missing == 0:
        return _passed(
            "calendar_coverage",
            summary=(
                "every expected exchange has exactly one calendar record "
                "for every natural day of the coverage window"
            ),
        )
    return _failed(
        "calendar_coverage",
        summary=f"{missing} calendar record(s) missing or duplicated in the coverage window",
        affected=missing,
    )


def _expected_sessions(
    request: QualityRequest, sessions: Mapping[str, list[date]], code: str
) -> list[date]:
    exchange = code.rsplit(".", 1)[-1]
    expected = list(sessions.get(exchange, ()))
    exceptions = (request.security_exceptions or {}).get(code, ())
    for start, end in exceptions:
        expected = [
            trade_date for trade_date in expected if not start.value <= trade_date <= end.value
        ]
    return expected


def _evaluate_expected_trading_days(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    """Per security: every expected open session must be present, nothing extra.

    Explicit listing/suspension exceptions bound in the frozen request shrink
    the expected session set; anything unexplained is a failure, never a guess.
    A legal empty result only passes when the calendar proves every security
    has no expected session — a fake ``empty_result`` cannot bypass the
    calendar.
    """
    if request.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
        return _skipped("expected_trading_days", summary="rule applies to daily bars only")
    calendar_table = reference_tables.get("exchange_calendar")
    if calendar_table is None:
        return _failed(
            "expected_trading_days",
            summary="exchange_calendar reference table is missing",
            affected=1,
        )
    sessions = _open_sessions(calendar_table, request)
    present: dict[str, set[date]] = {}
    unexpected = 0
    for row in table.to_pylist():
        code = str(row["code"])
        present.setdefault(code, set()).add(row["trade_date"])
        expected = set(_expected_sessions(request, sessions, code))
        if row["trade_date"] not in expected:
            unexpected += 1
    missing = 0
    for code in request.expected_securities:
        expected = set(_expected_sessions(request, sessions, code))
        missing += len(expected - present.get(code, set()))
    if unexpected == 0 and missing == 0:
        return _passed(
            "expected_trading_days",
            summary="every expected open session is present and nothing unexpected exists",
        )
    return _failed(
        "expected_trading_days",
        summary=f"{missing} missing and {unexpected} unexpected session row(s)",
        affected=missing + unexpected,
    )


def _evaluate_freshness(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    """Per security: the latest present session equals the latest expected session.

    A legal zero-row outcome only passes when every security has no expected
    session in the calendar; an empty table while sessions are expected is a
    freshness failure, never a free pass.
    """
    if request.dataset is not DatasetKind.DAILY_BAR_UNADJUSTED:
        return _skipped("freshness", summary="rule applies to daily bars only")
    calendar_table = reference_tables.get("exchange_calendar")
    if calendar_table is None:
        return _failed(
            "freshness",
            summary="exchange_calendar reference table is missing",
            affected=1,
        )
    sessions = _open_sessions(calendar_table, request)
    present: dict[str, set[date]] = {}
    for row in table.to_pylist():
        present.setdefault(str(row["code"]), set()).add(row["trade_date"])
    stale = 0
    for code in request.expected_securities:
        expected = _expected_sessions(request, sessions, code)
        if not expected:
            continue
        latest_expected = expected[-1]
        if latest_expected not in present.get(code, set()):
            stale += 1
    if stale == 0:
        return _passed("freshness", summary="every security is fresh through its latest session")
    return _failed(
        "freshness",
        summary=f"{stale} security(ies) not fresh through their latest expected session",
        affected=stale,
    )


def _evaluate_calendar_membership(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    calendar_table = reference_tables.get("exchange_calendar")
    if calendar_table is None:
        return _failed(
            "calendar_membership",
            summary="exchange_calendar reference table is missing",
            affected=1,
        )
    # Build the set of (exchange, trade_date) where is_open=True.
    exchanges = calendar_table.column("exchange").to_pylist()
    dates = calendar_table.column("trade_date").to_pylist()
    is_open = calendar_table.column("is_open").to_pylist()
    open_sessions: set[tuple[str, date]] = set()
    for exchange, trade_date, flag in zip(exchanges, dates, is_open, strict=True):
        if flag:
            open_sessions.add((str(exchange), trade_date))
    descriptor = descriptor_for(request.dataset.value)
    if "trade_date" not in descriptor.field_names or "code" not in descriptor.field_names:
        return _passed(
            "calendar_membership",
            summary="dataset has no trade_date/code columns to verify",
        )
    codes = table.column("code").to_pylist()
    trade_dates = table.column("trade_date").to_pylist()
    bad = 0
    for code, trade_date in zip(codes, trade_dates, strict=True):
        # Derive the exchange from the code suffix (000001.SZ -> SZ).
        suffix = str(code).rsplit(".", 1)[-1]
        if (suffix, trade_date) not in open_sessions:
            bad += 1
    if bad == 0:
        return _passed(
            "calendar_membership",
            summary="every trade_date is an open session for the code's exchange",
        )
    return _failed(
        "calendar_membership",
        summary=f"{bad} row(s) on a non-trading session for the code's exchange",
        affected=bad,
    )


def _evaluate_coverage_window(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    descriptor = descriptor_for(request.dataset.value)
    if "trade_date" not in descriptor.field_names:
        return _passed(
            "coverage_window",
            summary="dataset has no trade_date column; coverage window trivially matches",
        )
    trade_dates = sorted(
        {value for value in table.column("trade_date").to_pylist() if value is not None}
    )
    if not trade_dates:
        # Empty table: legal only when empty_result is supplied; schema
        # rule already covers the missing-classification case.
        if request.empty_result is not None:
            return _passed(
                "coverage_window",
                summary="legal empty result; coverage window is empty by classification",
            )
        return _failed(
            "coverage_window",
            summary="empty table without legal empty classification",
            affected=1,
        )
    first = trade_dates[0]
    last = trade_dates[-1]
    if first < request.coverage_start.value or last > request.coverage_end.value:
        return _failed(
            "coverage_window",
            summary=(
                f"trade dates [{first}, {last}] fall outside requested window "
                f"[{request.coverage_start}, {request.coverage_end}]"
            ),
            affected=1,
        )
    return _passed(
        "coverage_window",
        summary=(
            f"trade dates [{first}, {last}] within requested window "
            f"[{request.coverage_start}, {request.coverage_end}]"
        ),
    )


def _evaluate_coverage_by_security_exchange(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    descriptor = descriptor_for(request.dataset.value)
    if "trade_date" not in descriptor.field_names or "code" not in descriptor.field_names:
        return _passed(
            "coverage_by_security_exchange",
            summary="dataset has no trade_date/code columns to verify",
        )
    calendar_table = reference_tables.get("exchange_calendar")
    if calendar_table is None:
        return _failed(
            "coverage_by_security_exchange",
            summary="exchange_calendar reference table is missing",
            affected=1,
        )
    open_sessions: set[tuple[str, date]] = set()
    for row in calendar_table.to_pylist():
        trade_date = row["trade_date"]
        if (
            row["is_open"]
            and request.coverage_start.value <= trade_date <= request.coverage_end.value
        ):
            open_sessions.add((str(row["exchange"]), trade_date))
    present = {
        (str(row["code"]), row["trade_date"])
        for row in table.to_pylist()
        if row.get("code") is not None and row.get("trade_date") is not None
    }
    expected_exchanges = set(request.expected_exchanges)
    missing: list[str] = []
    for code in request.expected_securities:
        exchange = code.rsplit(".", 1)[-1]
        if exchange not in expected_exchanges:
            missing.append(f"{code}:unexpected-exchange:{exchange}")
            continue
        for session_exchange, trade_date in sorted(
            open_sessions, key=lambda item: (item[0], item[1])
        ):
            if session_exchange == exchange and (code, trade_date) not in present:
                missing.append(f"{code}:{exchange}:{trade_date.isoformat()}")
    if missing:
        sample = ",".join(missing[:10])
        return _failed(
            "coverage_by_security_exchange",
            summary=f"{len(missing)} missing open-session row(s): {sample}",
            affected=len(missing),
        )
    return _passed(
        "coverage_by_security_exchange",
        summary="every expected security has rows for every exchange open session",
    )


def _evaluate_listing_effective_range(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    # Validate point-in-time listing chronology and non-overlapping effective ranges.
    if request.dataset is not DatasetKind.SECURITY_MASTER:
        return _skipped(
            "listing_effective_range",
            summary="listing effective range rule applies only to security_master",
        )
    bad = 0
    rows = table.to_pylist()
    by_code: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        listing_date = row["listing_date"]
        delisting_date = row["delisting_date"]
        effective_from = row["effective_from"]
        effective_to = row["effective_to"]
        status = row["listing_status"]
        if listing_date is None or effective_from is None:
            bad += 1
            continue
        if delisting_date is not None and delisting_date < listing_date:
            bad += 1
        if effective_to is not None and effective_to < effective_from:
            bad += 1
        if effective_from < listing_date:
            bad += 1
        if status == "DELISTED" and delisting_date is None:
            bad += 1
        if status != "DELISTED" and delisting_date is not None and effective_from > delisting_date:
            bad += 1
        by_code.setdefault(str(row["code"]), []).append(row)
    for records in by_code.values():
        records.sort(key=lambda row: row["effective_from"])  # type: ignore[arg-type,return-value]
        for previous, current in pairwise(records):
            previous_end = previous["effective_to"]
            if previous_end is None or cast(date, current["effective_from"]) <= cast(
                date, previous_end
            ):
                bad += 1
    if bad == 0:
        return _passed(
            "listing_effective_range",
            summary="every security has a valid listing effective range",
        )
    return _failed(
        "listing_effective_range",
        summary=f"{bad} security(ies) with invalid listing effective range",
        affected=bad,
    )


def _evaluate_positive_adjustment_factor(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    if request.dataset is not DatasetKind.ADJUSTMENT_FACTOR:
        return _skipped(
            "positive_adjustment_factor",
            summary="adjustment factor rule applies only to adjustment_factor",
        )
    bad = 0
    for field_name in ("price_multiplier", "volume_multiplier"):
        for value in table.column(field_name).to_pylist():
            if not (isinstance(value, (int, float)) and value > 0):
                bad += 1
    if bad == 0:
        return _passed("positive_adjustment_factor", summary="all adjustment factors are positive")
    return _failed(
        "positive_adjustment_factor",
        summary=f"{bad} non-positive adjustment factor value(s)",
        affected=bad,
    )


# ---------------------------------------------------------------------------
# Rule catalogue
# ---------------------------------------------------------------------------


_DAILY_BAR_DATASETS = frozenset(
    {DatasetKind.DAILY_BAR_UNADJUSTED, DatasetKind.BENCHMARK_INDEX_DAILY}
)
_PRICE_DATASETS = frozenset({DatasetKind.DAILY_BAR_UNADJUSTED})
_VOLUME_DATASETS = frozenset({DatasetKind.DAILY_BAR_UNADJUSTED, DatasetKind.BENCHMARK_INDEX_DAILY})
_CALENDAR_DATASETS = frozenset(
    {
        DatasetKind.DAILY_BAR_UNADJUSTED,
        DatasetKind.ADJUSTMENT_FACTOR,
        DatasetKind.TRADING_STATUS,
        DatasetKind.BENCHMARK_INDEX_DAILY,
    }
)
_ALL_DATASETS = frozenset(
    {
        DatasetKind.SECURITY_MASTER,
        DatasetKind.EXCHANGE_CALENDAR,
        DatasetKind.DAILY_BAR_UNADJUSTED,
        DatasetKind.ADJUSTMENT_FACTOR,
        DatasetKind.TRADING_STATUS,
        DatasetKind.BENCHMARK_INDEX_DAILY,
    }
)
_SECURITY_MASTER_ONLY = frozenset({DatasetKind.SECURITY_MASTER})
_ADJUSTMENT_FACTOR_ONLY = frozenset({DatasetKind.ADJUSTMENT_FACTOR})


def default_rule_set() -> QualityRuleSet:
    """Return the canonical Stage 1 single-source quality rule set."""
    rules: tuple[QualityRule, ...] = (
        QualityRule(
            code="schema_conformance",
            applies_to=_ALL_DATASETS,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_schema_conformance,
        ),
        QualityRule(
            code="primary_key_uniqueness",
            applies_to=_ALL_DATASETS,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_primary_key_uniqueness,
        ),
        QualityRule(
            code="ohlc_consistency",
            applies_to=_DAILY_BAR_DATASETS,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_ohlc_consistency,
        ),
        QualityRule(
            code="positive_prices",
            applies_to=_PRICE_DATASETS,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_positive_prices,
        ),
        QualityRule(
            code="non_negative_volume_amount",
            applies_to=_VOLUME_DATASETS,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_non_negative_volume_amount,
        ),
        QualityRule(
            code="calendar_membership",
            applies_to=_CALENDAR_DATASETS,
            requires_reference=frozenset({"exchange_calendar"}),
            blocking=True,
            evaluate=_evaluate_calendar_membership,
        ),
        QualityRule(
            code="coverage_window",
            applies_to=_CALENDAR_DATASETS,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_coverage_window,
        ),
        QualityRule(
            code="coverage_by_security_exchange",
            applies_to=_CALENDAR_DATASETS,
            requires_reference=frozenset({"exchange_calendar"}),
            blocking=True,
            evaluate=_evaluate_coverage_by_security_exchange,
        ),
        QualityRule(
            code="listing_effective_range",
            applies_to=_SECURITY_MASTER_ONLY,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_listing_effective_range,
        ),
        QualityRule(
            code="positive_adjustment_factor",
            applies_to=_ADJUSTMENT_FACTOR_ONLY,
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_positive_adjustment_factor,
        ),
    )
    return QualityRuleSet(rule_version=RULE_VERSION, rules=rules)


def s108_daily_rule_set() -> QualityRuleSet:
    """Return the first-release daily rules without an unavailable calendar dependency."""
    selected = {
        "schema_conformance",
        "primary_key_uniqueness",
        "ohlc_consistency",
        "positive_prices",
        "non_negative_volume_amount",
        "coverage_window",
    }
    rules = tuple(rule for rule in default_rule_set().rules if rule.code in selected)
    if {rule.code for rule in rules} != selected:
        raise RuntimeError("S1-08 daily quality catalogue is incomplete")
    scope_rule = QualityRule(
        code="expected_scope_presence",
        applies_to=frozenset({DatasetKind.DAILY_BAR_UNADJUSTED}),
        requires_reference=frozenset(),
        blocking=True,
        evaluate=_evaluate_expected_scope_presence,
    )
    return QualityRuleSet(
        rule_version=S108_DAILY_RULE_VERSION,
        rules=(rules[0], rules[1], scope_rule, *rules[2:]),
    )


_DAILY_BAR_SELECTED_RULES = frozenset(
    {
        "schema_conformance",
        "primary_key_uniqueness",
        "ohlc_consistency",
        "positive_prices",
        "non_negative_volume_amount",
        "calendar_membership",
        "coverage_window",
    }
)


def daily_bar_rule_set() -> QualityRuleSet:
    """Return production daily-bar rules with calendar-bound checks.

    The exchange_calendar reference is bound by version and content SHA in the
    frozen request; expected trading days, freshness and pre_close consistency
    are blocking, with explicit listing/suspension exceptions bound in
    ``QualityRequest.security_exceptions``.
    """
    rules = tuple(
        rule for rule in default_rule_set().rules if rule.code in _DAILY_BAR_SELECTED_RULES
    )
    if {rule.code for rule in rules} != _DAILY_BAR_SELECTED_RULES:
        raise RuntimeError("daily-bar quality catalogue is incomplete")
    scope_rule = QualityRule(
        code="expected_scope_presence",
        applies_to=frozenset({DatasetKind.DAILY_BAR_UNADJUSTED}),
        requires_reference=frozenset(),
        blocking=True,
        evaluate=_evaluate_expected_scope_presence,
    )
    extra_rules = (
        QualityRule(
            code="pre_close_consistency",
            applies_to=frozenset({DatasetKind.DAILY_BAR_UNADJUSTED}),
            requires_reference=frozenset(),
            blocking=True,
            evaluate=_evaluate_pre_close_consistency,
        ),
        QualityRule(
            code="calendar_coverage",
            applies_to=frozenset({DatasetKind.DAILY_BAR_UNADJUSTED}),
            requires_reference=frozenset({"exchange_calendar"}),
            blocking=True,
            evaluate=_evaluate_calendar_coverage,
        ),
        QualityRule(
            code="expected_trading_days",
            applies_to=frozenset({DatasetKind.DAILY_BAR_UNADJUSTED}),
            requires_reference=frozenset({"exchange_calendar"}),
            blocking=True,
            evaluate=_evaluate_expected_trading_days,
        ),
        QualityRule(
            code="freshness",
            applies_to=frozenset({DatasetKind.DAILY_BAR_UNADJUSTED}),
            requires_reference=frozenset({"exchange_calendar"}),
            blocking=True,
            evaluate=_evaluate_freshness,
        ),
    )
    return QualityRuleSet(
        rule_version=DAILY_BAR_QUALITY_RULE_VERSION,
        rules=(rules[0], rules[1], scope_rule, *rules[2:], *extra_rules),
    )


__all__: tuple[str, ...] = (
    "RULE_VERSION",
    "QualityRule",
    "QualityRuleSet",
    "daily_bar_rule_set",
    "default_rule_set",
    "s108_daily_rule_set",
)
