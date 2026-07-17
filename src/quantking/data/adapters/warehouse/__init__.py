"""Raw batch landing and Canonical Warehouse adapters (Stage 1 S1-02).

The public surface is exposed module-by-module so that the
DuckDB-based read-only query adapter can be implemented independently
without pulling :mod:`duckdb` into every consumer. Importing the
package never transitively pulls in :mod:`duckdb`; :mod:`pyarrow` is
allowed because the canonical warehouse publishes Parquet files.
"""

from quantking.data.adapters.warehouse.canonical_warehouse import (
    CanonicalPublishRequest,
    CanonicalWarehouse,
)
from quantking.data.adapters.warehouse.partitioning import (
    PARTITION_SCHEME_V1,
    CanonicalPartitionScheme,
    partition_path_for,
    partition_scheme_for,
)
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchPublisher,
    RawBatchPublishRequest,
    RawManifestFile,
    load_raw_manifest,
)

__all__ = [
    "PARTITION_SCHEME_V1",
    "CanonicalPartitionScheme",
    "CanonicalPublishRequest",
    "CanonicalWarehouse",
    "RawBatchPublishRequest",
    "RawBatchPublisher",
    "RawManifestFile",
    "load_raw_manifest",
    "partition_path_for",
    "partition_scheme_for",
]
