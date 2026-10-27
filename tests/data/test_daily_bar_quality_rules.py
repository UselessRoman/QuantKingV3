from __future__ import annotations

from datetime import date

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.content import canonical_content_sha256
from quantking.data.models import DatasetKind, EmptyResultInfo, LegalEmptyClassification
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import (
    K4_DAILY_RULE_VERSION,
    QualityRequest,
    ReferenceDataBinding,
    RuleOutcomeStatus,
)
from quantking.data.quality.rules import daily_bar_rule_set

_CALENDAR_VERSION = new_entity_id("data_version")
_LEGAL_EMPTY = EmptyResultInfo(
    classification=LegalEmptyClassification.NO_TRADING_SESSION,
    reason="synthetic legal empty",
)


def _calendar_table(
    open_sessions: set[date] | None = None,
    *,
    exchanges: tuple[str, ...] = ("SZ",),
    skip: set[tuple[str, date]] | None = None,
) -> pa.Table:
    rows: list[dict[str, object]] = []
    for exchange in exchanges:
        for trade_date in (
            date(2026, 8, 5),
            date(2026, 8, 6),
            date(2026, 8, 7),
            date(2026, 8, 8),
        ):
            if (exchange, trade_date) in (skip or set()):
                continue
            is_open = (
                trade_date in open_sessions
                if open_sessions is not None
                else trade_date != date(2026, 8, 8)
            )
            rows.append({"exchange": exchange, "trade_date": trade_date, "is_open": is_open})
    return ensure_sorted(
        DatasetKind.EXCHANGE_CALENDAR.value,
        pa.Table.from_pylist(
            rows,
            schema=canonical_schema(DatasetKind.EXCHANGE_CALENDAR.value),
        ),
    )


def _calendar_sha(
    open_sessions: set[date] | None = None,
    *,
    skip: set[tuple[str, date]] | None = None,
) -> str:
    return canonical_content_sha256(
        DatasetKind.EXCHANGE_CALENDAR,
        [_calendar_table(open_sessions=open_sessions, skip=skip)],
    )


def _daily(rows: list[dict[str, object]]) -> pa.Table:
    return ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(
            rows,
            schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )


def _row(day: date, close: float, *, pre_close: float | None = None) -> dict[str, object]:
    return {
        "code": "000001.SZ",
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close if pre_close is None else pre_close,
        "volume": 100,
        "amount": 1_000.0,
    }


def _request(
    table: pa.Table,
    *,
    exceptions: dict[str, tuple[tuple[TradingDate, TradingDate], ...]] | None = None,
    content_sha: str | None = None,
    securities: tuple[str, ...] = ("000001.SZ",),
    exchanges: tuple[str, ...] = ("SZ",),
    open_sessions: set[date] | None = None,
    empty_result: EmptyResultInfo | None = None,
    skip: set[tuple[str, date]] | None = None,
) -> QualityRequest:
    return QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version=K4_DAILY_RULE_VERSION,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-07k4.v1",
        content_sha256=content_sha
        or canonical_content_sha256(
            DatasetKind.DAILY_BAR_UNADJUSTED, [table], empty_result=empty_result
        ),
        input_batch_ids=(new_entity_id("data_batch"),),
        expected_securities=securities,
        expected_exchanges=exchanges,
        coverage_start=TradingDate(date(2026, 8, 5)),
        coverage_end=TradingDate(date(2026, 8, 7)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=_CALENDAR_VERSION,
                content_sha256=_calendar_sha(open_sessions=open_sessions, skip=skip),
            )
        },
        empty_result=empty_result,
        security_exceptions=exceptions,
    )


def _execute(
    table: pa.Table,
    *,
    exceptions=None,
    securities: tuple[str, ...] = ("000001.SZ",),
    exchanges: tuple[str, ...] = ("SZ",),
    open_sessions: set[date] | None = None,
    empty_result: EmptyResultInfo | None = None,
    skip: set[tuple[str, date]] | None = None,
) -> dict[str, RuleOutcomeStatus]:
    artifact = QualityEngine(rule_set=daily_bar_rule_set()).execute(
        request=_request(
            table,
            exceptions=exceptions,
            securities=securities,
            exchanges=exchanges,
            open_sessions=open_sessions,
            empty_result=empty_result,
            skip=skip,
        ),
        table=table,
        reference_tables={
            "exchange_calendar": _calendar_table(open_sessions=open_sessions, skip=skip)
        },
    )
    return {result.rule_code: result.status for result in artifact.results}


def _statuses(artifact_statuses: dict[str, RuleOutcomeStatus]) -> dict[str, bool]:
    return {code: status is RuleOutcomeStatus.PASSED for code, status in artifact_statuses.items()}


def test_k4_rule_set_covers_the_full_calendar_bound_catalogue() -> None:
    codes = {rule.code for rule in daily_bar_rule_set().rules}
    assert {
        "schema_conformance",
        "primary_key_uniqueness",
        "expected_scope_presence",
        "ohlc_consistency",
        "positive_prices",
        "non_negative_volume_amount",
        "calendar_membership",
        "coverage_window",
        "pre_close_consistency",
        "calendar_coverage",
        "expected_trading_days",
        "freshness",
    } == codes
    assert daily_bar_rule_set().rule_version == K4_DAILY_RULE_VERSION


