from __future__ import annotations

import json
import os

import pytest

from quantking.common.time import TradingDate
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.contracts import SourceAdapterError
from quantking.data.comparison.k3_connectivity import (
    K3_CONNECTIVITY_MAX_INVOCATIONS,
    K3_CONNECTIVITY_PROBE_MARKER,
    K3ConnectivityOutcome,
    fixed_k3_connectivity_plan,
    summarize_k3_connectivity,
)
from quantking.data.incremental import FetchChunk
from quantking.data.models import DatasetKind


@pytest.mark.real_k3_sources
def test_fixed_bounded_k3_akshare_connectivity_probe() -> None:
    """Run all three frozen minimal calls once each; record every outcome."""
    if os.environ.get("QUANTKING_K3_PROBE_AUTHORIZATION") != K3_CONNECTIVITY_PROBE_MARKER:
        pytest.skip("exact K3 connectivity probe authorization marker is required")
    plan = fixed_k3_connectivity_plan()
    assert len(plan) == K3_CONNECTIVITY_MAX_INVOCATIONS == 3
    outcomes: list[K3ConnectivityOutcome] = []
    for invocation in plan:
        chunk = FetchChunk(
            sequence=invocation.sequence,
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            scope=(invocation.symbol,),
            start=TradingDate(invocation.target_date),
            end=TradingDate(invocation.target_date),
            request_sha256="a" * 64,
        )
        try:
            response = AkshareMarketDataGateway(AkshareDailyBarAdapter(invocation.upstream)).fetch(
                chunk
            )
        except SourceAdapterError as error:
            outcomes.append(
                K3ConnectivityOutcome(
                    upstream=invocation.upstream,
                    succeeded=False,
                    stable_code=error.code.value,
                    message=str(error),
                    complete=None,
                    canonical_row_count=None,
                    library_version=None,
                    volume_unit=None,
                    amount_unit=None,
                )
            )
            continue
        contract = response.contract
        outcomes.append(
            K3ConnectivityOutcome(
                upstream=invocation.upstream,
                succeeded=True,
                stable_code=None,
                message=None,
                complete=response.complete,
                canonical_row_count=len(response.records),
                library_version=contract.lineage.library_version,
                volume_unit=contract.volume_unit.value,
                amount_unit=contract.amount_unit.value,
            )
        )
    summary = summarize_k3_connectivity(tuple(outcomes))
    print(
        json.dumps(
            dict(summary),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
