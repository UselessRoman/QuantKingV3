"""Auditable XTQuant read-only capability matrix for S1-03.

The current evidence is limited to the public Python source/signature already
present on the machine. A callable name alone does not prove field units,
point-in-time history, response availability, or QMT connectivity.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantking.data.models import DatasetKind


@dataclass(frozen=True, slots=True)
class XtQuantCapability:
    """One dataset's evidence-backed public-interface capability."""

    dataset: DatasetKind
    public_interface: str
    required_supplier_fields: tuple[str, ...]
    field_basis: str
    deterministic_derivations: tuple[str, ...]
    available: bool
    gap: str | None


XTQUANT_CAPABILITIES: tuple[XtQuantCapability, ...] = (
    XtQuantCapability(
        DatasetKind.SECURITY_MASTER,
        "get_instrument_detail_list",
        (
            "ExchangeID",
            "InstrumentID",
            "InstrumentName",
            "OpenDate",
            "ExpireDate",
        ),
        "public source documents current instrument fields only",
        (),
        False,
        "current instrument details do not prove point-in-time master-data history",
    ),
    XtQuantCapability(
        DatasetKind.EXCHANGE_CALENDAR,
        "get_trading_dates",
        ("market", "start_time", "end_time", "open-date timestamps"),
        "public source documents inclusive date parameters and open-date timestamps",
        ("closed dates are the complement of returned open dates inside the explicit range",),
        True,
        None,
    ),
    XtQuantCapability(
        DatasetKind.DAILY_BAR_UNADJUSTED,
        "get_market_data_ex",
        ("time", "open", "high", "low", "close", "pre_close", "volume", "amount"),
        "public source documents OHLC/volume/amount names but not required units or pre_close",
        (),
        False,
        "pre_close and volume/amount unit semantics are not verified",
    ),
    XtQuantCapability(
        DatasetKind.ADJUSTMENT_FACTOR,
        "get_divid_factors",
        ("event date", "price multiplier", "volume multiplier", "normalization base"),
        "public source exposes a dataframe but does not document returned columns or formula",
        (),
        False,
        "factor formula, base date, columns and algorithm semantics are not verified",
    ),
    XtQuantCapability(
        DatasetKind.TRADING_STATUS,
        "get_instrument_detail",
        ("historical official suspension", "ST", "*ST", "delisting period"),
        "public source documents current IsTrading/InstrumentStatus only",
        (),
        False,
        "historical official status and ST/delisting-period fields are not verified",
    ),
    XtQuantCapability(
        DatasetKind.BENCHMARK_INDEX_DAILY,
        "get_market_data_ex",
        ("time", "open", "high", "low", "close", "pre_close", "volume", "amount"),
        "public source does not distinguish index unit/nullability semantics",
        (),
        False,
        "pre_close and optional index volume/amount semantics are not verified",
    ),
)


def capability_for(dataset: DatasetKind) -> XtQuantCapability:
    for capability in XTQUANT_CAPABILITIES:
        if capability.dataset is dataset:
            return capability
    raise KeyError(dataset)


__all__ = ["XTQUANT_CAPABILITIES", "XtQuantCapability", "capability_for"]
