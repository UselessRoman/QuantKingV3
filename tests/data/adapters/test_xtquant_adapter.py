"""XTQuant read-only boundary, delayed loading, capabilities, and errors."""

from __future__ import annotations

import socket
import sys
from datetime import date
from pathlib import Path

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate
from quantking.data.adapters.sources.capabilities import XTQUANT_CAPABILITIES
from quantking.data.adapters.sources.contracts import (
    SourceAdapterError,
    SourceErrorCode,
    classify_source_error,
)
from quantking.data.adapters.sources.xtquant import (
    ExplicitXtQuantModuleLoader,
    XtQuantMarketDataGateway,
)
from quantking.data.incremental import IncrementalPlanRequest, build_incremental_plan
from quantking.data.models import BatchFailureKind, DatasetKind


def _calendar_chunk() -> object:
    plan = build_incremental_plan(
        IncrementalPlanRequest(
            batch_id=new_entity_id("data_batch"),
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            scope=("SH",),
            request_start=TradingDate(date(2026, 7, 17)),
            request_end=TradingDate(date(2026, 7, 18)),
            start_inclusive=True,
            end_inclusive=True,
            known_coverage=(),
            lookback_days=0,
            max_days_per_chunk=10,
            max_items_per_chunk=3,
            adapter_version="xtquant-s1-03.v1",
        )
    )
    return plan.chunks[0]


def test_core_imports_do_not_require_or_import_xtquant() -> None:
    before = {name for name in sys.modules if name == "xtquant" or name.startswith("xtquant.")}
    __import__("quantking.data.models")
    __import__("quantking.data.incremental")
    __import__("quantking.data.adapters.sources.contracts")
    after = {name for name in sys.modules if name == "xtquant" or name.startswith("xtquant.")}
    assert after == before


def test_capability_matrix_is_explicit_for_all_six_datasets() -> None:
    assert tuple(item.dataset for item in XTQUANT_CAPABILITIES) == tuple(DatasetKind)
    by_dataset = {item.dataset: item for item in XTQUANT_CAPABILITIES}
    assert by_dataset[DatasetKind.EXCHANGE_CALENDAR].available
    assert by_dataset[DatasetKind.EXCHANGE_CALENDAR].public_interface == "get_trading_dates"
    for dataset in (
        DatasetKind.SECURITY_MASTER,
        DatasetKind.DAILY_BAR_UNADJUSTED,
        DatasetKind.ADJUSTMENT_FACTOR,
        DatasetKind.TRADING_STATUS,
        DatasetKind.BENCHMARK_INDEX_DAILY,
    ):
        assert not by_dataset[dataset].available
        assert by_dataset[dataset].gap


def test_loader_uses_only_explicit_xtdata_and_restores_sys_path(tmp_path: Path) -> None:
    root = tmp_path / "site-packages"
    package = root / "xtquant"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__='fake'\n", encoding="utf-8")
    (package / "xtdata.py").write_text(
        "def get_trading_dates(market, start_time='', end_time='', count=-1):\n"
        "    return [1658016000000]\n",
        encoding="utf-8",
    )
    (package / "xttrader.py").write_text(
        "raise AssertionError('trading module must never load')\n", encoding="utf-8"
    )
    before = tuple(sys.path)
    try:
        module = ExplicitXtQuantModuleLoader(root).load()
        assert module.__name__ == "xtquant.xtdata"
        assert tuple(sys.path) == before
        assert "xtquant.xttrader" not in sys.modules
    finally:
        for name in tuple(sys.modules):
            if name == "xtquant" or name.startswith("xtquant."):
                sys.modules.pop(name, None)