def test_complete_fresh_series_passes_every_k4_rule() -> None:
    table = _daily(
        [
            _row(date(2026, 8, 5), 10.0, pre_close=9.9),
            _row(date(2026, 8, 6), 10.2, pre_close=10.0),
            _row(date(2026, 8, 7), 10.1, pre_close=10.2),
        ]
    )
    statuses = _statuses(_execute(table))
    assert statuses == {code: True for code in statuses}


def test_pre_close_mismatch_fails_pre_close_consistency_only() -> None:
    table = _daily(
        [
            _row(date(2026, 8, 5), 10.0, pre_close=9.9),
            _row(date(2026, 8, 6), 10.2, pre_close=10.0),
            _row(date(2026, 8, 7), 10.1, pre_close=10.5),  # violates prior close 10.2
        ]
    )
    statuses = _statuses(_execute(table))
    assert statuses["pre_close_consistency"] is False
    assert statuses["expected_trading_days"] is True
    assert statuses["freshness"] is True


def test_missing_session_fails_expected_trading_days_and_freshness() -> None:
    table = _daily(
        [
            _row(date(2026, 8, 5), 10.0, pre_close=9.9),
            _row(date(2026, 8, 6), 10.2, pre_close=10.0),
        ]
    )
    statuses = _statuses(_execute(table))
    assert statuses["expected_trading_days"] is False
    assert statuses["freshness"] is False
    assert statuses["pre_close_consistency"] is True


def test_row_on_closed_session_fails_calendar_membership() -> None:
    table = _daily(
        [
            _row(date(2026, 8, 5), 10.0, pre_close=9.9),
            _row(date(2026, 8, 6), 10.2, pre_close=10.0),
            _row(date(2026, 8, 7), 10.1, pre_close=10.2),
            _row(date(2026, 8, 8), 10.3, pre_close=10.1),  # 08-08 is closed
        ]
    )
    statuses = _statuses(_execute(table))
    assert statuses["calendar_membership"] is False
    assert statuses["expected_trading_days"] is False


def test_frozen_suspension_exception_restores_gap_and_freshness() -> None:
    table = _daily(
        [
            _row(date(2026, 8, 5), 10.0, pre_close=9.9),
            _row(date(2026, 8, 6), 10.2, pre_close=10.0),
        ]
    )
    statuses = _statuses(
        _execute(
            table,
            exceptions={
                "000001.SZ": ((TradingDate(date(2026, 8, 7)), TradingDate(date(2026, 8, 7))),)
            },
        )
    )
    assert statuses["expected_trading_days"] is True
    assert statuses["freshness"] is True


def test_k4_request_requires_bound_exchange_calendar_reference() -> None:
    table = _daily([_row(date(2026, 8, 5), 10.0, pre_close=9.9)])
    with pytest.raises(ValueError, match="exchange_calendar"):
        QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=new_entity_id("data_version"),
            rule_version=K4_DAILY_RULE_VERSION,
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-07k4.v1",
            content_sha256=canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [table]),
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 8, 5)),
            coverage_end=TradingDate(date(2026, 8, 7)),
            reference_data={},
            empty_result=None,
        )


def test_exceptions_are_bound_to_the_k4_rule_version_only() -> None:
    table = _daily([_row(date(2026, 8, 5), 10.0, pre_close=9.9)])
    from quantking.data.quality.models import S108_DAILY_RULE_VERSION

    with pytest.raises(ValueError, match="security_exceptions"):
        QualityRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            version_id=new_entity_id("data_version"),
            rule_version=S108_DAILY_RULE_VERSION,
            schema_version="daily_bar_unadjusted.v1",
            code_version="s1-07k4.v1",
            content_sha256=canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [table]),
            input_batch_ids=(new_entity_id("data_batch"),),
            expected_securities=("000001.SZ",),
            expected_exchanges=("SZ",),
            coverage_start=TradingDate(date(2026, 8, 5)),
            coverage_end=TradingDate(date(2026, 8, 7)),
            reference_data={},
            empty_result=None,
            security_exceptions={
                "000001.SZ": ((TradingDate(date(2026, 8, 7)), TradingDate(date(2026, 8, 7))),)
            },
        )


# ---------------------------------------------------------------------------
# KB-01: a fake empty_result can never bypass the calendar.
# ---------------------------------------------------------------------------


def test_all_closed_calendar_empty_table_with_legal_classification_passes() -> None:
    table = _daily([])
    statuses = _statuses(
        _execute(
            table,
            open_sessions=set(),
            empty_result=_LEGAL_EMPTY,
        )
    )
    assert statuses["expected_trading_days"] is True
    assert statuses["freshness"] is True
    assert statuses["calendar_membership"] is True


def test_open_session_with_empty_table_and_fake_empty_result_fails() -> None:
    table = _daily([])
    statuses = _statuses(
        _execute(
            table,
            open_sessions={date(2026, 8, 6)},
            empty_result=_LEGAL_EMPTY,
        )
    )
    assert statuses["expected_trading_days"] is False
    assert statuses["freshness"] is False


