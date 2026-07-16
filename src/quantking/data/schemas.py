"""Framework-free canonical schema descriptors for the six Stage 1 datasets.

These descriptors are the single source of truth for field names, python types,
units, primary keys, sort keys, nullability and schema versions. They intentionally
import no third-party library (no pyarrow, no pandas, no sqlalchemy) so that the
data domain contract stays framework-agnostic. The PyArrow mapping that turns a
descriptor into an executable ``pyarrow.Schema`` lives in
``quantking.data.adapters.canonical_schema``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class FieldType(StrEnum):
    """Python-level canonical field types mapped to physical types by the adapter."""

    CODE = "code"
    EXCHANGE = "exchange"
    DATE = "date"
    BOOL = "bool"
    INT64 = "int64"
    FLOAT64 = "float64"
    TEXT = "text"
    LISTING_STATUS = "listing_status"
    BOARD = "board"


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    """One canonical field with type, unit, nullability and key flags."""

    name: str
    field_type: FieldType
    unit: str | None = None
    nullable: bool = False
    primary_key: bool = False
    positive_required: bool = False
    non_negative_required: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("field name must be non-empty text")
        if not isinstance(self.field_type, FieldType):
            raise TypeError("field_type must be a FieldType")
        if self.unit is not None and (not isinstance(self.unit, str) or not self.unit.strip()):
            raise ValueError("unit must be non-empty text or None")
        if self.positive_required and self.non_negative_required:
            raise ValueError("field cannot require both positive and non-negative")


@dataclass(frozen=True, slots=True)
class DatasetSchemaDescriptor:
    """A framework-free contract for one canonical dataset."""

    name: str
    schema_version: str
    fields: tuple[FieldDescriptor, ...]
    primary_key: tuple[str, ...]
    sort_key: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("dataset name must be non-empty text")
        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version must be non-empty text")
        object.__setattr__(self, "fields", tuple(self.fields))
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        for pk in self.primary_key:
            if pk not in names:
                raise ValueError(f"primary key field {pk!r} not in fields")
        for sk in self.sort_key:
            if sk not in names:
                raise ValueError(f"sort key field {sk!r} not in fields")
        pk_set = set(self.primary_key)
        for field in self.fields:
            if field.name in pk_set and field.nullable:
                raise ValueError(f"primary key field {field.name!r} must not be nullable")
        # Mark primary-key membership on each field so callers can introspect it.
        normalized_fields = tuple(
            FieldDescriptor(
                name=field.name,
                field_type=field.field_type,
                unit=field.unit,
                nullable=field.nullable,
                primary_key=field.name in pk_set,
                positive_required=field.positive_required,
                non_negative_required=field.non_negative_required,
                description=field.description,
            )
            for field in self.fields
        )
        object.__setattr__(self, "fields", normalized_fields)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @classmethod
    def all(cls) -> Sequence[DatasetSchemaDescriptor]:
        """Return all six canonical dataset descriptors in stable order."""
        return _ALL_DESCRIPTORS

    @classmethod
    def security_master(cls) -> DatasetSchemaDescriptor:
        return _SECURITY_MASTER

    @classmethod
    def exchange_calendar(cls) -> DatasetSchemaDescriptor:
        return _EXCHANGE_CALENDAR

    @classmethod
    def daily_bar_unadjusted(cls) -> DatasetSchemaDescriptor:
        return _DAILY_BAR_UNADJUSTED

    @classmethod
    def adjustment_factor(cls) -> DatasetSchemaDescriptor:
        return _ADJUSTMENT_FACTOR

    @classmethod
    def trading_status(cls) -> DatasetSchemaDescriptor:
        return _TRADING_STATUS

    @classmethod
    def benchmark_index_daily(cls) -> DatasetSchemaDescriptor:
        return _BENCHMARK_INDEX_DAILY


_CANONICAL_CODE = re.compile(r"^\d{6}\.(SZ|SH|BJ)$")


def canonical_code_pattern() -> re.Pattern[str]:
    """The canonical A-share code pattern: ``000001.SZ``, ``600000.SH``, ``430047.BJ``."""
    return _CANONICAL_CODE


# ---------------------------------------------------------------------------
# Field reuse helpers
# ---------------------------------------------------------------------------

_CODE = FieldDescriptor(
    name="code",
    field_type=FieldType.CODE,
    description="Canonical 6-digit code with .SZ/.SH/.BJ exchange suffix.",
)
_TRADE_DATE = FieldDescriptor(
    name="trade_date",
    field_type=FieldType.DATE,
    description="Exchange-local trading day; not a timestamp.",
)
_EXCHANGE = FieldDescriptor(
    name="exchange",
    field_type=FieldType.EXCHANGE,
    description="Exchange suffix SZ/SH/BJ.",
)


def _ohlcv(
    *, volume_nullable: bool = False, amount_nullable: bool = False
) -> tuple[FieldDescriptor, ...]:
    return (
        FieldDescriptor(
            name="open",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            positive_required=True,
            description="Open price, unadjusted, RMB.",
        ),
        FieldDescriptor(
            name="high",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            positive_required=True,
            description="High price, unadjusted, RMB.",
        ),
        FieldDescriptor(
            name="low",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            positive_required=True,
            description="Low price, unadjusted, RMB.",
        ),
        FieldDescriptor(
            name="close",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            positive_required=True,
            description="Close price, unadjusted, RMB.",
        ),
        FieldDescriptor(
            name="pre_close",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            positive_required=True,
            description="Previous close, unadjusted, RMB.",
        ),
        FieldDescriptor(
            name="volume",
            field_type=FieldType.INT64,
            unit="share",
            non_negative_required=True,
            nullable=volume_nullable,
            description="Trade volume in shares.",
        ),
        FieldDescriptor(
            name="amount",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            non_negative_required=True,
            nullable=amount_nullable,
            description="Trade amount in RMB yuan.",
        ),
    )


# ---------------------------------------------------------------------------
# The six canonical datasets
# ---------------------------------------------------------------------------

_SECURITY_MASTER = DatasetSchemaDescriptor(
    name="security_master",
    schema_version="security_master.v1",
    fields=(
        _CODE,
        _EXCHANGE,
        FieldDescriptor(
            name="name", field_type=FieldType.TEXT, description="Security display name."
        ),
        FieldDescriptor(
            name="board",
            field_type=FieldType.BOARD,
            description="Board: MAIN, STAR, GEM, BSE, etc.",
        ),
        FieldDescriptor(
            name="listing_date", field_type=FieldType.DATE, description="Original listing date."
        ),
        FieldDescriptor(
            name="delisting_date",
            field_type=FieldType.DATE,
            nullable=True,
            description="Delisting date; null while still listed.",
        ),
        FieldDescriptor(
            name="effective_from",
            field_type=FieldType.DATE,
            primary_key=True,
            description="Point-in-time effective-from date for this record.",
        ),
        FieldDescriptor(
            name="effective_to",
            field_type=FieldType.DATE,
            nullable=True,
            description="Effective-to date; null for the current record.",
        ),
        FieldDescriptor(
            name="listing_status",
            field_type=FieldType.LISTING_STATUS,
            description="LISTED, SUSPENDED, DELISTED, etc.",
        ),
    ),
    primary_key=("code", "effective_from"),
    sort_key=("code", "effective_from"),
)

_EXCHANGE_CALENDAR = DatasetSchemaDescriptor(
    name="exchange_calendar",
    schema_version="exchange_calendar.v1",
    fields=(
        _EXCHANGE,
        FieldDescriptor(
            name="trade_date",
            field_type=FieldType.DATE,
            primary_key=True,
            description="Calendar date.",
        ),
        FieldDescriptor(
            name="is_open",
            field_type=FieldType.BOOL,
            description="True if the exchange is open for trading.",
        ),
    ),
    primary_key=("exchange", "trade_date"),
    sort_key=("exchange", "trade_date"),
)

_DAILY_BAR_UNADJUSTED = DatasetSchemaDescriptor(
    name="daily_bar_unadjusted",
    schema_version="daily_bar_unadjusted.v1",
    fields=(_CODE, _TRADE_DATE, *_ohlcv()),
    primary_key=("code", "trade_date"),
    sort_key=("code", "trade_date"),
)

_ADJUSTMENT_FACTOR = DatasetSchemaDescriptor(
    name="adjustment_factor",
    schema_version="adjustment_factor.v1",
    fields=(
        _CODE,
        _TRADE_DATE,
        FieldDescriptor(
            name="price_multiplier",
            field_type=FieldType.FLOAT64,
            positive_required=True,
            description=(
                "Multiply unadjusted price by this factor to obtain the normalized "
                "price on the normalization base day. Formula: p_adj = p_raw * "
                "price_multiplier."
            ),
        ),
        FieldDescriptor(
            name="volume_multiplier",
            field_type=FieldType.FLOAT64,
            positive_required=True,
            description=(
                "Multiply unadjusted volume by this factor to obtain normalized "
                "volume. Formula: v_adj = v_raw * volume_multiplier."
            ),
        ),
        FieldDescriptor(
            name="normalization_date",
            field_type=FieldType.DATE,
            description=(
                "Base day whose unadjusted prices are the normalization reference; "
                "multipliers are forward/backward applied relative to this day."
            ),
        ),
        FieldDescriptor(
            name="algorithm_version",
            field_type=FieldType.TEXT,
            description="Versioned复权 algorithm identifier; not a vendor name.",
        ),
    ),
    primary_key=("code", "trade_date"),
    sort_key=("code", "trade_date"),
)

_TRADING_STATUS = DatasetSchemaDescriptor(
    name="trading_status",
    schema_version="trading_status.v1",
    fields=(
        _CODE,
        _TRADE_DATE,
        FieldDescriptor(
            name="is_trading",
            field_type=FieldType.BOOL,
            description="True if the security traded on this day.",
        ),
        FieldDescriptor(
            name="is_suspended",
            field_type=FieldType.BOOL,
            description="True if officially suspended.",
        ),
        FieldDescriptor(
            name="is_st", field_type=FieldType.BOOL, description="True if in ST status."
        ),
        FieldDescriptor(
            name="is_star_st", field_type=FieldType.BOOL, description="True if in *ST status."
        ),
        FieldDescriptor(
            name="is_delisting_period",
            field_type=FieldType.BOOL,
            description="True if in delisting review period.",
        ),
        FieldDescriptor(
            name="limit_up_price",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            nullable=True,
            positive_required=False,
            description="Limit-up price; null when not applicable.",
        ),
        FieldDescriptor(
            name="limit_down_price",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            nullable=True,
            positive_required=False,
            description="Limit-down price; null when not applicable.",
        ),
        FieldDescriptor(
            name="status_reason",
            field_type=FieldType.TEXT,
            nullable=True,
            description="Free-text status reason; nullable.",
        ),
    ),
    primary_key=("code", "trade_date"),
    sort_key=("code", "trade_date"),
)

_BENCHMARK_INDEX_DAILY = DatasetSchemaDescriptor(
    name="benchmark_index_daily",
    schema_version="benchmark_index_daily.v1",
    fields=(
        _CODE,
        _TRADE_DATE,
        FieldDescriptor(
            name="open",
            field_type=FieldType.FLOAT64,
            positive_required=True,
            description="Index open.",
        ),
        FieldDescriptor(
            name="high",
            field_type=FieldType.FLOAT64,
            positive_required=True,
            description="Index high.",
        ),
        FieldDescriptor(
            name="low",
            field_type=FieldType.FLOAT64,
            positive_required=True,
            description="Index low.",
        ),
        FieldDescriptor(
            name="close",
            field_type=FieldType.FLOAT64,
            positive_required=True,
            description="Index close.",
        ),
        FieldDescriptor(
            name="pre_close",
            field_type=FieldType.FLOAT64,
            positive_required=True,
            description="Index previous close.",
        ),
        FieldDescriptor(
            name="volume",
            field_type=FieldType.INT64,
            unit="share",
            nullable=True,
            non_negative_required=True,
            description="Index volume; nullable because some indices lack it.",
        ),
        FieldDescriptor(
            name="amount",
            field_type=FieldType.FLOAT64,
            unit="cny_yuan",
            nullable=True,
            non_negative_required=True,
            description="Index amount; nullable because some indices lack it.",
        ),
    ),
    primary_key=("code", "trade_date"),
    sort_key=("code", "trade_date"),
)

_ALL_DESCRIPTORS: tuple[DatasetSchemaDescriptor, ...] = (
    _SECURITY_MASTER,
    _EXCHANGE_CALENDAR,
    _DAILY_BAR_UNADJUSTED,
    _ADJUSTMENT_FACTOR,
    _TRADING_STATUS,
    _BENCHMARK_INDEX_DAILY,
)

_DESCRIPTORS_BY_NAME = MappingProxyType({d.name: d for d in _ALL_DESCRIPTORS})


def descriptor_for(name: str) -> DatasetSchemaDescriptor:
    """Return the canonical descriptor for a dataset name."""
    if name not in _DESCRIPTORS_BY_NAME:
        raise KeyError(f"unknown canonical dataset: {name!r}")
    return _DESCRIPTORS_BY_NAME[name]
