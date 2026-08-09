from __future__ import annotations

from datetime import date

import pytest

from quantking.data.adapters.sources.contracts import SourceAdapterError, SourceErrorCode
from quantking.data.adapters.sources.parameters import (
    AKSHARE_ENDPOINT_TIMEOUT_SECONDS,
    DailyBarRequest,
    endpoint_parameters,
)
from quantking.data.adapters.sources.request_window import AKSHARE_LOOKBACK_DAYS
from quantking.data.models import DataUpstream


def test_parameters_are_centralized_explicit_and_unadjusted() -> None:
    request = DailyBarRequest("600000.SH", date(2026, 6, 1), date(2026, 7, 31))

    assert endpoint_parameters(DataUpstream.EASTMONEY, request) == {
        "symbol": "600000",
        "period": "daily",
        "start_date": "20260517",
        "end_date": "20260731",
        "adjust": "",
        "timeout": 15.0,
    }
    assert AKSHARE_ENDPOINT_TIMEOUT_SECONDS == 15.0
    assert endpoint_parameters(DataUpstream.TENCENT, request) == {
        "symbol": "sh600000",
        "start_date": "20260517",
        "end_date": "20260731",
        "adjust": "",
        "timeout": 15.0,
    }
    # Locked AKShare 1.18.83 exposes no timeout kwarg on stock_zh_a_daily.
    assert endpoint_parameters(DataUpstream.SINA, request) == {
        "symbol": "sh600000",
        "start_date": "20260517",
        "end_date": "20260731",
        "adjust": "",
    }


def test_target_60_dates_expand_to_exactly_75_bounded_upstream_dates() -> None:
    request = DailyBarRequest("600000.SH", date(2026, 6, 11), date(2026, 8, 9))
    window = request.request_window

    assert AKSHARE_LOOKBACK_DAYS == 15
    assert (window.target_end - window.target_start).days + 1 == 60
    assert window.upstream_start == date(2026, 5, 27)
    assert window.upstream_end == date(2026, 8, 9)
    assert (window.upstream_end - window.upstream_start).days + 1 == 75


@pytest.mark.parametrize("symbol", ["600000", "430047.BJ", "sh600000", "ABCDEF.SH"])
def test_parameters_reject_non_k1_symbol_formats(symbol: str) -> None:
    with pytest.raises(SourceAdapterError) as caught:
        DailyBarRequest(symbol, date(2026, 1, 1), date(2026, 1, 2))
    assert caught.value.code is SourceErrorCode.REQUEST_REJECTED


def test_parameters_reject_reversed_range() -> None:
    with pytest.raises(SourceAdapterError) as caught:
        DailyBarRequest("000001.SZ", date(2026, 1, 2), date(2026, 1, 1))
    assert caught.value.code is SourceErrorCode.REQUEST_REJECTED
