"""S1-08R3 A: close the two non-blocking observations from the R2 acceptance.

1. ``recover_stale_incomplete`` classification coverage for the
   ``promotion-interrupted`` and ``orphaned-after-publish`` branches.
2. A direct fail-closed assertion that ``verify_manifest_release`` rejects a
   synthetic ``XTQUANT_LEGACY`` batch grafted into an otherwise valid release
   lineage.
"""

from __future__ import annotations

import gc
import json
import sqlite3
from pathlib import Path

import pytest
from tests.integration.data.test_s1_08r3_formal_entry import _FormalEnv, _gateways

from quantking.data.adapters.release_verifier import (
    ManifestReleaseVerificationError,
    verify_manifest_release,
)
from quantking.data.adapters.warehouse.canonical_warehouse import CanonicalWarehouse
from quantking.data.adapters.warehouse.manifest import WAREHOUSE_COMPLETE_FILENAME
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
from quantking.data.models import DatasetVersionStatus
from quantking.runtime import build_eod_runtime

_AUTH_ID = "S1-08R3-E1-4f7c2a90b1d3"


@pytest.fixture()
def formal(tmp_path: Path) -> _FormalEnv:
    return _FormalEnv(tmp_path)


# -- 1. recover_stale_incomplete classification branches ----------------------


def _publish_one(tmp_path: Path):  # type: ignore[no-untyped-def]
    from tests.integration.data.test_s1_08r2_publish_reliability import (
        _request,
        _version,
    )

    warehouse = CanonicalWarehouse(data_root=tmp_path)
    version = _version()
    final = warehouse.publish(_request(version))
    return warehouse, version, final


def test_recover_classifies_promotion_interrupted(tmp_path: Path) -> None:
    warehouse, version, _final = _publish_one(tmp_path)
    # Simulate the crash window after the COMPLETE marker was written into
    # staging but before the promotion rename: no final directory exists and
    # the .incomplete directory carries a COMPLETE marker.
    warehouse_root = tmp_path / "warehouse" / "daily_bar_unadjusted"
    published = next(
        path
        for path in warehouse_root.iterdir()
        if "incomplete" not in path.name and not path.name.startswith(".")
    )
    staging = warehouse_root / (published.name + ".incomplete")
    staging.mkdir()
    (staging / WAREHOUSE_COMPLETE_FILENAME).write_text("interrupted\n", encoding="utf-8")
    # The publish already finalized, so remove the promoted directory to
    # reproduce the pre-promotion crash state.
    import shutil

    shutil.rmtree(published)

    outcome = warehouse.recover_stale_incomplete(version)

    assert outcome["status"] == "recovered"
    assert outcome["classification"] == "promotion-interrupted"
    assert not staging.exists()
    preserved = warehouse_root / outcome["preserved_directory"]
    assert (preserved / WAREHOUSE_COMPLETE_FILENAME).is_file()


def test_recover_classifies_orphaned_after_publish(tmp_path: Path) -> None:
    warehouse, version, final = _publish_one(tmp_path)
    verify_canonical_directory(final, data_root=tmp_path)
    warehouse_root = tmp_path / "warehouse" / "daily_bar_unadjusted"
    orphan = final.with_name(final.name + ".incomplete")
    orphan.mkdir()
    (orphan / "leftover.txt").write_text("stale staging debris\n", encoding="utf-8")

    outcome = warehouse.recover_stale_incomplete(version)

    assert outcome["status"] == "recovered"
    assert outcome["classification"] == "orphaned-after-publish"
    assert final.is_dir()  # the promoted directory is untouched
    assert not orphan.exists()
    preserved = warehouse_root / outcome["preserved_directory"]
    assert (preserved / "leftover.txt").is_file()
    # A rerun after recovery is a clean no-op report.
    again = warehouse.recover_stale_incomplete(version)
    assert again == {"status": "absent", "version_id": str(version.version_id)}


# -- 2. verifier rejects a synthetic XTQUANT_LEGACY lineage row ----------------


def _settled_database(formal: _FormalEnv) -> Path:
    gc.collect()
    database = formal.settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    return database


def test_verifier_rejects_xtquant_legacy_in_the_release_lineage(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    gateways, _modules = _gateways()
    formal.run(
        authorization=formal.token(),
        envelope_path=formal.envelope_path(tmp_path),
        gateways=gateways,
    )
    database = _settled_database(formal)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as read_only:
        rows = read_only.execute(
            "SELECT version_id FROM dataset_versions"
            " WHERE dataset = 'daily_bar_unadjusted' AND status = 'PUBLISHED'"
        ).fetchall()
    assert len(rows) == 1
    version_id = rows[0][0]

    # Baseline: the untouched release verifies.
    verify_manifest_release(
        data_root=formal.settings.paths.data_root,
        version_id=version_id,
        manifest=formal.manifest,
    )

    # Graft a synthetic XTQUANT_LEGACY batch into the same lineage. The
    # production database makes terminal batches immutable by trigger; this
    # synthetic tampering fixture drops that guard on the temporary test
    # database only, exactly to prove the verifier does not rely on it.
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER ingestion_batches_no_update_terminal")
        batch = connection.execute(
            "SELECT batch_id, request_params_json FROM ingestion_batches ORDER BY batch_id LIMIT 1"
        ).fetchone()
        assert batch is not None
        params = json.loads(batch[1])
        assert params["upstream"] in {"BAOSTOCK", "TENCENT", "SINA"}
        params["upstream"] = "XTQUANT"
        params["provider"] = "XTQUANT_LEGACY"
        connection.execute(
            "UPDATE ingestion_batches SET request_params_json = ? WHERE batch_id = ?",
            (json.dumps(params, sort_keys=True), batch[0]),
        )

    with pytest.raises(ManifestReleaseVerificationError) as error:
        verify_manifest_release(
            data_root=formal.settings.paths.data_root,
            version_id=version_id,
            manifest=formal.manifest,
        )
    assert "XTQUANT_LEGACY batch entered the release lineage" in str(error.value)


def test_formal_release_stays_the_only_published_version(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    gateways, _modules = _gateways()
    formal.run(
        authorization=formal.token(),
        envelope_path=formal.envelope_path(tmp_path),
        gateways=gateways,
    )
    runtime = build_eod_runtime(formal.settings, gateways=_gateways()[0])
    with runtime.data_uow_factory() as uow:
        published = [
            item
            for item in uow.data.list_versions(dataset=None)
            if item.status is DatasetVersionStatus.PUBLISHED
            and item.dataset.value == "daily_bar_unadjusted"
        ]
    assert len(published) == 1
