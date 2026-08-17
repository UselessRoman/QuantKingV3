"""One-shot authorized real K4D pilot executor (skipped by default).

The executor refuses to run unless the user supplies the frozen marker, a
fresh authorization id, a fixed attempt directory, and the idempotency key
through environment variables. It then reads exactly the six inventoried
legacy files, performs the 18 frozen A/B/C calls with fail-fast semantics,
confirms the K4D semantics, and writes one immutable pilot batch into
``E:/QuantKingV3Data``. Nothing here runs in the default offline suite.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from quantking.common.time import TradingDate
from quantking.data.adapters.legacy_pilot import (
    K4DServiceFetchResult,
    LegacyPilotExecutor,
    PilotInvocationJournal,
    verify_legacy_pilot_batch,
)
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.baostock import (
    BaostockDailyBarAdapter,
    BaostockMarketDataGateway,
)
from quantking.data.adapters.sources.contracts import FetchChunk
from quantking.data.adapters.sources.normalization import normalize_market_data_response
from quantking.data.legacy_import.contracts import (
    K4D_PILOT_REAL_MARKER,
    LegacyImportAuthorization,
    LegacyImportMode,
    inventory_from_payload,
)
from quantking.data.legacy_import.k4d_pilot import (
    K4D_ENDPOINT_CALL_BUDGET,
    K4D_PILOT_SYMBOLS,
    K4D_RECHECK_WINDOW_END,
    K4D_RECHECK_WINDOW_START,
    K4DPilotPlan,
)
from quantking.data.models import DataProvider, DatasetKind, DataUpstream

_ROOT = Path(__file__).resolve().parents[2]
_PLAN_FILE = _ROOT / "docs/stage1/evidence/k4d_pilot_dry_run_2026-08-16.json"
_INVENTORY_FILE = _ROOT / "docs/stage1/evidence/k4d_inventory_2026-08-16.json"
_ENV_MARKER = "QUANTKING_K4D_PILOT_AUTHORIZATION"
_ENV_AUTHORIZATION_ID = "QUANTKING_K4D_PILOT_AUTHORIZATION_ID"
_ENV_ATTEMPT_DIR = "QUANTKING_K4D_PILOT_ATTEMPT_DIR"
_ENV_IDEMPOTENCY_KEY = "QUANTKING_K4D_PILOT_IDEMPOTENCY_KEY"
_REAL_SOURCE_ROOT = Path(r"E:\QuantKing\data\kline\1d")
_REAL_STAGING_ROOT = Path(r"E:\QuantKingV3Data")
_WINDOW_START = date.fromisoformat(K4D_RECHECK_WINDOW_START)
_WINDOW_END = date.fromisoformat(K4D_RECHECK_WINDOW_END)


def _request_sha256(provider: DataProvider, upstream: DataUpstream, symbol: str) -> str:
    payload = {
        "provider": provider.value,
        "upstream": upstream.value,
        "symbol": symbol,
        "target_start": _WINDOW_START.isoformat(),
        "target_end": _WINDOW_END.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _vendor_pre_close(raw_payload: bytes, symbol: str) -> dict[tuple[str, str], float]:
    envelope = json.loads(raw_payload)
    provider_payload = envelope["provider_payload"]
    fields = provider_payload["fields"]
    rows = provider_payload["rows"]
    date_index = fields.index("date")
    preclose_index = fields.index("preclose")
    vendor: dict[tuple[str, str], float] = {}
    for row in rows:
        value = row[preclose_index]
        if value in ("", None):
            continue
        vendor[(symbol, str(row[date_index])[:10])] = float(value)
    return vendor


@pytest.mark.real_k4d_pilot
def test_authorized_one_shot_k4d_pilot() -> None:
    if os.environ.get(_ENV_MARKER) != K4D_PILOT_REAL_MARKER:
        pytest.skip("K4D pilot requires the frozen marker plus a fresh authorization id")
    authorization_id = os.environ.get(_ENV_AUTHORIZATION_ID)
    if not authorization_id:
        pytest.skip("K4D pilot requires QUANTKING_K4D_PILOT_AUTHORIZATION_ID")
    attempt_dir_value = os.environ.get(_ENV_ATTEMPT_DIR)
    if not attempt_dir_value:
        pytest.skip("K4D pilot requires QUANTKING_K4D_PILOT_ATTEMPT_DIR")
    idempotency_key = os.environ.get(_ENV_IDEMPOTENCY_KEY, "k4d-pilot-20260816-v1")
    attempt_dir = Path(attempt_dir_value)

    plan_payload = json.loads(_PLAN_FILE.read_bytes())
    plan = K4DPilotPlan(payload=plan_payload)
    inventory_payload = json.loads(_INVENTORY_FILE.read_bytes())
    inventory = inventory_from_payload(inventory_payload)

    calls = {"count": 0}

    def fetcher(symbol: str, journal: PilotInvocationJournal) -> K4DServiceFetchResult:
        tables: dict[DataUpstream, pa.Table] = {}
        vendor: dict[tuple[str, str], float] = {}
        evidence: dict[str, Any] = {}
        for provider, upstream, gateway in (
            (
                DataProvider.BAOSTOCK,
                DataUpstream.BAOSTOCK,
                BaostockMarketDataGateway(BaostockDailyBarAdapter()),
            ),
            (
                DataProvider.AKSHARE,
                DataUpstream.TENCENT,
                AkshareMarketDataGateway(AkshareDailyBarAdapter(DataUpstream.TENCENT)),
            ),
            (
                DataProvider.AKSHARE,
                DataUpstream.SINA,
                AkshareMarketDataGateway(AkshareDailyBarAdapter(DataUpstream.SINA)),
            ),
        ):
            calls["count"] += 1
            if calls["count"] > K4D_ENDPOINT_CALL_BUDGET:
                raise RuntimeError("K4D pilot exceeded its 18-call budget")
            chunk = FetchChunk(
                sequence=calls["count"],
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                scope=(symbol,),
                start=TradingDate(_WINDOW_START),
                end=TradingDate(_WINDOW_END),
                request_sha256=_request_sha256(provider, upstream, symbol),
            )
            try:
                response = gateway.fetch(chunk)  # fail-fast: any error stops the attempt
            except Exception as error:
                # Journal the failing endpoint with its stable, redacted
                # source error code before the fail-fast abort.
                journal.record_endpoint_failure(
                    symbol=symbol,
                    provider=provider.value,
                    upstream=upstream.value,
                    error=error,
                )
                raise
            table = normalize_market_data_response(DatasetKind.DAILY_BAR_UNADJUSTED, response)
            journal.record_endpoint_success(
                symbol=symbol,
                provider=provider.value,
                upstream=upstream.value,
                row_count=table.num_rows,
                raw_payload_sha256=hashlib.sha256(response.raw_payload).hexdigest(),
                library_version=response.contract.lineage.library_version,
            )
            tables[upstream] = table
            evidence[upstream.value] = {
                "row_count": table.num_rows,
                "raw_payload_sha256": hashlib.sha256(response.raw_payload).hexdigest(),
                "library_version": response.contract.lineage.library_version,
            }
            if upstream is DataUpstream.BAOSTOCK:
                vendor = _vendor_pre_close(response.raw_payload, symbol)
        return K4DServiceFetchResult(
            symbol=symbol,
            canonical=tables,
            vendor_pre_close=vendor,
            evidence=evidence,
        )

    executor = LegacyPilotExecutor(
        authorization=LegacyImportAuthorization(
            source_root=_REAL_SOURCE_ROOT,
            staging_root=_REAL_STAGING_ROOT,
            mode=LegacyImportMode.PILOT,
            marker=K4D_PILOT_REAL_MARKER,
            pilot_symbols=K4D_PILOT_SYMBOLS,
        ),
        plan=plan,
        inventory_payload=inventory_payload,
        fetcher=fetcher,
    )
    outcome = executor.run(
        authorization_id=authorization_id,
        attempt_dir=attempt_dir,
        idempotency_key=idempotency_key,
    )
    assert calls["count"] == K4D_ENDPOINT_CALL_BUDGET
    assert inventory.source_fingerprint == plan_payload["source_fingerprint"]
    manifest, manifest_sha = verify_legacy_pilot_batch(
        outcome.directory, staging_root=_REAL_STAGING_ROOT
    )
    assert manifest_sha == outcome.manifest_sha256
    assert manifest["warehouse_published"] is False
    evidence_path = attempt_dir / "pilot-evidence.json"
    evidence_path.write_bytes(
        json.dumps(
            {
                "schema_version": "legacy-pilot-evidence.k4d.v1",
                "authorization_id": authorization_id,
                "plan_sha256": plan.plan_sha256,
                "inventory_sha256": plan_payload["inventory_sha256"],
                "batch_id": outcome.batch_id,
                "manifest_sha256": manifest_sha,
                "endpoint_calls": calls["count"],
                "canonical_row_count": outcome.canonical_row_count,
                "raw_row_count": outcome.raw_row_count,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
