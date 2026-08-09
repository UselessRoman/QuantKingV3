"""Fixed Stage 1 AKShare upstream registry."""

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


def source_registry() -> tuple[SourceSpec, ...]:
    """Return the immutable registry in stable A/B/C order."""
    return tuple(_REGISTRY[upstream] for upstream in DataUpstream)


def source_spec(upstream: DataUpstream) -> SourceSpec:
    if not isinstance(upstream, DataUpstream):
        raise TypeError("upstream must be a DataUpstream")
    return _REGISTRY[upstream]


__all__ = ["SourceSpec", "source_registry", "source_spec"]
