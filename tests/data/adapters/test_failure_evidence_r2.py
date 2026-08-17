"""S1-08R2 A6: failed batch evidence stays sanitized and bounded."""

from __future__ import annotations

import json
from pathlib import Path

from quantking.data.adapters.warehouse.raw_store import (
    RawBatchFailureRequest,
    RawBatchPublisher,
)


def test_record_failure_writes_sanitized_immutable_evidence(tmp_path: Path) -> None:
    publisher = RawBatchPublisher(data_root=tmp_path)
    request = RawBatchFailureRequest(
        source="akshare_tencent",
        dataset="daily_bar_unadjusted",
        batch_id="batch-0001",
        adapter_version="baostock-akshare-daily-bar.k4.v1",
        request_params={
            "request_sha256": "a" * 64,
        },
        error_code="AKSHARE_NETWORK",
        failed_at="2026-08-17T00:00:00Z",
        code_version="s1-08r2.v1",
    )

    evidence = publisher.record_failure(request)

    failure_path = evidence / "failure.json"
    record = json.loads(failure_path.read_text(encoding="utf-8"))
    serialized = json.dumps(record)
    assert record["error_code"] == "AKSHARE_NETWORK"
    assert record["batch_id"] == "batch-0001"
    assert "E:\\private" not in serialized
    assert "token=hunter2" not in serialized
    assert "Traceback" not in serialized
    assert record["message"] == "raw attempt failed before supplier payload was available"

    # The failed attempt never touches the warehouse root.
    assert not (tmp_path / "warehouse").exists()

    # Evidence is immutable: the same batch id cannot be rewritten.
    from quantking.data.adapters.warehouse.raw_store import RawStoreError

    try:
        publisher.record_failure(request)
    except RawStoreError:
        pass
    else:
        raise AssertionError("duplicate failure evidence must be rejected")
    again = json.loads(failure_path.read_text(encoding="utf-8"))
    assert again == record
