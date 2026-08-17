"""K4D legacy X vs live A/B/C recheck engine (pure, offline-safe).

Staged legacy rows are DATA_ONLY history. Before any historical row may be
published it must be supported by two actual independent sources: X plus a
live source that agrees, or a live two-source consensus that replaces X.
Rows without such support stay unverified or quarantined — they are never
silently dropped and never published. The engine consumes the same versioned
Decimal ``ThresholdSet`` as DifferenceReport and Resolution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from types import MappingProxyType

import pyarrow as pa

from quantking.data.adapters.canonical_schema import validate_table
from quantking.data.comparison.thresholds import (
    ThresholdSet,
    rows_within_threshold,
)
from quantking.data.legacy_import.contracts import LegacyResolutionEvidenceSummary
from quantking.data.models import DatasetKind, DataUpstream

_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
_SERVICE_ORDER = (DataUpstream.BAOSTOCK, DataUpstream.TENCENT, DataUpstream.SINA)
Key = tuple[str, str]


def _index_rows(table: pa.Table) -> dict[Key, Mapping[str, object]]:
    rows: dict[Key, Mapping[str, object]] = {}
    for row in table.to_pylist():
        key: Key = (str(row["code"]), str(row["trade_date"]))
        if key in rows:
            raise ValueError("recheck table contains a duplicate primary key")
        rows[key] = MappingProxyType(dict(row))
    return rows


def _validate_input(label: str, table: pa.Table) -> None:
    if not isinstance(table, pa.Table):
        raise TypeError(f"{label} must be a pyarrow Table")
    if table.num_rows == 0:
        # A source with no held rows is a valid zero-row side; the canonical
        # schema must still match exactly so no silent drift is possible.
        from quantking.data.adapters.canonical_schema import canonical_schema

        if table.schema != canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value):
            raise ValueError(f"{label} is not the canonical daily-bar schema")
        return
    validation = validate_table(DatasetKind.DAILY_BAR_UNADJUSTED.value, table)
    if not validation.ok:
        raise ValueError(f"{label} is not a canonical daily-bar table")


@dataclass(frozen=True, slots=True)
class LegacyRecheckOutcome:
    evidence: LegacyResolutionEvidenceSummary
    x_row_count: int
    service_held_keys: int
    replaced_keys: int
    quarantined_keys: int

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "legacy-recheck.k4d.v1",
            "x_row_count": self.x_row_count,
            "service_held_keys": self.service_held_keys,
            "replaced_keys": self.replaced_keys,
            "quarantined_keys": self.quarantined_keys,
            "evidence": self.evidence.to_payload(),
        }


class LegacyRecheckEngine:
    """Classify every staged legacy row against live A/B/C held rows.

    The threshold rule is explicit and mandatory: K4D paths must bind
    ``comparison-thresholds.daily-bar-unadjusted.k4.v1`` (0/50); there is no
    silent default so the K1 zero-tolerance set can never leak into a pilot
    recheck by accident.
    """

    def __init__(self, *, thresholds: ThresholdSet) -> None:
        if not isinstance(thresholds, ThresholdSet):
            raise TypeError("thresholds must be an explicit ThresholdSet")
        self._thresholds = thresholds

    def reevaluate(
        self,
        *,
        x_table: pa.Table,
        service_tables: Mapping[DataUpstream, pa.Table],
    ) -> LegacyRecheckOutcome:
        _validate_input("legacy X table", x_table)
        if not isinstance(service_tables, Mapping) or any(
            not isinstance(upstream, DataUpstream)
            or upstream not in _SERVICE_ORDER
            or not isinstance(table, pa.Table)
            for upstream, table in service_tables.items()
        ):
            raise ValueError("service_tables must map supported sources to canonical tables")
        for upstream, table in service_tables.items():
            _validate_input(f"{upstream.value} service table", table)
        service_rows: dict[DataUpstream, Mapping[Key, Mapping[str, object]]] = {
            upstream: _index_rows(table) for upstream, table in service_tables.items()
        }
        service_keys: set[Key] = (
            set().union(*(rows.keys() for rows in service_rows.values())) if service_rows else set()
        )
        x_rows = _index_rows(x_table)
        x_a_consensus = 0
        service_replaced = 0
        quarantined = 0
        x_only = 0
        replaced_keys = 0
        quarantined_keys = 0
        for key, x_row in x_rows.items():
            held: dict[DataUpstream, Mapping[str, object]] = {}
            for upstream in _SERVICE_ORDER:
                row = service_rows.get(upstream, {}).get(key)
                if row is not None:
                    held[upstream] = row
            if not held:
                x_only += 1
                continue
            a_row = held.get(DataUpstream.BAOSTOCK)
            if a_row is not None and rows_within_threshold(self._thresholds, x_row, a_row, _FIELDS):
                x_a_consensus += 1
                continue
            agreeing_pair = next(
                (
                    (left, right)
                    for left, right in combinations(_SERVICE_ORDER, 2)
                    if left in held
                    and right in held
                    and rows_within_threshold(self._thresholds, held[left], held[right], _FIELDS)
                ),
                None,
            )
            if agreeing_pair is not None:
                service_replaced += 1
                replaced_keys += 1
                continue
            quarantined += 1
            quarantined_keys += 1
        evidence = LegacyResolutionEvidenceSummary(
            input_rows=len(x_rows),
            x_only_unverified_rows=x_only,
            x_a_consensus_rows=x_a_consensus,
            service_consensus_replaced_x_rows=service_replaced,
            quarantined_rows=quarantined,
        )
        return LegacyRecheckOutcome(
            evidence=evidence,
            x_row_count=len(x_rows),
            service_held_keys=len(service_keys),
            replaced_keys=replaced_keys,
            quarantined_keys=quarantined_keys,
        )


__all__ = ["LegacyRecheckEngine", "LegacyRecheckOutcome"]
