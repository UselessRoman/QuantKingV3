"""S1-04-R3 tests for override provenance in Canonical manifests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.warehouse.canonical_warehouse import (
    CanonicalPublishRequest,
    CanonicalWarehouse,
)
from quantking.data.adapters.warehouse.errors import ManifestIntegrityError
from quantking.data.adapters.warehouse.manifest import WAREHOUSE_COMPLETE_FILENAME, compute_sha256
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
from quantking.data.models import DatasetKind, DatasetVersion


def _daily_table() -> pa.Table:
    raw = pa.Table.from_pylist(
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.8,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_800_000.0,
            }
        ]
    )
    schema = canonical_schema("daily_bar_unadjusted")
    return pa.Table.from_arrays(
        [raw.column(field.name).cast(field.type, safe=False) for field in schema],
        schema=pa.schema(
            [pa.field(field.name, field.type, nullable=field.nullable) for field in schema]
        ),
    )


def _version() -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-04-r3",
        rule_version="quality-rules.v1",
        parent_version_id=new_entity_id("data_version"),
        input_batch_ids=(new_entity_id("data_batch"), new_entity_id("data_batch")),
        created_at=utc_now(),
    )


def _provenance() -> dict[str, object]:
    contributions = [
        {
            "primary_key": {"code": "000001.SZ", "trade_date": "2026-07-15"},
            "kind": "FIELD_OVERRIDE",
            "fields": ["close"],
        }
    ]
    return {
        "override_id": str(new_entity_id("data_override")),
        "difference_report_id": str(new_entity_id("data_diff")),
        "report_sha256": "a" * 64,
        "primary": {
            "source": "EASTMONEY",
            "batch_id": str(new_entity_id("data_batch")),
            "version_id": str(new_entity_id("data_version")),
            "content_sha256": "b" * 64,
        },
        "fallback": {
            "source": "TENCENT",
            "batch_id": str(new_entity_id("data_batch")),
            "version_id": str(new_entity_id("data_version")),
            "content_sha256": "c" * 64,
        },
        "fallback_contributions": contributions,
        "contribution_scope_sha256": compute_sha256(
            json.dumps(
                contributions,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ),
    }


def test_override_manifest_provenance_is_signed_and_strictly_verified(tmp_path: Path) -> None:
    warehouse = CanonicalWarehouse(data_root=tmp_path)
    version = _version()
    provenance = _provenance()
    directory = warehouse.publish(
        CanonicalPublishRequest(
            version=version,
            tables=(_daily_table(),),
            code_version=version.code_version,
            rule_version=version.rule_version,
            source_override=provenance,
        )
    )

    verified = verify_canonical_directory(directory, data_root=tmp_path)
    assert verified.manifest["source_override"] == provenance

    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_override"]["fallback_contributions"][0]["fields"] = ["open"]
    without_sha = dict(manifest)
    without_sha.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = compute_sha256(
        json.dumps(without_sha, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    (directory / WAREHOUSE_COMPLETE_FILENAME).write_text(
        json.dumps(
            {"version_id": str(version.version_id), "manifest_sha256": manifest["manifest_sha256"]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestIntegrityError, match="contribution scope SHA"):
        verify_canonical_directory(directory, data_root=tmp_path)
