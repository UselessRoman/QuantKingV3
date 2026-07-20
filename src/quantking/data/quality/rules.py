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
from datetime import date
from typing import Protocol

import pyarrow as pa

from quantking.data.adapters.canonical_schema import (
    validate_table,
)
from quantking.data.models import DatasetKind
from quantking.data.quality.models import (
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


def _evaluate_listing_effective_range(
    *,
    request: QualityRequest,
    table: pa.Table,
    reference_tables: Mapping[str, pa.Table],
) -> QualityOutcomeResult:
    # security_master: list_date <= delist_date (if both present); code/exchange consistent.
    if request.dataset is not DatasetKind.SECURITY_MASTER:
        return _skipped(
            "listing_effective_range",
            summary="listing effective range rule applies only to security_master",
        )
    bad = 0
    list_dates = table.column("list_date").to_pylist() if "list_date" in table.column_names else []
    delist_dates = (
        table.column("delist_date").to_pylist() if "delist_date" in table.column_names else []
    )
    for list_date, delist_date in zip(list_dates, delist_dates, strict=True):
        if list_date is None:
            bad += 1
            continue
        if delist_date is not None and delist_date < list_date:
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
    if "adjustment_factor" not in table.column_names:
        return _failed(
            "positive_adjustment_factor",
            summary="adjustment_factor column is missing",
            affected=1,
        )
    bad = 0
    for value in table.column("adjustment_factor").to_pylist():
        if value is None:
            continue
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


__all__: tuple[str, ...] = (
    "RULE_VERSION",
    "QualityRule",
    "QualityRuleSet",
    "default_rule_set",
)
