"""Warehouse-specific stable error types.

These are stable error categories exposed across the warehouse and query
adapter boundaries. The base :class:`WarehouseError` inherits from
:class:`quantking.common.errors.QuantKingError` so all warehouse failures
carry a stable error code and string context, which the S1-02 tests rely
on to distinguish published/failed/conflict outcomes.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from quantking.common.errors import ErrorCode, QuantKingError


class WarehouseErrorCode(StrEnum):
    """Stable codes for warehouse-specific failures."""

    RAW_PUBLISH_FAILED = "raw_publish_failed"
    CANONICAL_PUBLISH_FAILED = "canonical_publish_failed"
    PARTITION_SCHEME_INVALID = "partition_scheme_invalid"
    MANIFEST_INTEGRITY = "manifest_integrity"
    QUERY_CONTRACT_VIOLATION = "query_contract_violation"
    INCOMPATIBLE_INPUT = "incompatible_input"


class WarehouseError(QuantKingError):
    """Base class for warehouse adapter failures.

    Warehouse errors intentionally do not collide with the public
    :class:`quantking.common.errors.ErrorCode` enum: the public codes are
    for cross-domain contracts (paths, transitions, idempotency, ...).
    Warehouse-specific failures expose :class:`WarehouseErrorCode` while
    remaining rooted in :class:`QuantKingError` so they can be rendered and
    converted to a stable string context.
    """

    warehouse_code: WarehouseErrorCode

    def __init__(
        self,
        warehouse_code: WarehouseErrorCode,
        message: str,
        *,
        context: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(warehouse_code, WarehouseErrorCode):
            raise TypeError("warehouse_code must be a WarehouseErrorCode")
        self.warehouse_code = warehouse_code
        super().__init__(ErrorCode.CONFIGURATION, message, context=context)


class RawStoreError(WarehouseError):
    """A raw batch landing failed and the failure is preserved on disk."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(WarehouseErrorCode.RAW_PUBLISH_FAILED, message, context=context)


class CanonicalWarehouseError(WarehouseError):
    """A canonical warehouse publish or contract check failed."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(WarehouseErrorCode.CANONICAL_PUBLISH_FAILED, message, context=context)


class PartitionSchemeError(WarehouseError):
    """A dataset does not have a recognised partition scheme."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(WarehouseErrorCode.PARTITION_SCHEME_INVALID, message, context=context)


class ManifestIntegrityError(WarehouseError):
    """A manifest does not match the on-disk files."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(WarehouseErrorCode.MANIFEST_INTEGRITY, message, context=context)


class QueryContractError(WarehouseError):
    """A query request violated the contract (unknown field, ad-hoc SQL, etc.)."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(WarehouseErrorCode.QUERY_CONTRACT_VIOLATION, message, context=context)


class IncompatibleInputError(WarehouseError):
    """A publish or query request is not compatible with the dataset/version."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(WarehouseErrorCode.INCOMPATIBLE_INPUT, message, context=context)


class DatasetVersionNotQueryableError(WarehouseError):
    """A :class:`quantking.data.models.DatasetVersion` cannot be queried."""

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        super().__init__(WarehouseErrorCode.QUERY_CONTRACT_VIOLATION, message, context=context)


__all__ = [
    "CanonicalWarehouseError",
    "DatasetVersionNotQueryableError",
    "IncompatibleInputError",
    "ManifestIntegrityError",
    "PartitionSchemeError",
    "QueryContractError",
    "RawStoreError",
    "WarehouseError",
    "WarehouseErrorCode",
]
