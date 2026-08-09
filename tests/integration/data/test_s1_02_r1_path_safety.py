"""S1-02-R1 red tests: path safety boundaries.

These tests target the S1-02-R1 path-boundary requirements (section 二 of
the repair brief). They MUST fail against the S1-02 baseline
(``3235aba``) and pass after R1 lands:

1. ``RawBatchPublishRequest.source/dataset/batch_id`` must be single safe
   path components. Absolute paths, drive letters, slashes, backslashes,
   colons, ``.``, ``..``, parent fragments, control characters, trailing
   spaces and the reserved ``.incomplete`` suffix are all rejected. No
   file or directory may be created outside ``data_root``.
2. Raw, Canonical and Query must refuse ``symlink``/``junction`` at the
   ``data_root`` AND at every intermediate path component, not only at
   the leaf.

All tests use only ``tmp_path`` and synthetic data.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.errors import InvalidPathError
from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.warehouse.canonical_warehouse import (
    CanonicalPublishRequest,
    CanonicalWarehouse,
)
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.adapters.warehouse.raw_store import (
    RawBatchPublisher,
    RawBatchPublishRequest,
    RawManifestFile,
)
from quantking.data.models import DatasetKind, DatasetVersion

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file(name: str, payload: bytes) -> RawManifestFile:
    return RawManifestFile(relative_path=name, payload=payload, row_count=0)


def _make_junction(link: Path, target: Path) -> None:
    """Create a Windows directory junction (no admin rights required)."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise OSError("junction creation failed")


def _is_reparse_point(path: Path) -> bool:
    """Return True when ``path`` is a Windows reparse point (junction/symlink)."""
    if sys.platform != "win32":
        return path.is_symlink()
    try:
        import ctypes

        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
        if attrs == -1:
            return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return path.is_symlink()


def _canonical_daily_bar_table() -> pa.Table:
    table = pa.Table.from_pylist(
        [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ]
    )
    descriptor = canonical_schema("daily_bar_unadjusted")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def _building_version() -> DatasetVersion:
    return DatasetVersion.building(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-02",
        parent_version_id=None,
        input_batch_ids=(),
        created_at=utc_now(),
    )


# ---------------------------------------------------------------------------
# Red test 1: Raw source/dataset/batch_id path escape all rejected
# ---------------------------------------------------------------------------


class TestRawPathComponentRejection:
    """Red test 1: source/dataset/batch_id path escape must be rejected."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("source", "../../escape"),
            ("source", "/etc/passwd"),
            ("source", "C:/evil"),
            ("source", "foo/bar"),
            ("source", "foo\\bar"),
            ("source", "foo:bar"),
            ("source", "."),
            ("source", ".."),
            ("source", "foo "),
            ("source", "foo\tbar"),
            ("source", "foo\x1fbar"),
            ("source", "foo\x00bar"),
            ("source", "evil.incomplete"),
            ("source", "evil.INCOMPLETE"),
            ("dataset", "../../escape"),
            ("dataset", "/etc/passwd"),
            ("dataset", "foo/bar"),
            ("dataset", "foo\\bar"),
            ("dataset", "foo:bar"),
            ("dataset", "."),
            ("dataset", ".."),
            ("dataset", "evil.incomplete"),
            ("batch_id", "../../escape"),
            ("batch_id", "/etc/passwd"),
            ("batch_id", "foo/bar"),
            ("batch_id", "foo\\bar"),
            ("batch_id", "foo:bar"),
            ("batch_id", "."),
            ("batch_id", ".."),
            ("batch_id", "evil.incomplete"),
        ],
    )
    def test_unsafe_component_is_rejected(self, tmp_path: Path, field: str, value: str) -> None:
        publisher = RawBatchPublisher(data_root=tmp_path)
        kwargs: dict[str, object] = {
            "source": "eastmoney",
            "dataset": "daily_bar_unadjusted",
            "batch_id": "b-2026-07-16-001",
            "adapter_version": "eastmoney-probe-1",
            "request_params": {"code": "000001.SZ"},
            "files": (_file("rows/000001.SZ.parquet", b"AAA"),),
        }
        kwargs[field] = value
        with pytest.raises((InvalidPathError, ValueError)):
            publisher.publish(RawBatchPublishRequest(**kwargs))  # type: ignore[arg-type]

    def test_no_file_created_outside_data_root_after_rejected_request(self, tmp_path: Path) -> None:
        """A rejected request must not leave any artefact outside data_root."""
        outside = tmp_path / "outside"
        outside.mkdir()
        publisher = RawBatchPublisher(data_root=tmp_path)
        # ``source`` contains a parent traversal that would land outside
        # the data root if it were accepted.
        with pytest.raises((InvalidPathError, ValueError)):
            publisher.publish(
                RawBatchPublishRequest(
                    source="../../escape",
                    dataset="daily_bar_unadjusted",
                    batch_id="b-2026-07-16-002",
                    adapter_version="eastmoney-probe-1",
                    request_params={"code": "000001.SZ"},
                    files=(_file("rows/000001.SZ.parquet", b"AAA"),),
                )
            )
        # No directory may have been created outside data_root.
        assert not (tmp_path / "escape").exists()
        # The ``outside`` directory we created must be untouched.
        assert outside.is_dir()
        assert list(outside.iterdir()) == []

    def test_raw_checks_managed_target_before_creating_incomplete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed root-boundary check must happen before any directory is created."""
        publisher = RawBatchPublisher(data_root=tmp_path)
        request = RawBatchPublishRequest(
            source="eastmoney",
            dataset="daily_bar_unadjusted",
            batch_id="b-2026-07-16-preflight",
            adapter_version="eastmoney-probe-1",
            request_params={"code": "000001.SZ"},
            files=(_file("rows/000001.SZ.parquet", b"AAA"),),
        )

        def _reject(*args: object, **kwargs: object) -> Path:
            raise InvalidPathError("simulated managed-root boundary rejection")

        monkeypatch.setattr(
            "quantking.data.adapters.warehouse.raw_store.assert_path_under_root",
            _reject,
        )
        with pytest.raises(InvalidPathError):
            publisher.publish(request)
        incomplete = (
            tmp_path / "raw" / request.source / request.dataset / f"{request.batch_id}.incomplete"
        )
        assert not incomplete.exists()