def test_unverified_capability_fails_before_sdk_load() -> None:
    loaded = {"count": 0}

    def loader() -> object:
        loaded["count"] += 1
        raise AssertionError("SDK must not load for unavailable capability")

    gateway = XtQuantMarketDataGateway(module_loader=loader)
    plan = build_incremental_plan(
        IncrementalPlanRequest(
            batch_id=new_entity_id("data_batch"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            scope=("000001.SZ",),
            request_start=TradingDate(date(2026, 7, 17)),
            request_end=TradingDate(date(2026, 7, 17)),
            start_inclusive=True,
            end_inclusive=True,
            known_coverage=(),
            lookback_days=0,
            max_days_per_chunk=1,
            max_items_per_chunk=1,
            adapter_version="xtquant-s1-03.v1",
        )
    )
    with pytest.raises(SourceAdapterError) as exc_info:
        gateway.fetch(plan.chunks[0])
    assert exc_info.value.code is SourceErrorCode.CAPABILITY_UNAVAILABLE
    assert exc_info.value.failure_kind is BatchFailureKind.INTERNAL
    assert loaded["count"] == 0


def test_permission_error_is_chained_but_public_message_is_stable_and_redacted() -> None:
    class DeniedModule:
        @staticmethod
        def get_trading_dates(*_args: object, **_kwargs: object) -> object:
            raise PermissionError(r"token=SECRET database=E:\private\market")

    gateway = XtQuantMarketDataGateway(module_loader=lambda: DeniedModule())
    with pytest.raises(SourceAdapterError) as exc_info:
        gateway.fetch(_calendar_chunk())  # type: ignore[arg-type]
    error = exc_info.value
    assert error.code is SourceErrorCode.PERMISSION
    assert error.failure_kind is BatchFailureKind.PERMISSION
    assert error.message == "XTQuant permission or authorization failed"
    assert "SECRET" not in str(error)
    assert "E:\\" not in str(error)
    assert isinstance(error.__cause__, PermissionError)


def test_empty_calendar_response_is_not_expanded_to_all_closed_days() -> None:
    class EmptyCalendarModule:
        @staticmethod
        def get_trading_dates(*_args: object, **_kwargs: object) -> list[object]:
            return []

    response = XtQuantMarketDataGateway(module_loader=lambda: EmptyCalendarModule()).fetch(
        _calendar_chunk()
    )  # type: ignore[arg-type]
    assert response.records == ()


def test_gateway_revalidates_tampered_chunk_at_entry() -> None:
    chunk = _calendar_chunk()
    object.__setattr__(chunk, "scope", ("../escape",))
    gateway = XtQuantMarketDataGateway(module_loader=lambda: object())
    with pytest.raises(ValueError):
        gateway.fetch(chunk)  # type: ignore[arg-type]


def test_loader_failure_restores_all_xtquant_import_state(tmp_path: Path) -> None:
    root = tmp_path / "site-packages"
    package = root / "xtquant"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import xtquant.xttrader\n", encoding="utf-8")
    (package / "xttrader.py").write_text("LOADED = True\n", encoding="utf-8")
    (package / "xtdata.py").write_text(
        "raise RuntimeError('sensitive loader failure')\n",
        encoding="utf-8",
    )
    before = {
        name: module
        for name, module in sys.modules.items()
        if name == "xtquant" or name.startswith("xtquant.")
    }
    with pytest.raises(SourceAdapterError):
        ExplicitXtQuantModuleLoader(root).load()
    after = {
        name: module
        for name, module in sys.modules.items()
        if name == "xtquant" or name.startswith("xtquant.")
    }
    assert after == before


def test_loader_rejects_and_removes_new_trading_module_on_successful_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "site-packages"
    package = root / "xtquant"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import xtquant.xttrader\n", encoding="utf-8")
    (package / "xttrader.py").write_text("LOADED = True\n", encoding="utf-8")
    (package / "xtdata.py").write_text(
        "def get_trading_dates(*args, **kwargs):\n    return []\n",
        encoding="utf-8",
    )
    before = {
        name: module
        for name, module in sys.modules.items()
        if name == "xtquant" or name.startswith("xtquant.")
    }
    with pytest.raises(SourceAdapterError) as exc_info:
        ExplicitXtQuantModuleLoader(root).load()
    assert exc_info.value.code is SourceErrorCode.CAPABILITY_UNAVAILABLE
    after = {
        name: module
        for name, module in sys.modules.items()
        if name == "xtquant" or name.startswith("xtquant.")
    }
    assert after == before


@pytest.mark.parametrize(
    ("raw", "code", "kind", "retryable"),
    (
        (
            TimeoutError(r"token=SECRET E:\private"),
            SourceErrorCode.NETWORK,
            BatchFailureKind.NETWORK,
            True,
        ),
        (
            PermissionError("account=123456"),
            SourceErrorCode.PERMISSION,
            BatchFailureKind.PERMISSION,
            False,
        ),
        (
            ValueError("supplier field secret"),
            SourceErrorCode.RESPONSE_CONTRACT,
            BatchFailureKind.PARSE,
            False,
        ),
        (
            RuntimeError("opaque SDK secret"),
            SourceErrorCode.INTERNAL_SDK,
            BatchFailureKind.INTERNAL,
            True,
        ),
    ),
)
def test_unknown_errors_have_stable_redacted_classification(
    raw: BaseException,
    code: SourceErrorCode,
    kind: BatchFailureKind,
    retryable: bool,
) -> None:
    error = classify_source_error(raw)
    assert error.code is code
    assert error.failure_kind is kind
    assert error.retryable is retryable
    assert "SECRET" not in str(error)
    assert "123456" not in str(error)
    assert "E:\\" not in str(error)


def test_default_suite_network_guard_is_active() -> None:
    with pytest.raises(RuntimeError, match="cannot access the network"):
        socket.getaddrinfo("example.invalid", 443)
