"""Read-only AKShare upstream adapters for Stage 1."""

from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareEndpointResponse,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.contracts import (
    NetworkReason,
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    SupplierResponseContract,
)
from quantking.data.adapters.sources.registry import SourceSpec, source_registry, source_spec

__all__ = [
    "AkshareDailyBarAdapter",
    "AkshareEndpointResponse",
    "AkshareMarketDataGateway",
    "NetworkReason",
    "SourceAdapterError",
    "SourceErrorCode",
    "SourceSpec",
    "SupplierResponse",
    "SupplierResponseContract",
    "source_registry",
    "source_spec",
]
