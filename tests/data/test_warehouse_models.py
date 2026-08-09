"""Domain contract tests for warehouse-specific value objects (Stage 1 S1-02)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from quantking.common.errors import InvalidPathError
from quantking.data.adapters.warehouse.errors import (
    CanonicalWarehouseError,
    PartitionSchemeError,
    RawStoreError,
    WarehouseError,
)
from quantking.data.adapters.warehouse.manifest import (
    CanonicalManifestFile,
    RawManifest,
    RawManifestFile,
    compute_sha256,
)
from quantking.data.adapters.warehouse.partitioning import (
    PARTITION_SCHEME_V1,
    partition_path_for,
    partition_scheme_for,
)


class TestRawManifestFile:
    def test_relative_path_must_be_managed(self) -> None:
        with pytest.raises(InvalidPathError):
            RawManifestFile(relative_path="/etc/passwd", payload=b"x", row_count=0)
        with pytest.raises(InvalidPathError):
            RawManifestFile(relative_path="../../escape", payload=b"x", row_count=0)
        with pytest.raises(InvalidPathError):
            RawManifestFile(relative_path="sub\0dir", payload=b"x", row_count=0)

    def test_row_count_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError):
            RawManifestFile(relative_path="rows/0.parquet", payload=b"x", row_count=-1)

    def test_payload_must_be_bytes(self) -> None:
        with pytest.raises(TypeError):
            RawManifestFile(relative_path="rows/0.parquet", payload="not-bytes", row_count=0)  # type: ignore[arg-type]


class TestCanonicalManifestFile:
    def test_partition_keys_must_be_strings(self) -> None:
        with pytest.raises(TypeError):
            CanonicalManifestFile(
                relative_path="year=2026/month=07/part-0.parquet",
                partition={"year": 2026, "month": 7},  # type: ignore[dict-item]
                size=10,
                row_count=0,
                sha256="0" * 64,
            )

    def test_sha256_must_be_64_hex(self) -> None:
        with pytest.raises(ValueError):
            CanonicalManifestFile(
                relative_path="year=2026/month=07/part-0.parquet",
                partition={"year": "2026", "month": "07"},
                size=10,
                row_count=0,
                sha256="bad",
            )

    def test_size_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError):
            CanonicalManifestFile(
                relative_path="year=2026/month=07/part-0.parquet",
                partition={"year": "2026", "month": "07"},
                size=-1,
                row_count=0,
                sha256="0" * 64,
            )


class TestRawManifestSerialization:
    def test_deterministic_json_ordering(self) -> None:
        manifest = RawManifest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-1",
            adapter_version="v1",
            request_params={"code": "000001.SZ", "start": "2026-01-01"},
            files=(
                RawManifestFile(relative_path="rows/0.parquet", payload=b"AAA", row_count=0),
                RawManifestFile(relative_path="rows/1.parquet", payload=b"BBB", row_count=0),
            ),
            created_at="2026-07-16T01:00:00Z",
        )
        encoded = manifest.to_canonical_json()
        # File entries are sorted by relative_path; metadata keys sorted
        # lexicographically.
        data = json.loads(encoded)
        assert [entry["relative_path"] for entry in data["files"]] == [
            "rows/0.parquet",
            "rows/1.parquet",
        ]
        assert list(data["request_params"].keys()) == ["code", "start"]
        # The same manifest must produce byte-identical output.
        again = manifest.to_canonical_json()
        assert again == encoded


class TestComputeSha256:
    def test_known_vector(self) -> None:
        assert compute_sha256(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        assert compute_sha256(b"abc") == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )


class TestPartitionScheme:
    def test_default_scheme_is_v1(self) -> None:
        scheme = partition_scheme_for("daily_bar_unadjusted")
        assert scheme.scheme_id == "trade_date_v1"
        assert scheme.scheme_version == PARTITION_SCHEME_V1

    def test_security_master_uses_effective_from_partition(self) -> None:
        scheme = partition_scheme_for("security_master")
        assert scheme.scheme_id == "effective_from_v1"
        relative = partition_path_for(
            "security_master",
            row={"effective_from": date(2026, 7, 15), "code": "000001.SZ"},
        )
        assert relative.startswith("year=2026/month=07/")

    def test_unknown_dataset_raises(self) -> None:
        with pytest.raises(PartitionSchemeError):
            partition_scheme_for("not_a_dataset")

    def test_relative_path_includes_partition_keys(self) -> None:
        relative = partition_path_for(
            "daily_bar_unadjusted",
            row={"trade_date": date(2026, 7, 15), "code": "000001.SZ"},
            part_index=0,
        )
        assert relative.startswith("year=2026/month=07/part-00000.parquet")


class TestWarehouseErrors:
    def test_error_hierarchy(self) -> None:
        assert issubclass(RawStoreError, WarehouseError)
        assert issubclass(CanonicalWarehouseError, WarehouseError)
        assert issubclass(PartitionSchemeError, WarehouseError)
