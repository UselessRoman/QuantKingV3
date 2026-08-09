"""Provider-neutral, deterministic incremental fetch planning."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.incremental import (
    CoverageWindow,
    IncrementalPlanRequest,
    build_incremental_plan,
)
from quantking.data.models import DatasetKind


def _day(value: str) -> TradingDate:
    return TradingDate(date.fromisoformat(value))


def _request(**changes: object) -> IncrementalPlanRequest:
    values: dict[str, object] = {
        "batch_id": new_entity_id("data_batch"),
        "dataset": DatasetKind.DAILY_BAR_UNADJUSTED,
        "scope": ("600000.SH", "000001.SZ"),
        "request_start": _day("2026-07-01"),
        "request_end": _day("2026-07-10"),
        "start_inclusive": True,
        "end_inclusive": True,
        "known_coverage": (
            CoverageWindow("000001.SZ", _day("2026-07-01"), _day("2026-07-03")),
            CoverageWindow("000001.SZ", _day("2026-07-05"), _day("2026-07-08")),
            CoverageWindow("600000.SH", _day("2026-07-01"), _day("2026-07-08")),
        ),
        "lookback_days": 2,
        "max_days_per_chunk": 2,
        "max_items_per_chunk": 1,
        "adapter_version": "akshare-daily-bar.v1",
    }
    values.update(changes)
    return IncrementalPlanRequest(**values)  # type: ignore[arg-type]


def test_plan_is_deterministic_sorted_and_preserves_middle_gaps() -> None:
    request = _request()
    first = build_incremental_plan(request)
    second = build_incremental_plan(request)

    assert first == second
    assert first.scope == ("000001.SZ", "600000.SH")
    assert [(str(c.start), str(c.end), c.scope) for c in first.chunks] == [
        ("2026-07-04", "2026-07-04", ("000001.SZ",)),
        ("2026-07-07", "2026-07-08", ("000001.SZ",)),
        ("2026-07-07", "2026-07-08", ("600000.SH",)),
        ("2026-07-09", "2026-07-10", ("000001.SZ",)),
        ("2026-07-09", "2026-07-10", ("600000.SH",)),
    ]
    assert all(chunk.start.value <= chunk.end.value for chunk in first.chunks)
    assert [chunk.sequence for chunk in first.chunks] == list(range(len(first.chunks)))


def test_boundaries_and_explicit_zero_lookback_are_honoured() -> None:
    plan = build_incremental_plan(
        _request(
            scope=("000001.SZ",),
            known_coverage=(),
            start_inclusive=False,
            end_inclusive=False,
            lookback_days=0,
            max_days_per_chunk=3,
        )
    )
    assert [(str(chunk.start), str(chunk.end)) for chunk in plan.chunks] == [
        ("2026-07-02", "2026-07-04"),
        ("2026-07-05", "2026-07-07"),
        ("2026-07-08", "2026-07-09"),
    ]
    assert plan.request_params["start_inclusive"] == "false"
    assert plan.request_params["end_inclusive"] == "false"
    assert plan.request_params["lookback_days"] == "0"


def test_same_parameters_with_new_batch_id_have_same_replay_summary() -> None:
    first = build_incremental_plan(_request())
    second = build_incremental_plan(_request(batch_id=new_entity_id("data_batch")))
    assert first.batch_id != second.batch_id
    assert first.request_params == second.request_params
    assert first.request_sha256 == second.request_sha256
    assert first.chunks == second.chunks


def test_empty_universe_produces_no_supplier_chunk() -> None:
    plan = build_incremental_plan(_request(scope=(), known_coverage=()))
    assert plan.scope == ()
    assert plan.chunks == ()


@pytest.mark.parametrize(
    "changes",
    (
        {"scope": ("../escape",)},
        {"scope": ("000001.SZ/evil",)},
        {"lookback_days": -1},
        {"max_days_per_chunk": 0},
        {"max_items_per_chunk": 0},
        {"request_start": _day("2026-07-11")},
        {"adapter_version": r"SECRET E:\private"},
    ),
)
def test_invalid_ranges_components_and_chunk_rules_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _request(**changes)


def test_direct_chunk_construction_rejects_unsorted_duplicate_scope() -> None:
    chunk = build_incremental_plan(_request()).chunks[0]
    with pytest.raises(ValueError):
        replace(chunk, scope=("600000.SH", "000001.SZ", "000001.SZ"))


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "reversed", "request_hash"))
def test_direct_plan_construction_rejects_chunk_inventory_and_hash_mutation(
    mutation: str,
) -> None:
    plan = build_incremental_plan(_request())
    with pytest.raises(ValueError):
        if mutation == "missing":
            replace(plan, chunks=plan.chunks[1:])
        elif mutation == "duplicate":
            replace(plan, chunks=(plan.chunks[0], *plan.chunks))
        elif mutation == "reversed":
            replace(plan, chunks=tuple(reversed(plan.chunks)))
        else:
            replace(plan, request_sha256="0" * 64)


def test_plan_request_params_are_generated_from_an_exact_whitelist() -> None:
    plan = build_incremental_plan(_request())
    assert set(plan.request_params) == {
        "adapter_version",
        "dataset",
        "end_inclusive",
        "known_coverage",
        "lookback_days",
        "max_days_per_chunk",
        "max_items_per_chunk",
        "request_end",
        "request_start",
        "scope",
        "start_inclusive",
    }
    with pytest.raises(ValueError, match="whitelist"):
        replace(
            plan,
            request_params={**plan.request_params, "token": "SECRET"},
        )