def test_two_securities_where_one_has_expected_sessions_fail_on_empty() -> None:
    # SZ has no open sessions; SH has one. An empty table is only legal when
    # EVERY security has zero expected sessions.
    calendar = _calendar_table(
        open_sessions={date(2026, 8, 6)},
        exchanges=("SZ", "SH"),
    )
    content_sha = canonical_content_sha256(DatasetKind.EXCHANGE_CALENDAR, [calendar])
    from quantking.data.content import canonical_content_sha256 as _daily_sha

    table = _daily([])
    request = QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version=K4_DAILY_RULE_VERSION,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-07k4.v1",
        content_sha256=_daily_sha(
            DatasetKind.DAILY_BAR_UNADJUSTED, [table], empty_result=_LEGAL_EMPTY
        ),
        input_batch_ids=(new_entity_id("data_batch"),),
        expected_securities=("000001.SZ", "600000.SH"),
        expected_exchanges=("SZ", "SH"),
        coverage_start=TradingDate(date(2026, 8, 5)),
        coverage_end=TradingDate(date(2026, 8, 7)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=_CALENDAR_VERSION,
                content_sha256=content_sha,
            )
        },
        empty_result=_LEGAL_EMPTY,
        security_exceptions=None,
    )
    artifact = QualityEngine(rule_set=daily_bar_rule_set()).execute(
        request=request,
        table=table,
        reference_tables={"exchange_calendar": calendar},
    )
    statuses = _statuses({result.rule_code: result.status for result in artifact.results})
    assert statuses["expected_trading_days"] is False
    assert statuses["freshness"] is False


def test_exception_covering_every_expected_session_allows_legal_empty() -> None:
    table = _daily([])
    statuses = _statuses(
        _execute(
            table,
            open_sessions={date(2026, 8, 6)},
            empty_result=_LEGAL_EMPTY,
            exceptions={
                "000001.SZ": ((TradingDate(date(2026, 8, 6)), TradingDate(date(2026, 8, 6))),)
            },
        )
    )
    assert statuses["expected_trading_days"] is True
    assert statuses["freshness"] is True


def test_exception_covering_only_part_of_sessions_still_fails() -> None:
    table = _daily([])
    statuses = _statuses(
        _execute(
            table,
            open_sessions={date(2026, 8, 5), date(2026, 8, 6)},
            empty_result=_LEGAL_EMPTY,
            exceptions={
                "000001.SZ": ((TradingDate(date(2026, 8, 5)), TradingDate(date(2026, 8, 5))),)
            },
        )
    )
    assert statuses["expected_trading_days"] is False
    assert statuses["freshness"] is False


# ---------------------------------------------------------------------------
# Calendar coverage completeness: truncated calendars must block every
# calendar-bound rule; exceptions cannot mask the gap.
# ---------------------------------------------------------------------------


def test_truncated_calendar_blocks_legal_empty_quality() -> None:
    table = _daily([])
    statuses = _statuses(
        _execute(
            table,
            open_sessions=set(),
            empty_result=_LEGAL_EMPTY,
            skip={("SZ", date(2026, 8, 6))},
        )
    )
    assert statuses["calendar_coverage"] is False
    # The fake empty claim must not pass the session rules either: the gap
    # blocks the whole run regardless of the other rules' outcomes.
    artifact = QualityEngine(rule_set=daily_bar_rule_set()).execute(
        request=_request(
            table,
            open_sessions=set(),
            empty_result=_LEGAL_EMPTY,
            skip={("SZ", date(2026, 8, 6))},
        ),
        table=table,
        reference_tables={
            "exchange_calendar": _calendar_table(
                open_sessions=set(), skip={("SZ", date(2026, 8, 6))}
            )
        },
    )
    assert artifact.status.value == "FAILED"


def test_truncated_calendar_blocks_even_with_rows() -> None:
    table = _daily([_row(date(2026, 8, 5), 10.0, pre_close=9.9)])
    statuses = _statuses(
        _execute(
            table,
            open_sessions={date(2026, 8, 5)},
            skip={("SZ", date(2026, 8, 6))},
        )
    )
    assert statuses["calendar_coverage"] is False


def test_security_exceptions_cannot_mask_calendar_gaps() -> None:
    table = _daily([])
    statuses = _statuses(
        _execute(
            table,
            open_sessions=set(),
            empty_result=_LEGAL_EMPTY,
            skip={("SZ", date(2026, 8, 6))},
            exceptions={
                "000001.SZ": ((TradingDate(date(2026, 8, 6)), TradingDate(date(2026, 8, 6))),)
            },
        )
    )
    assert statuses["calendar_coverage"] is False


def test_complete_all_closed_calendar_passes_coverage_and_empties() -> None:
    table = _daily([])
    statuses = _statuses(
        _execute(
            table,
            open_sessions=set(),
            empty_result=_LEGAL_EMPTY,
        )
    )
    assert statuses["calendar_coverage"] is True
    assert statuses["expected_trading_days"] is True
    assert statuses["freshness"] is True
