from __future__ import annotations

from datetime import date

import pytest

from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    VolumeUnit,
)
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.units import (
    AKSHARE_UNIT_POLICY_VERSION,
    resolve_returned_units,
)
from quantking.data.models import DataUpstream

_SUPPORTED_SYMBOLS = (
    "600000.SH",
    "601001.SH",
    "603001.SH",
    "605001.SH",
    "688001.SH",
    "000001.SZ",
    "001001.SZ",
    "002001.SZ",
    "003001.SZ",
    "300001.SZ",
    "301001.SZ",
)


@pytest.mark.parametrize("symbol", _SUPPORTED_SYMBOLS)
def test_frozen_policy_resolves_eastmoney_lots_and_sina_shares(symbol: str) -> None:
    eastmoney = resolve_returned_units(DataUpstream.EASTMONEY, symbol, "1.18.83")
    sina = resolve_returned_units(DataUpstream.SINA, symbol, "1.18.83")

    assert eastmoney.policy_version == AKSHARE_UNIT_POLICY_VERSION
    assert eastmoney.volume_unit is VolumeUnit.LOT_100_SHARES
    assert eastmoney.amount_unit is AmountUnit.CNY_YUAN
    assert eastmoney.verified is True
    assert sina.volume_unit is VolumeUnit.SHARE
    assert sina.amount_unit is AmountUnit.CNY_YUAN
    assert sina.verified is True


@pytest.mark.parametrize("symbol", _SUPPORTED_SYMBOLS)
def test_tencent_frozen_policy_preserves_000_sz_lots_only(symbol: str) -> None:
    resolved = resolve_returned_units(DataUpstream.TENCENT, symbol, "1.18.83")

    expected = VolumeUnit.LOT_100_SHARES if symbol.startswith("000") else VolumeUnit.SHARE
    assert resolved.volume_unit is expected
    assert resolved.amount_unit is AmountUnit.CNY_YUAN
    assert resolved.verified is True


@pytest.mark.parametrize(
    "symbol",
    (
        "900901.SH",
        "200001.SZ",
        "000001.SH",
        "399001.SZ",
        "689001.SH",
        "430001.BJ",
    ),
)
def test_non_k1_code_segments_are_rejected_and_never_assigned_units(symbol: str) -> None:
    for upstream in DataUpstream:
        resolved = resolve_returned_units(upstream, symbol, "1.18.83")
        assert resolved.volume_unit is VolumeUnit.UNVERIFIED
        assert resolved.amount_unit is AmountUnit.UNVERIFIED
        assert resolved.verified is False

    with pytest.raises(SourceAdapterError) as caught:
        DailyBarRequest(symbol, date(2026, 8, 1), date(2026, 8, 8))
    assert caught.value.code is SourceErrorCode.REQUEST_REJECTED


def test_unverified_package_version_never_reuses_the_frozen_policy() -> None:
    for upstream in DataUpstream:
        resolved = resolve_returned_units(upstream, "600000.SH", "1.18.84")
        assert resolved.volume_unit is VolumeUnit.UNVERIFIED
        assert resolved.amount_unit is AmountUnit.UNVERIFIED
        assert resolved.verified is False