# ---------------------------------------------------------------------------
# Red test 2: symlink/junction root and intermediate paths rejected
# ---------------------------------------------------------------------------


class TestSymlinkJunctionRejection:
    """Red test 2: symlink/junction must be rejected at root AND intermediates."""

    def test_raw_rejects_symlink_data_root(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-raw"
        outside.mkdir()
        link = tmp_path / "linked-raw-root"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            try:
                _make_junction(link, outside)
            except OSError as error:
                pytest.skip(f"symlink and junction unavailable: {error}")
        if not link.is_symlink() and not _is_reparse_point(link):
            pytest.skip("symlink was not actually created")
        with pytest.raises(InvalidPathError):
            RawBatchPublisher(data_root=link)

    def test_canonical_rejects_symlink_data_root(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-canonical"
        outside.mkdir()
        link = tmp_path / "linked-canonical-root"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            try:
                _make_junction(link, outside)
            except OSError as error:
                pytest.skip(f"symlink and junction unavailable: {error}")
        if not link.is_symlink() and not _is_reparse_point(link):
            pytest.skip("symlink was not actually created")
        with pytest.raises(InvalidPathError):
            CanonicalWarehouse(data_root=link)

    def test_query_rejects_symlink_data_root(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-query"
        outside.mkdir()
        link = tmp_path / "linked-query-root"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            try:
                _make_junction(link, outside)
            except OSError as error:
                pytest.skip(f"symlink and junction unavailable: {error}")
        if not link.is_symlink() and not _is_reparse_point(link):
            pytest.skip("symlink was not actually created")
        with pytest.raises(InvalidPathError):
            CanonicalQueryAdapter(data_root=link, uow_factory=None)  # type: ignore[arg-type]

    def test_raw_rejects_junction_inside_intermediate_path(self, tmp_path: Path) -> None:
        """A junction planted inside the data_root must not be followable."""
        if sys.platform != "win32":
            pytest.skip("Windows junction test")
        outside = tmp_path / "outside-intermediate-raw"
        outside.mkdir()
        # Create the directory tree up to where the publisher would write,
        # then replace an intermediate component with a junction.
        (tmp_path / "raw").mkdir()
        (tmp_path / "raw" / "eastmoney").mkdir()
        junction = tmp_path / "raw" / "eastmoney" / "evil-junction"
        try:
            _make_junction(junction, outside)
        except OSError as error:
            pytest.skip(f"junction not available: {error}")
        if not _is_reparse_point(junction):
            pytest.skip("junction was not actually created")
        publisher = RawBatchPublisher(data_root=tmp_path)
        # ``dataset`` resolves through the junction; the publish must reject
        # because the final path escapes the managed data_root via the junction.
        with pytest.raises((InvalidPathError, Exception)):
            publisher.publish(
                RawBatchPublishRequest(
                    source="eastmoney",
                    dataset="evil-junction",
                    batch_id="b-2026-07-16-003",
                    adapter_version="eastmoney-probe-1",
                    request_params={"code": "000001.SZ"},
                    files=(_file("rows/000001.SZ.parquet", b"AAA"),),
                )
            )
        # No file may be created in the outside directory.
        assert list(outside.iterdir()) == []

    def test_canonical_rejects_symlink_at_intermediate(self, tmp_path: Path) -> None:
        """A symlink planted inside the warehouse tree must be rejected."""
        outside = tmp_path / "outside-canonical-intermediate"
        outside.mkdir()
        (tmp_path / "warehouse").mkdir()
        (tmp_path / "warehouse" / "daily_bar_unadjusted").mkdir()
        link = tmp_path / "warehouse" / "daily_bar_unadjusted" / "evil-link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            try:
                _make_junction(link, outside)
            except OSError as error:
                pytest.skip(f"symlink and junction unavailable: {error}")
        if not link.is_symlink() and not _is_reparse_point(link):
            pytest.skip("symlink was not actually created")
        warehouse = CanonicalWarehouse(data_root=tmp_path)
        version = _building_version()
        # Force the final path to land inside the symlinked directory by
        # overriding the path_safe_id of the version id to ``evil-link``.
        request = CanonicalPublishRequest(
            version=version,
            tables=(_canonical_daily_bar_table(),),
            code_version="s1-02",
            rule_version="quality-rules.v1",
        )
        # Replace the final path computation to point at the symlinked dir.
        # The publisher must reject the symlink even if the path-safe id
        # itself is valid.
        original = CanonicalWarehouse._final_path
        try:

            def _patched(self: CanonicalWarehouse, req: CanonicalPublishRequest) -> Path:
                return self._data_root / "warehouse" / req.version.dataset.value / "evil-link"

            CanonicalWarehouse._final_path = _patched  # type: ignore[method-assign]
            with pytest.raises((InvalidPathError, Exception)):
                warehouse.publish(request)
        finally:
            CanonicalWarehouse._final_path = original  # type: ignore[method-assign]
        # No file may be created in the outside directory.
        assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# Module-level smoke imports (kept simple to satisfy ruff/mypy)
# ---------------------------------------------------------------------------

__all__ = [
    "TestRawPathComponentRejection",
    "TestSymlinkJunctionRejection",
]
