"""Central AKShare security-code and date parameter conversion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from quantking.data.adapters.sources.contracts import SourceAdapterError, SourceErrorCode
from quantking.data.adapters.sources.request_window import (
    AkshareRequestWindow,
    fixed_akshare_request_window,
)
from quantking.data.models import DataUpstream

_CANONICAL_SYMBOL = re.compile(r"(?P<digits>\d{6})\.(?P<exchange>SH|SZ)\Z")
_SUPPORTED_SH_A_SHARE = re.compile(r"(?:600|601|603|605|688)\d{3}\.SH\Z")
_SUPPORTED_SZ_A_SHARE = re.compile(r"(?:000|001|002|003|300|301)\d{3}\.SZ\Z")
AKSHARE_ENDPOINT_TIMEOUT_SECONDS = 15.0


def is_supported_a_share(symbol: str) -> bool:
    """Return whether the symbol belongs to the explicit K1 Shanghai/Shenzhen A-share set."""
    if not isinstance(symbol, str):
        return False
    return (
        _SUPPORTED_SH_A_SHARE.fullmatch(symbol) is not None
        or _SUPPORTED_SZ_A_SHARE.fullmatch(symbol) is not None
    )


@dataclass(frozen=True, slots=True)
class DailyBarRequest:
    """One explicit symbol/date request; no implicit latest window."""

    symbol: str
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if _CANONICAL_SYMBOL.fullmatch(self.symbol) is None:
            raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
        if not is_supported_a_share(self.symbol):
            raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
        if self.start_date > self.end_date:
            raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)

    @property
    def request_window(self) -> AkshareRequestWindow:
        return fixed_akshare_request_window(self.start_date, self.end_date)


def endpoint_parameters(
    upstream: DataUpstream,
    request: DailyBarRequest,
) -> dict[str, str | float]:
    """Return the exact unadjusted endpoint kwargs for one upstream."""
    if not isinstance(upstream, DataUpstream):
        raise TypeError("upstream must be a DataUpstream")
    if not isinstance(request, DailyBarRequest):
        raise TypeError("request must be a DailyBarRequest")
    matched = _CANONICAL_SYMBOL.fullmatch(request.symbol)
    if matched is None:  # guarded by DailyBarRequest, retained for type narrowing
        raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
    digits = matched.group("digits")
    exchange = matched.group("exchange").lower()
    window = request.request_window
    start = window.upstream_start.strftime("%Y%m%d")
    end = window.upstream_end.strftime("%Y%m%d")
    if upstream not in {DataUpstream.TENCENT, DataUpstream.SINA}:
        raise SourceAdapterError(SourceErrorCode.REQUEST_REJECTED)
    parameters: dict[str, str | float] = {
        "symbol": f"{exchange}{digits}",
        "start_date": start,
        "end_date": end,
        "adjust": "",
    }
    # AKShare 1.18.83 exposes timeout for Tencent, but not for Sina. The Sina
    # wall-clock bound is enforced by the controlled subprocess session timeout.
    if upstream is DataUpstream.TENCENT:
        parameters["timeout"] = AKSHARE_ENDPOINT_TIMEOUT_SECONDS
    return parameters


__all__ = [
    "AKSHARE_ENDPOINT_TIMEOUT_SECONDS",
    "DailyBarRequest",
    "endpoint_parameters",
    "is_supported_a_share",
]
