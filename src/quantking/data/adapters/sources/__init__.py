"""Read-only market-data source adapters for Stage 1."""

from quantking.data.adapters.sources.capabilities import (
    XTQUANT_CAPABILITIES,
    XtQuantCapability,
)
from quantking.data.adapters.sources.contracts import (
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    SupplierResponseContract,
)

__all__ = [
    "XTQUANT_CAPABILITIES",
    "SourceAdapterError",
    "SourceErrorCode",
    "SupplierResponse",
    "SupplierResponseContract",
    "XtQuantCapability",
]
