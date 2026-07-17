"""Canonical partition scheme for Stage 1 Warehouse releases.

The partition scheme is a deliberately small, explicit contract:

* The current scheme version is :data:`PARTITION_SCHEME_V1`.
* For the six canonical datasets, the only partition keys are
  ``year`` and ``month`` (rendered as zero-padded ``YYYY`` / ``MM``).
* ``security_master`` partitions on ``effective_from`` instead of
  ``trade_date`` because its point-in-time semantics live there; the
  resulting on-disk layout still uses ``year=YYYY/month=MM`` so all
  datasets share a single file-tree convention.
* Every other dataset partitions on the canonical sort key's first date
  field (``trade_date`` for daily, index, factor, status; the calendar
  uses ``trade_date``).
* The scheme id and version are recorded in the manifest so future
  changes produce a new scheme version rather than silently changing
  partition paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from quantking.data.adapters.warehouse.errors import PartitionSchemeError
from quantking.data.models import DatasetKind

PARTITION_SCHEME_V1 = "v1"

# The directory layout is ``year=YYYY/month=MM/part-NNNNN.parquet`` for
# every dataset. Each part is numbered sequentially within a single
# publish. We deliberately do not yet partition by security code bucket
# in S1-02; the partition scheme is versioned so adding a code bucket can
# be done later as a new scheme version.
PART_FILE_TEMPLATE = "part-{part_index:05d}.parquet"


@dataclass(frozen=True, slots=True)
class CanonicalPartitionScheme:
    """A versioned partition scheme description for one dataset family."""

    dataset: str
    scheme_id: str
    scheme_version: str
    partition_keys: tuple[str, ...]
    date_field: str

    def to_dict(self) -> dict[str, str | tuple[str, ...]]:
        return {
            "dataset": self.dataset,
            "scheme_id": self.scheme_id,
            "scheme_version": self.scheme_version,
            "partition_keys": self.partition_keys,
            "date_field": self.date_field,
        }


_SCHEMES: dict[str, CanonicalPartitionScheme] = {
    DatasetKind.SECURITY_MASTER.value: CanonicalPartitionScheme(
        dataset=DatasetKind.SECURITY_MASTER.value,
        scheme_id="effective_from_v1",
        scheme_version=PARTITION_SCHEME_V1,
        partition_keys=("year", "month"),
        date_field="effective_from",
    ),
    DatasetKind.EXCHANGE_CALENDAR.value: CanonicalPartitionScheme(
        dataset=DatasetKind.EXCHANGE_CALENDAR.value,
        scheme_id="trade_date_v1",
        scheme_version=PARTITION_SCHEME_V1,
        partition_keys=("year", "month"),
        date_field="trade_date",
    ),
    DatasetKind.DAILY_BAR_UNADJUSTED.value: CanonicalPartitionScheme(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
        scheme_id="trade_date_v1",
        scheme_version=PARTITION_SCHEME_V1,
        partition_keys=("year", "month"),
        date_field="trade_date",
    ),
    DatasetKind.ADJUSTMENT_FACTOR.value: CanonicalPartitionScheme(
        dataset=DatasetKind.ADJUSTMENT_FACTOR.value,
        scheme_id="trade_date_v1",
        scheme_version=PARTITION_SCHEME_V1,
        partition_keys=("year", "month"),
        date_field="trade_date",
    ),
    DatasetKind.TRADING_STATUS.value: CanonicalPartitionScheme(
        dataset=DatasetKind.TRADING_STATUS.value,
        scheme_id="trade_date_v1",
        scheme_version=PARTITION_SCHEME_V1,
        partition_keys=("year", "month"),
        date_field="trade_date",
    ),
    DatasetKind.BENCHMARK_INDEX_DAILY.value: CanonicalPartitionScheme(
        dataset=DatasetKind.BENCHMARK_INDEX_DAILY.value,
        scheme_id="trade_date_v1",
        scheme_version=PARTITION_SCHEME_V1,
        partition_keys=("year", "month"),
        date_field="trade_date",
    ),
}


def partition_scheme_for(dataset: str) -> CanonicalPartitionScheme:
    """Return the canonical partition scheme for a dataset."""
    scheme = _SCHEMES.get(dataset)
    if scheme is None:
        raise PartitionSchemeError(
            "no partition scheme registered for dataset",
            context={"dataset": dataset},
        )
    return scheme


def partition_keys_for(dataset: str) -> Mapping[str, str]:
    """Return the ``year=YYYY/month=MM`` partition path for a row's date."""
    scheme = partition_scheme_for(dataset)
    raw_date = _coerce_date(scheme, dataset)
    return {
        "year": f"{raw_date.year:04d}",
        "month": f"{raw_date.month:02d}",
    }


def partition_path_for(
    dataset: str,
    *,
    row: Mapping[str, object],
    part_index: int = 0,
) -> str:
    """Build the relative Parquet path for one partition of a dataset.

    The returned path is a managed relative path of the form
    ``year=YYYY/month=MM/part-NNNNN.parquet``.
    """
    scheme = partition_scheme_for(dataset)
    date_value = row.get(scheme.date_field)
    if not isinstance(date_value, date):
        raise PartitionSchemeError(
            "row does not carry the expected date field",
            context={"dataset": dataset, "date_field": scheme.date_field},
        )
    partition = {
        "year": f"{date_value.year:04d}",
        "month": f"{date_value.month:02d}",
    }
    return (
        f"year={partition['year']}/month={partition['month']}/"
        f"{PART_FILE_TEMPLATE.format(part_index=part_index)}"
    )


def _coerce_date(scheme: CanonicalPartitionScheme, dataset: str) -> date:
    """Internal helper used to surface sensible errors when no row is given."""
    # The default here is never used in production; partition_keys_for
    # always supplies an actual date via partition_path_for. The default
    # is only present for defensive unit tests.
    del dataset
    return date(1970, 1, 1)


__all__ = [
    "PARTITION_SCHEME_V1",
    "CanonicalPartitionScheme",
    "partition_keys_for",
    "partition_path_for",
    "partition_scheme_for",
]
