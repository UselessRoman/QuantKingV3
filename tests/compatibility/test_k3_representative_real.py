from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal

import pytest

from quantking.common.time import TradingDate
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.baostock import (
    BaostockDailyBarAdapter,
    BaostockMarketDataGateway,
)
from quantking.data.adapters.sources.baostock_worker import BAOSTOCK_FIELDS
from quantking.data.adapters.sources.contracts import AmountUnit, SupplierResponse, VolumeUnit
from quantking.data.adapters.sources.normalization import normalize_market_data_response
from quantking.data.comparison.k3_probe import (
    K3_REPRESENTATIVE_MAX_INVOCATIONS,
    K3_REPRESENTATIVE_PROBE_MARKER,
    K3ProbeSourceResult,
    fixed_k3_representative_probe_plan,
    summarize_k3_probe,
)
from quantking.data.incremental import FetchChunk
from quantking.data.models import DataProvider, DatasetKind, DataUpstream


@pytest.mark.real_k3_sources
def test_fixed_bounded_k3_representative_four_source_probe() -> None:
    """Run only after explicit marker selection and exact environment acknowledgement."""
    if os.environ.get("QUANTKING_K3_PROBE_AUTHORIZATION") != K3_REPRESENTATIVE_PROBE_MARKER:
        pytest.skip("exact K3 representative probe authorization marker is required")
    plan = fixed_k3_representative_probe_plan()
    assert len(plan) == K3_REPRESENTATIVE_MAX_INVOCATIONS == 24
    attempts = 0
    responses: list[SupplierResponse] = []
    records: dict[str, list[Mapping[str, object]]] = {role: [] for role in "ABCD"}
    upstream_counts = {role: 0 for role in "ABCD"}
    library_versions = {role: set[str]() for role in "ABCD"}
    contract_versions = {role: set[str]() for role in "ABCD"}
    volume_units = {role: set[str]() for role in "ABCD"}
    amount_units = {role: set[str]() for role in "ABCD"}
    official_preclose: dict[tuple[str, str], Decimal] = {}
    observed_isST: set[str] = set()
    for invocation in plan:
        chunk = FetchChunk(
            sequence=invocation.sequence,
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            scope=(invocation.symbol,),
            start=TradingDate(invocation.target_start),
            end=TradingDate(invocation.target_end),
            request_sha256="a" * 64,
        )
        attempts += 1
        if invocation.provider is DataProvider.BAOSTOCK:
            response = BaostockMarketDataGateway(BaostockDailyBarAdapter()).fetch(chunk)
        else:
            response = AkshareMarketDataGateway(AkshareDailyBarAdapter(invocation.upstream)).fetch(
                chunk
            )
        responses.append(response)
        contract = response.contract
        lineage = contract.lineage
        assert lineage.provider is invocation.provider
        assert lineage.upstream is invocation.upstream
        assert lineage.library_version == (
            "0.9.3" if invocation.provider is DataProvider.BAOSTOCK else "1.18.83"
        )
        assert contract.dataset is DatasetKind.DAILY_BAR_UNADJUSTED
        assert contract.volume_unit is not VolumeUnit.UNVERIFIED
        assert contract.amount_unit is AmountUnit.CNY_YUAN
        assert contract.unadjusted is True
        assert response.complete is True
        assert response.request_window.target_start == invocation.target_start
        assert response.request_window.target_end == invocation.target_end
        assert response.request_window.upstream_start == invocation.upstream_start
        assert response.request_window.upstream_end == invocation.upstream_end
        table = normalize_market_data_response(DatasetKind.DAILY_BAR_UNADJUSTED, response)
        records[invocation.role].extend(table.to_pylist())
        upstream_counts[invocation.role] += response.upstream_row_count
        library_versions[invocation.role].add(lineage.library_version)
        contract_versions[invocation.role].add(contract.contract_version)
        volume_units[invocation.role].add(contract.volume_unit.value)
        amount_units[invocation.role].add(contract.amount_unit.value)
        if invocation.provider is DataProvider.BAOSTOCK:
            raw_envelope = json.loads(response.raw_payload)
            provider_payload = raw_envelope["provider_payload"]
            assert provider_payload["fields"] == list(BAOSTOCK_FIELDS)
            raw_rows = provider_payload["rows"]
            assert isinstance(raw_rows, list)
            date_index = BAOSTOCK_FIELDS.index("date")
            preclose_index = BAOSTOCK_FIELDS.index("preclose")
            isst_index = BAOSTOCK_FIELDS.index("isST")
            for row in raw_rows:
                assert isinstance(row, list)
                day = str(row[date_index])
                observed_isST.add(str(row[isst_index]))
                if invocation.target_start.isoformat() <= day <= invocation.target_end.isoformat():
                    official_preclose[(invocation.symbol, day)] = Decimal(str(row[preclose_index]))
    assert attempts == K3_REPRESENTATIVE_MAX_INVOCATIONS
    assert len(responses) == K3_REPRESENTATIVE_MAX_INVOCATIONS
    results = tuple(
        K3ProbeSourceResult(
            role=role,
            provider=provider,
            upstream=upstream,
            library_versions=tuple(sorted(library_versions[role])),
            contract_versions=tuple(sorted(contract_versions[role])),
            volume_units=tuple(sorted(volume_units[role])),
            amount_units=tuple(sorted(amount_units[role])),
            unadjusted=True,
            complete=True,
            upstream_row_count=upstream_counts[role],
            records=tuple(records[role]),
            official_preclose=official_preclose if role == "A" else None,
            observed_isST=tuple(sorted(observed_isST)) if role == "A" else None,
        )
        for role, provider, upstream in (
            ("A", DataProvider.BAOSTOCK, DataUpstream.BAOSTOCK),
            ("B", DataProvider.AKSHARE, DataUpstream.EASTMONEY),
            ("C", DataProvider.AKSHARE, DataUpstream.TENCENT),
            ("D", DataProvider.AKSHARE, DataUpstream.SINA),
        )
    )
    summary = summarize_k3_probe(
        results,
        marker=K3_REPRESENTATIVE_PROBE_MARKER,
        schema_version="s1-07k3b-representative-evidence.v1",
    )
    print(
        json.dumps(
            dict(summary),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
