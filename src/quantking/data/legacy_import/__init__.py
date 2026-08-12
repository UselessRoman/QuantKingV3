"""Framework-free contracts for the S1-07K3C legacy DATA_ONLY import."""

from quantking.data.legacy_import.contracts import (
    K3C_SYNTHETIC_AUTHORIZATION_MARKER,
    LEGACY_IMPORT_ADAPTER_VERSION,
    LEGACY_IMPORT_MANIFEST_SCHEMA_VERSION,
    LegacyEvidenceStatus,
    LegacyImportAuthorization,
    LegacyImportDryRun,
    LegacyImportInventory,
    LegacyImportMode,
    LegacyImportSemantics,
    LegacyInventoryEntry,
    LegacyResolutionEvidenceSummary,
)

__all__ = [
    "K3C_SYNTHETIC_AUTHORIZATION_MARKER",
    "LEGACY_IMPORT_ADAPTER_VERSION",
    "LEGACY_IMPORT_MANIFEST_SCHEMA_VERSION",
    "LegacyEvidenceStatus",
    "LegacyImportAuthorization",
    "LegacyImportDryRun",
    "LegacyImportInventory",
    "LegacyImportMode",
    "LegacyImportSemantics",
    "LegacyInventoryEntry",
    "LegacyResolutionEvidenceSummary",
]
