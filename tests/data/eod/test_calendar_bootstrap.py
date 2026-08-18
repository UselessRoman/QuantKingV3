"""S1-08R3-C1 calendar bootstrap plan and table contracts (zero I/O)."""

from __future__ import annotations

from datetime import date

import pyarrow as pa
import pytest

from quantking.data.adapters.canonical_content import canonical_content_sha256
from quantking.data.eod.calendar_bootstrap import (
    CALENDAR_BOOTSTRAP_EXCHANGES,
    CALENDAR_BOOTSTRAP_PLAN_SCHEMA_VERSION,
    CALENDAR_BOOTSTRAP_PROTOCOL_MAX,
    CalendarBootstrapError,
    build_calendar_bootstrap_plan,
    build_exchange_calendar_table,
    calendar_bootstrap_plan_sha256,
)
from quantking.data.models import DatasetKind

_MARKER = "S1-08R3-C1-CALENDAR-BOOTSTRAP-V1"
_AUTH = "S1-08R3-C1-3f5a7c9e2b41"


def _plan(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "marker": _MARKER,
        "authorization_id": _AUTH,
        "start": date(2026, 5, 27),
        "end": date(2026, 8, 9),
        "code_tree_sha256": "a" * 64,
        "data_root_baseline_sha256": "b" * 64,
    }
    fields.update(overrides)
    return build_calendar_bootstrap_plan(**fields)  # type: ignore[arg-type]


def test_plan_is_pure_and_byte_stable() -> None:
    first = _plan()
    second = _plan()
    assert first == second
    assert calendar_bootstrap_plan_sha256(first) == calendar_bootstrap_plan_sha256(second)
    assert first["schema_version"] == CALENDAR_BOOTSTRAP_PLAN_SCHEMA_VERSION
    assert first["requires_new_real_authorization"] is True


def test_plan_freezes_the_c1_budget_and_write_surface() -> None:
    plan = _plan()
    assert plan["budgets"] == {
        "endpoint_max": 1,
        "protocol_max": CALENDAR_BOOTSTRAP_PROTOCOL_MAX,
        "http_max": 0,
        "application_retries": 0,
    }
    assert plan["fail_fast"] is True
    assert plan["hard_timeouts"] == {"session_seconds": 20.0}
    assert plan["exchanges"] == ["SH", "SZ"]
    assert plan["window"] == {"start": "2026-05-27", "end": "2026-08-09"}
    write_paths = [entry["relative_path"] for entry in plan["allowed_write_paths"]]
    assert "raw/baostock/exchange_calendar/<batch_id>" in write_paths
    assert "warehouse/exchange_calendar/<version_id>" in write_paths
    assert plan["reads_legacy_root"] is False
    assert plan["accesses_backup_root"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"marker": "S1-08R3-E1-FORMAL-RELEASE-V1"},
        {"authorization_id": "not-the-c1-shape"},
        {"start": date(2026, 8, 9), "end": date(2026, 5, 27)},
        {"end": date(2028, 8, 9)},
        {"code_tree_sha256": "z" * 64},
        {"exchanges": ("SZ",)},
        {"protocol_max": 9},
        {"timeout_seconds": 30.0},
    ],
)
def test_plan_rejects_out_of_contract_inputs(overrides: dict[str, object]) -> None:
    with pytest.raises(CalendarBootstrapError) as caught:
        _plan(**overrides)
    assert caught.value.code == "PLAN_FIELD_INVALID"


def _parse(plan: dict[str, object]):
    from quantking.data.eod.calendar_bootstrap import parse_calendar_bootstrap_plan

    return parse_calendar_bootstrap_plan(plan)


def test_parse_accepts_the_builder_output_and_binds_token() -> None:
    from quantking.data.eod.calendar_bootstrap import calendar_bootstrap_token

    checked = _parse(_plan())
    assert checked.marker == _MARKER
    assert checked.authorization_id == _AUTH
    assert checked.start == date(2026, 5, 27)
    assert checked.protocol_max == 4
    token = calendar_bootstrap_token(checked)
    assert token == f"{_MARKER}:{_AUTH}:{checked.plan_sha256}"


