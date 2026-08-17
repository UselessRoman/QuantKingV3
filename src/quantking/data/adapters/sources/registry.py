"""Fixed Stage 1 source roles and provider/upstream pairs (ADR-021 three-role route)."""

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


# Runtime AKShare registry: Tencent is the fixed B source and Sina is the fixed
# exception-arbitration C source. EASTMONEY is retired from the runtime route by
# ADR-021; its historical Raw, migrations and evidence stay readable through the
# enum and PROVIDER_UPSTREAM_PAIRS, never through a runtime SourceSpec.
_REGISTRY = MappingProxyType(
    {
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
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)
PRODUCTION_UPSTREAMS = (
    DataUpstream.BAOSTOCK,
    DataUpstream.TENCENT,
    DataUpstream.SINA,
)
# Retired AKShare upstreams kept only for historical Raw/migration readability.
HISTORICAL_AKSHARE_UPSTREAMS = (DataUpstream.EASTMONEY,)
LEGACY_ONLY_UPSTREAMS = (DataUpstream.XTQUANT,)
PROVIDER_UPSTREAM_PAIRS = frozenset(
    {
        (DataProvider.BAOSTOCK, DataUpstream.BAOSTOCK),
        (DataProvider.AKSHARE, DataUpstream.TENCENT),
        (DataProvider.AKSHARE, DataUpstream.SINA),
        # Historical readability: retired EastMoney lineage still parses.
        (DataProvider.AKSHARE, DataUpstream.EASTMONEY),
        (DataProvider.XTQUANT_LEGACY, DataUpstream.XTQUANT),
    }
)


def source_registry() -> tuple[SourceSpec, ...]:
    """Return the immutable runtime AKShare registry in its fixed B/C order."""
    return tuple(_REGISTRY[upstream] for upstream in AKSHARE_RUNTIME_UPSTREAMS)


def source_spec(upstream: DataUpstream) -> SourceSpec:
    if not isinstance(upstream, DataUpstream):
        raise TypeError("upstream must be a DataUpstream")
    if upstream not in AKSHARE_RUNTIME_UPSTREAMS:
        raise ValueError("source_spec supports AKShare runtime upstreams only")
    return _REGISTRY[upstream]


__all__ = [
    "AKSHARE_RUNTIME_UPSTREAMS",
    "HISTORICAL_AKSHARE_UPSTREAMS",
    "LEGACY_ONLY_UPSTREAMS",
    "PRODUCTION_UPSTREAMS",
    "PROVIDER_UPSTREAM_PAIRS",
    "SourceSpec",
    "source_registry",
    "source_spec",
]
