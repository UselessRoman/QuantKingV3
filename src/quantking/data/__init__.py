"""Market data domain: contracts, schemas, and metadata persistence adapters.

Framework-free domain objects live in :mod:`quantking.data.models` and schema
descriptors in :mod:`quantking.data.schemas`. The PyArrow mapping and SQLAlchemy
ORM stay inside :mod:`quantking.data.adapters` so the domain never imports a
third-party framework.
"""

from quantking.data.models import (
    BATCH_TRANSITIONS,
    DATASET_VERSION_TRANSITIONS,
    OVERRIDE_TRANSITIONS,
    BatchFailureInfo,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    DifferenceReport,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
    QualityRunStatus,
    SourceOverride,
    SourceOverrideStatus,
    transition_batch,
    transition_dataset_version,
    transition_override,
)
from quantking.data.schemas import (
    DatasetSchemaDescriptor,
    FieldDescriptor,
    FieldType,
    canonical_code_pattern,
    descriptor_for,
)

__all__ = [
    "BATCH_TRANSITIONS",
    "DATASET_VERSION_TRANSITIONS",
    "OVERRIDE_TRANSITIONS",
    "BatchFailureInfo",
    "DataSource",
    "DatasetKind",
    "DatasetSchemaDescriptor",
    "DatasetVersion",
    "DatasetVersionStatus",
    "DifferenceReport",
    "FieldDescriptor",
    "FieldType",
    "IngestionBatch",
    "IngestionBatchStatus",
    "ManifestFileEntry",
    "QualityResult",
    "QualityRun",
    "QualityRunStatus",
    "SourceOverride",
    "SourceOverrideStatus",
    "canonical_code_pattern",
    "descriptor_for",
    "transition_batch",
    "transition_dataset_version",
    "transition_override",
]
