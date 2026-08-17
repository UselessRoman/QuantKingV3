"""Returned-value unit policy frozen from AKShare 1.18.83 evidence."""

from __future__ import annotations

from dataclasses import dataclass

from quantking.data.adapters.sources.contracts import AmountUnit, VolumeUnit
from quantking.data.adapters.sources.parameters import is_supported_a_share
from quantking.data.models import DataUpstream

BAOSTOCK_UNIT_POLICY_VERSION = "baostock-0.9.3-returned-units.v1"
AKSHARE_UNIT_POLICY_VERSION = "akshare-1.18.83-returned-units.v1"
_VERIFIED_AKSHARE_VERSION = "1.18.83"


def upstream_unit_policy_version(upstream: DataUpstream) -> str:
    """Return the fixed returned-unit policy version bound to one runtime source."""
    if upstream is DataUpstream.BAOSTOCK:
        return BAOSTOCK_UNIT_POLICY_VERSION
    if upstream in {DataUpstream.TENCENT, DataUpstream.SINA}:
        return AKSHARE_UNIT_POLICY_VERSION
    raise ValueError("upstream has no locked runtime unit policy")


@dataclass(frozen=True, slots=True)
class ReturnedUnitPolicy:
    """Resolved interpretation of values returned by one locked endpoint call."""

    policy_version: str
    volume_unit: VolumeUnit
    amount_unit: AmountUnit

    @property
    def verified(self) -> bool:
        return (
            self.volume_unit is not VolumeUnit.UNVERIFIED
            and self.amount_unit is not AmountUnit.UNVERIFIED
        )


_UNVERIFIED = ReturnedUnitPolicy(
    policy_version=AKSHARE_UNIT_POLICY_VERSION,
    volume_unit=VolumeUnit.UNVERIFIED,
    amount_unit=AmountUnit.UNVERIFIED,
)


def resolve_returned_units(
    upstream: DataUpstream,
    symbol: str,
    library_version: str,
) -> ReturnedUnitPolicy:
    """Resolve units only for the locked package and explicit K1 A-share code ranges."""
    if not isinstance(upstream, DataUpstream):
        raise TypeError("upstream must be a DataUpstream")
    if not isinstance(symbol, str):
        raise TypeError("symbol must be text")
    if not isinstance(library_version, str):
        raise TypeError("library_version must be text")
    if library_version != _VERIFIED_AKSHARE_VERSION or not is_supported_a_share(symbol):
        return _UNVERIFIED
    if upstream is DataUpstream.SINA:
        volume_unit = VolumeUnit.SHARE
    elif upstream is DataUpstream.TENCENT and symbol.endswith(".SZ") and symbol.startswith("000"):
        # AKShare 1.18.83 excludes sz000* from its Tencent volume * 100 branch.
        volume_unit = VolumeUnit.LOT_100_SHARES
    elif upstream is DataUpstream.TENCENT:
        volume_unit = VolumeUnit.SHARE
    else:
        return _UNVERIFIED
    return ReturnedUnitPolicy(
        policy_version=AKSHARE_UNIT_POLICY_VERSION,
        volume_unit=volume_unit,
        amount_unit=AmountUnit.CNY_YUAN,
    )


__all__ = [
    "AKSHARE_UNIT_POLICY_VERSION",
    "BAOSTOCK_UNIT_POLICY_VERSION",
    "ReturnedUnitPolicy",
    "resolve_returned_units",
    "upstream_unit_policy_version",
]
