"""Fixed Stage 1 source roles and provider/upstream pairs."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from quantking.data.models import DataProvider, DatasetKind, DataUpstream


@dataclass(frozen=True, slots=True)
class SourceSpec:
    provider: DataProvider
    upstream: DataUpstream
    endpoint: str
    dataset: DatasetKind


_REGISTRY = MappingProxyType(
    {
        DataUpstream.EASTMONEY: SourceSpec(
            provider=DataProvider.AKSHARE,
            upstream=DataUpstream.EASTMONEY,
            endpoint="stock_zh_a_hist",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        ),
        DataUpstream.TENCENT: SourceSpec(
            provider=DataProvider.AKSHARE,
            upstream=DataUpstream.TENCENT,
            endpoint="stock_zh_a_hist_tx",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        ),
        DataUpstream.SINA: SourceSpec(
            provider=DataProvider.AKSHARE,
            upstream=DataUpstream.SINA,
            endpoint="stock_zh_a_daily",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        ),
    }
)

AKSHARE_RUNTIME_UPSTREAMS = (
    DataUpstream.EASTMONEY,
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)
PRODUCTION_UPSTREAMS = (
    DataUpstream.BAOSTOCK,
    DataUpstream.EASTMONEY,
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)
LEGACY_ONLY_UPSTREAMS = (DataUpstream.XTQUANT,)
PROVIDER_UPSTREAM_PAIRS = frozenset(
    {
        (DataProvider.BAOSTOCK, DataUpstream.BAOSTOCK),
        (DataProvider.AKSHARE, DataUpstream.EASTMONEY),
        (DataProvider.AKSHARE, DataUpstream.TENCENT),
        (DataProvider.AKSHARE, DataUpstream.SINA),
        (DataProvider.XTQUANT_LEGACY, DataUpstream.XTQUANT),
    }
)


def source_registry() -> tuple[SourceSpec, ...]:
    """Return the immutable AKShare registry in its historical order."""
    return tuple(_REGISTRY[upstream] for upstream in AKSHARE_RUNTIME_UPSTREAMS)


def source_spec(upstream: DataUpstream) -> SourceSpec:
    if not isinstance(upstream, DataUpstream):
        raise TypeError("upstream must be a DataUpstream")
    if upstream not in AKSHARE_RUNTIME_UPSTREAMS:
        raise ValueError("source_spec supports AKShare runtime upstreams only")
    return _REGISTRY[upstream]


__all__ = [
    "AKSHARE_RUNTIME_UPSTREAMS",
    "LEGACY_ONLY_UPSTREAMS",
    "PRODUCTION_UPSTREAMS",
    "PROVIDER_UPSTREAM_PAIRS",
    "SourceSpec",
    "source_registry",
    "source_spec",
]