@pytest.mark.parametrize(
    "mutate",
    [
        {"unknown_field": True},
        {"schema_version": "other.v1"},
        {"provider": "AKSHARE"},
        {"upstream": "TENCENT"},
        {"endpoint": "query_history_k_data_plus"},
        {"window": {"start": "2026-08-09", "end": "2026-05-27"}},
        {"window": {"start": "2026-05-27", "end": "2029-05-27"}},
        {"exchanges": ["SZ"]},
        {
            "budgets": {
                "endpoint_max": 2,
                "protocol_max": 4,
                "http_max": 0,
                "application_retries": 0,
            }
        },
        {
            "budgets": {
                "endpoint_max": 1,
                "protocol_max": 4,
                "http_max": 1,
                "application_retries": 0,
            }
        },
        {
            "budgets": {
                "endpoint_max": 1,
                "protocol_max": 4,
                "http_max": 0,
                "application_retries": 1,
            }
        },
        {
            "budgets": {
                "endpoint_max": 1,
                "protocol_max": 9,
                "http_max": 0,
                "application_retries": 0,
            }
        },
        {"hard_timeouts": {"session_seconds": 30.0}},
        {"fail_fast": False},
        {"allowed_write_paths": []},
        {"reads_legacy_root": True},
        {"accesses_backup_root": True},
        {"other_datasets_touched": True},
        {"code_tree_sha256": "not-a-sha"},
        {"data_root_baseline_sha256": "not-a-sha"},
        {"requires_new_real_authorization": False},
        {"adapter_version": "other.v1"},
        {"baostock_version": "0.0.0"},
        {"code_version": "other.v1"},
        {"rule_version": "other.v1"},
        {"dataset": "daily_bar_unadjusted"},
        {"schema_version_calendar": "other.v1"},
    ],
)
def test_parse_rejects_any_drift_from_the_frozen_contract(mutate: dict[str, object]) -> None:
    plan = _plan()
    plan.update(mutate)
    with pytest.raises(CalendarBootstrapError) as caught:
        _parse(plan)
    assert caught.value.code == "CALENDAR_PLAN_INVALID"


def test_plan_content_change_invalidates_the_old_token() -> None:
    from quantking.data.eod.calendar_bootstrap import calendar_bootstrap_token

    first = _parse(_plan())
    plan = _plan()
    plan["budgets"] = {
        "endpoint_max": 1,
        "protocol_max": 3,
        "http_max": 0,
        "application_retries": 0,
    }
    second = _parse(plan)
    assert first.plan_sha256 != second.plan_sha256
    assert calendar_bootstrap_token(first) != calendar_bootstrap_token(second)


def test_table_projects_one_series_onto_both_exchanges_sorted() -> None:
    days = (("2026-05-29", "1"), ("2026-05-30", "0"), ("2026-05-31", "0"))
    table = build_exchange_calendar_table(days)
    assert isinstance(table, pa.Table)
    assert table.num_rows == 6
    assert table.column("exchange").to_pylist() == ["SH", "SH", "SH", "SZ", "SZ", "SZ"]
    assert (
        table.column("trade_date").to_pylist()
        == [
            date(2026, 5, 29),
            date(2026, 5, 30),
            date(2026, 5, 31),
        ]
        * 2
    )
    assert table.column("is_open").to_pylist() == [True, False, False, True, False, False]


def test_table_content_sha_is_stable_and_input_sensitive() -> None:
    days = (("2026-05-29", "1"), ("2026-05-30", "0"))
    first = canonical_content_sha256(
        DatasetKind.EXCHANGE_CALENDAR, [build_exchange_calendar_table(days)]
    )
    second = canonical_content_sha256(
        DatasetKind.EXCHANGE_CALENDAR, [build_exchange_calendar_table(tuple(reversed(days)))]
    )
    changed = canonical_content_sha256(
        DatasetKind.EXCHANGE_CALENDAR,
        [build_exchange_calendar_table((("2026-05-29", "0"), ("2026-05-30", "0")))],
    )
    assert first == second
    assert first != changed
    assert CALENDAR_BOOTSTRAP_EXCHANGES == ("SH", "SZ")
