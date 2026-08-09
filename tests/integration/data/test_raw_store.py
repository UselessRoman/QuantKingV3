"""Raw batch landing contract tests (Stage 1 S1-02).

The raw store is the only location that physically writes supplier material. It
must:

* Land under a managed root with safe relative paths and no path traversal.
* Refuse absolute paths, symlink/junction escapes and parent-traversal.
* Treat the entire raw directory as append-only; no in-place rewrite, no delete.
* Bind a deterministic manifest that lists every file with its managed relative
  path, size, row count and SHA-256.
* Atomically move an ``.incomplete`` directory to its final name and write
  ``COMPLETE`` only after the manifest is fully written and re-verified.
* Return the same final directory on a duplicate successful publish and raise a
  stable conflict when the contents differ.
* Preserve failure evidence: a failed write keeps the ``.incomplete`` directory,
  a ``failure.json`` and the input listing; retries must use a new ``batch_id``.

All tests below use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

from quantking.common.errors import IdempotencyConflictError, InvalidPathError
from quantking.data.adapters.warehouse.errors import RawStoreError
from quantking.data.adapters.warehouse.manifest import (
    RAW_COMPLETE_FILENAME,
    RAW_FAILURE_FILENAME,
    RAW_MANIFEST_FILENAME,
)
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchPublisher,
    RawBatchPublishRequest,
    RawManifestFile,
    load_raw_manifest,
)


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _file(name: str, payload: bytes) -> RawManifestFile:
    return RawManifestFile(relative_path=name, payload=payload, row_count=0)


@pytest.fixture()
def publisher(tmp_path: Path) -> RawBatchPublisher:
    return RawBatchPublisher(data_root=tmp_path)


class TestRawStoreLanding:
    def test_publish_creates_batch_directory_under_managed_root(
        self, tmp_path: Path, publisher: RawBatchPublisher
    ) -> None:
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-001",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        landed = publisher.publish(request)
        final = tmp_path / "raw" / "eastmoney" / "daily_bar_unadjusted" / request.batch_id
        assert landed == final
        assert landed.is_dir()
        assert (
            not (landed.with_suffix(".incomplete") if False else landed)
            .with_name(landed.name + ".incomplete")
            .exists()
        )

    def test_publish_writes_deterministic_manifest_with_sha256(
        self, tmp_path: Path, publisher: RawBatchPublisher
    ) -> None:
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-002",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ", "start": "2026-01-01"},
            files=(
                _file("rows/000001.SZ.parquet", b"hello-000001"),
                _file("rows/600000.SH.parquet", b"hello-600000"),
            ),
        )
        publisher.publish(request)
        final = tmp_path / "raw" / "eastmoney" / "daily_bar_unadjusted" / request.batch_id
        manifest_path = final / RAW_MANIFEST_FILENAME
        complete_path = final / RAW_COMPLETE_FILENAME
        assert manifest_path.is_file()
        assert complete_path.is_file()
        manifest = load_raw_manifest(final)
        assert manifest["source"] == "eastmoney"
        assert manifest["dataset"] == "daily_bar_unadjusted"
        assert manifest["batch_id"] == request.batch_id
        assert manifest["adapter_version"] == "eastmoney-probe-1"
        assert manifest["request_params"] == {"code": "000001.SZ", "start": "2026-01-01"}
        files = {entry["relative_path"]: entry for entry in manifest["files"]}
        assert set(files) == {"rows/000001.SZ.parquet", "rows/600000.SH.parquet"}
        for entry in files.values():
            assert entry["size"] == len(_payload_for(entry["relative_path"]))
            assert len(entry["sha256"]) == 64
            assert entry["row_count"] == 0
        assert (final / "rows" / "000001.SZ.parquet").is_file()

    def test_idempotent_republish_returns_existing_directory(
        self, tmp_path: Path, publisher: RawBatchPublisher
    ) -> None:
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-003",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        first = publisher.publish(request)
        second = publisher.publish(request)
        assert first == second
        # The original payload must remain unchanged.
        assert (first / "rows" / "000001.SZ.parquet").read_bytes() == b"AAA"

    def test_idempotency_conflict_when_content_differs(
        self, tmp_path: Path, publisher: RawBatchPublisher
    ) -> None:
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-004",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        publisher.publish(request)
        conflicting = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-004",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"BBB"),),
        )
        with pytest.raises(IdempotencyConflictError):
            publisher.publish(conflicting)
        # The original payload must still be intact; the conflicting request
        # must not have overwritten the batch.
        final = tmp_path / "raw" / "eastmoney" / "daily_bar_unadjusted" / request.batch_id
        assert (final / "rows" / "000001.SZ.parquet").read_bytes() == b"AAA"

    def test_idempotency_conflict_when_manifest_differs(
        self, tmp_path: Path, publisher: RawBatchPublisher
    ) -> None:
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-005",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        publisher.publish(request)
        conflicting = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-005",
            adapter_version="eastmoney-probe-2",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        with pytest.raises(IdempotencyConflictError):
            publisher.publish(conflicting)

    def test_failure_preserves_incomplete_directory_and_failure_json(self, tmp_path: Path) -> None:
        publisher = RawBatchPublisher(data_root=tmp_path)

        class _BoomPublisher(RawBatchPublisher):
            def _write_file(self, target: Path, payload: bytes) -> None:  # type: ignore[override]
                del payload
                raise RuntimeError("simulated disk full")

        publisher = _BoomPublisher(data_root=tmp_path)
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-006",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        with pytest.raises(RawStoreError):
            publisher.publish(request)
        incomplete = (
            tmp_path
            / "raw"
            / "eastmoney"
            / "daily_bar_unadjusted"
            / (request.batch_id + ".incomplete")
        )
        assert incomplete.is_dir()
        assert not (
            tmp_path / "raw" / "eastmoney" / "daily_bar_unadjusted" / request.batch_id
        ).exists()
        failure = json.loads((incomplete / RAW_FAILURE_FILENAME).read_text(encoding="utf-8"))
        assert failure["batch_id"] == request.batch_id
        # The input listing must also survive the failure for forensic review.
        assert (incomplete / "input_listing.json").is_file()
        listing = json.loads((incomplete / "input_listing.json").read_text(encoding="utf-8"))
        assert listing["files"][0]["relative_path"] == "rows/000001.SZ.parquet"

    def test_failed_retry_must_use_new_batch_id(self, tmp_path: Path) -> None:
        class _BoomPublisher(RawBatchPublisher):
            def _write_file(self, target: Path, payload: bytes) -> None:  # type: ignore[override]
                del payload
                raise RuntimeError("simulated disk full")

        publisher = _BoomPublisher(data_root=tmp_path)
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-007",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        with pytest.raises(RawStoreError):
            publisher.publish(request)
        # Retry with same batch_id must still fail, not silently turn the
        # failure into a success.
        with pytest.raises(RawStoreError):
            publisher.publish(request)
        # A retry with a new batch id must succeed independently.
        ok_publisher = RawBatchPublisher(data_root=tmp_path)
        new_request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-007-retry",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )
        landed = ok_publisher.publish(new_request)
        assert landed.is_dir()
        assert (landed / RAW_COMPLETE_FILENAME).is_file()


class TestRawStorePathContainment:
    def test_rejects_absolute_path(self, tmp_path: Path, publisher: RawBatchPublisher) -> None:
        # The publisher is the source of truth for path safety; the
        # :class:`RawManifestFile` value object also rejects the
        # unsafe path inside its own ``__post_init__`` so we have to
        # catch the error raised by either side.
        with pytest.raises(InvalidPathError):
            request = RawBatchPublishRequest(
                source="eastmoney",
                dataset="daily_bar_unadjusted",
                batch_id="b-2026-07-16-008",
                adapter_version="eastmoney-probe-1",
                request_params={"code": "000001.SZ"},
                files=(RawManifestFile(relative_path="/etc/passwd", payload=b"x", row_count=0),),
            )
            publisher.publish(request)

    def test_rejects_parent_traversal(self, tmp_path: Path, publisher: RawBatchPublisher) -> None:
        with pytest.raises(InvalidPathError):
            request = RawBatchPublishRequest(
                source="eastmoney",
                dataset="daily_bar_unadjusted",
                batch_id="b-2026-07-16-009",
                adapter_version="eastmoney-probe-1",
                request_params={"code": "000001.SZ"},
                files=(
                    RawManifestFile(
                        relative_path="../../escape.txt",
                        payload=b"x",
                        row_count=0,
                    ),
                ),
            )
            publisher.publish(request)

    def test_rejects_symlink_escape_target(self, tmp_path: Path) -> None:
        # The data root itself is a symlink to a different directory;
        # the publisher must reject the construction before any bytes
        # are written.
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = tmp_path / "linked-root"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"symlink not available: {error}")
        if not link.is_symlink() and not _is_windows_link(link):
            pytest.skip("symlink was not actually created (Windows without admin)")
        with pytest.raises(InvalidPathError):
            RawBatchPublisher(data_root=link)
        # Nothing should have been written to the outside target.
        assert outside.read_text(encoding="utf-8") == "secret"

    def test_rejects_junction_inside_data_root(self, tmp_path: Path) -> None:
        if not _junction_available(tmp_path):
            pytest.skip("Windows junction not available")
        outside = tmp_path.parent / "outside-junction"
        outside.mkdir(exist_ok=True)
        try:
            link = tmp_path / "evil-junction"
            _make_junction(link, outside)
        except OSError as error:
            pytest.skip(f"junction not available: {error}")
        if not _is_windows_link(link):
            pytest.skip("junction was not actually created")
        with pytest.raises(InvalidPathError):
            RawBatchPublisher(data_root=link)

    def test_rejects_data_root_outside_managed_boundary(self, tmp_path: Path) -> None:
        # The publisher must be constructed with an absolute data root and
        # reject relative roots.
        with pytest.raises(InvalidPathError):
            RawBatchPublisher(data_root=Path("relative/data"))


def _payload_for(name: str) -> bytes:
    return {
        "rows/000001.SZ.parquet": b"hello-000001",
        "rows/600000.SH.parquet": b"hello-600000",
    }[name]


def _junction_available(tmp_path: Path) -> bool:
    probe = tmp_path / "probe"
    try:
        _make_junction(probe, tmp_path)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(probe)
    return True


def _make_junction(link: Path, target: Path) -> None:
    import ctypes
    from ctypes import wintypes

    CreateSymbolicLinkW = ctypes.windll.kernel32.CreateSymbolicLinkW  # type: ignore[attr-defined]
    CreateSymbolicLinkW.restype = wintypes.BOOLEAN
    CreateSymbolicLinkW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    # 1 = directory junction
    ok = CreateSymbolicLinkW(str(link), str(target), 1)
    if not ok:
        raise OSError("CreateSymbolicLinkW failed")


def _is_windows_link(path: Path) -> bool:
    """Return True when ``path`` is a symlink or Windows reparse point."""
    try:
        import ctypes

        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
        if attrs == -1:
            return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return path.is_symlink()
