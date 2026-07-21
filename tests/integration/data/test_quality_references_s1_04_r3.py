"""S1-04-R3 red tests for verified reference version loading and coverage gaps."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.models import DatasetKind, DatasetVersion
from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import QualityRequest, ReferenceDataBinding
from quantking.data.quality.rules import default_rule_set
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _canonical(dataset: str, rows: list[dict[str, object]]) -> pa.Table:
    raw = pa.Table.from_pylist(rows)
    schema = canonical_schema(dataset)
    return pa.Table.from_arrays(
        [raw.column(field.name).cast(field.type, safe=False) for field in schema],
        schema=pa.schema(
            [pa.field(field.name, field.type, nullable=field.nullable) for field in schema]
        ),
    )


def _daily(rows: list[tuple[str, date]]) -> pa.Table:
    return _canonical(
        "daily_bar_unadjusted",
        [
            {
                "code": code,
                "trade_date": trade_date,
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
            for code, trade_date in rows
        ],
    )


def _calendar() -> pa.Table:
    return _canonical(
        "exchange_calendar",
        [
            {"exchange": "SZ", "trade_date": date(2026, 7, 15), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 16), "is_open": True},
            {"exchange": "SZ", "trade_date": date(2026, 7, 17), "is_open": False},
            {"exchange": "SH", "trade_date": date(2026, 7, 15), "is_open": True},
            {"exchange": "SH", "trade_date": date(2026, 7, 16), "is_open": True},
            {"exchange": "SH", "trade_date": date(2026, 7, 17), "is_open": False},
        ],
    )


@pytest.fixture()
def uow_factory(tmp_path: Path) -> DataSqliteUnitOfWorkFactory:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    yield DataSqliteUnitOfWorkFactory(engine)
    engine.dispose()


def test_published_reference_loader_rejects_non_published_reference_version(
    tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
) -> None:
    from quantking.data.quality.references import (
        PublishedReferenceLoader,
        ReferenceDataBindingError,
    )

    version = DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        schema_version="exchange_calendar.v1",
        code_version="s1-04",
        parent_version_id=None,
        input_batch_ids=(),
        created_at=utc_now(),
    )
    with uow_factory() as uow:
        uow.data.save_version(version)
        uow.commit()

    loader = PublishedReferenceLoader(data_root=tmp_path, uow_factory=uow_factory)
    binding = ReferenceDataBinding(
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        version_id=version.version_id,
        content_sha256="a" * 64,
    )

    with pytest.raises(ReferenceDataBindingError, match="PUBLISHED"):
        loader.load("exchange_calendar", binding)


def test_quality_engine_fails_missing_open_session_by_security_exchange() -> None:
    candidate = _daily(
        [
            ("000001.SZ", date(2026, 7, 15)),
            ("000001.SZ", date(2026, 7, 16)),
            ("600000.SH", date(2026, 7, 15)),
        ]
    )
    request = QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ", "600000.SH"),
        expected_exchanges=("SZ", "SH"),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 17)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="a" * 64,
            )
        },
        empty_result=None,
    )

    outcome = QualityEngine(rule_set=default_rule_set()).execute(
        request=request,
        table=candidate,
        reference_tables={"exchange_calendar": _calendar()},
    )

    failed = {
        item.rule_code: item.summary for item in outcome.results if item.status.value == "FAILED"
    }
    assert "coverage_by_security_exchange" in failed
    assert "2026-07-16" in failed["coverage_by_security_exchange"]
    assert "2026-07-17" not in failed["coverage_by_security_exchange"]


def test_quality_engine_fails_completely_missing_expected_security() -> None:
    candidate = _daily(
        [
            ("000001.SZ", date(2026, 7, 15)),
            ("000001.SZ", date(2026, 7, 16)),
        ]
    )
    request = QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04-r4",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ", "600000.SH"),
        expected_exchanges=("SZ", "SH"),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 16)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="a" * 64,
            )
        },
        empty_result=None,
    )

    outcome = QualityEngine(rule_set=default_rule_set()).execute(
        request=request,
        table=candidate,
        reference_tables={"exchange_calendar": _calendar()},
    )

    rule = next(
        item for item in outcome.results if item.rule_code == "coverage_by_security_exchange"
    )
    assert rule.status.value == "FAILED"
    assert "600000.SH" in rule.summary


def test_quality_engine_fails_trailing_open_session_gap() -> None:
    candidate = _daily([("000001.SZ", date(2026, 7, 15))])
    request = QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04-r4",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 16)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=new_entity_id("data_version"),
                content_sha256="a" * 64,
            )
        },
        empty_result=None,
    )

    outcome = QualityEngine(rule_set=default_rule_set()).execute(
        request=request,
        table=candidate,
        reference_tables={"exchange_calendar": _calendar()},
    )

    rule = next(
        item for item in outcome.results if item.rule_code == "coverage_by_security_exchange"
    )
    assert rule.status.value == "FAILED"
    assert "2026-07-16" in rule.summary


def test_quality_runner_loads_bound_references_and_rejects_wrong_sha(
    tmp_path: Path, uow_factory: DataSqliteUnitOfWorkFactory
) -> None:
    from quantking.data.quality.references import (
        PublishedReferenceLoader,
        ReferenceDataBindingError,
    )
    from quantking.data.quality.runner import QualityRunner

    version = DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        schema_version="exchange_calendar.v1",
        code_version="s1-04-r4",
        parent_version_id=None,
        input_batch_ids=(),
        created_at=utc_now(),
    )
    with uow_factory() as uow:
        uow.data.save_version(version)
        uow.commit()

    request = QualityRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        version_id=new_entity_id("data_version"),
        rule_version="quality-rules.v1",
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04-r4",
        content_sha256="0" * 64,
        input_batch_ids=(),
        expected_securities=("000001.SZ",),
        expected_exchanges=("SZ",),
        coverage_start=TradingDate(date(2026, 7, 15)),
        coverage_end=TradingDate(date(2026, 7, 16)),
        reference_data={
            "exchange_calendar": ReferenceDataBinding(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                version_id=version.version_id,
                content_sha256="f" * 64,
            )
        },
        empty_result=None,
    )
    runner = QualityRunner(
        engine=QualityEngine(rule_set=default_rule_set()),
        reference_loader=PublishedReferenceLoader(data_root=tmp_path, uow_factory=uow_factory),
    )

    with pytest.raises(ReferenceDataBindingError):
        runner.run(request=request, table=_daily([("000001.SZ", date(2026, 7, 15))]))
