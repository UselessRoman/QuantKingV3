"""Project a validated BaoStock trading-day series onto the canonical calendar.

This module owns the only pyarrow touch point of the S1-08R3-C1 calendar
bootstrap; the ``data.eod`` orchestration stays pyarrow-free and calls in.
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa

from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.models import DatasetKind

CALENDAR_PROJECTION_EXCHANGES: tuple[str, ...] = ("SH", "SZ")


def project_exchange_calendar_table(
    days: tuple[tuple[str, str], ...],
    *,
    exchanges: tuple[str, ...] = CALENDAR_PROJECTION_EXCHANGES,
) -> pa.Table:
    """Project one validated day series onto the canonical exchange schema."""
    rows: list[dict[str, object]] = []
    for exchange in exchanges:
        for day, flag in sorted(days):
            rows.append(
                {
                    "exchange": exchange,
                    "trade_date": date.fromisoformat(day),
                    "is_open": flag == "1",
                }
            )
    raw = pa.Table.from_pylist(rows)
    descriptor = canonical_schema(DatasetKind.EXCHANGE_CALENDAR.value)
    return pa.Table.from_arrays(
        [raw.column(field.name).cast(field.type, safe=False) for field in descriptor],
        schema=pa.schema(
            [pa.field(field.name, field.type, nullable=field.nullable) for field in descriptor]
        ),
    )


__all__ = [
    "CALENDAR_PROJECTION_EXCHANGES",
    "project_exchange_calendar_table",
]
