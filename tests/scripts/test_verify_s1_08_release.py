"""S1-08 release verifier script remains a thin canonical-JSON entry point."""

from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from scripts.history.stage1.verify_fixed_release import _parser, main
from tests.data.adapters.test_akshare_adapter import FakeRunner
from tests.integration.data.test_eod_worker_k4 import (
    _NOW,
    FakeBaostockGateway,
    SyntheticAkshareModule,
    _build_runtime,
    _constant,
    _settings,
)

from quantking.data.adapters.release_verifier import (
    S108ReleaseVerificationError,
    verify_s108_release,
)
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
from quantking.data.eod.release import S107K4_IDEMPOTENCY_KEY
from quantking.data.models import DatasetKind, DatasetVersionStatus, DataUpstream
from quantking.jobs.models import JobStatus


def test_script_accepts_only_explicit_version_id_and_outputs_canonical_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_root = (tmp_path / "absent-data-root").resolve()
    monkeypatch.setenv("QUANTKING_CODE_ROOT", str((tmp_path / "code").resolve()))
    monkeypatch.setenv("QUANTKING_DATA_ROOT", str(data_root))
    monkeypatch.setenv(
        "QUANTKING_OFFSITE_BACKUP_ROOT",
        str((tmp_path / "offsite").resolve()),
    )
    parser = _parser()
    assert {action.dest for action in parser._actions} == {"help", "version_id"}

    result = main(["--version-id", "data_version:00000000-0000-4000-8000-000000000000"])

    assert result == 2
    output = capsys.readouterr().out.strip()
    payload = json.loads(output)
    assert output == json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert payload == {
        "error_code": "S1_08_RELEASE_VERIFICATION_FAILED",
        "message": "S1-08 release verification failed.",
        "status": "FAILED",
    }
    assert str(data_root) not in output
    assert not data_root.exists()


def test_script_configuration_failure_is_canonical_and_does_not_leak_detail(
    monkeypatch,
    capsys,
) -> None:
    secret = r"E:\secret\provider-token"
    monkeypatch.setenv("QUANTKING_UNRECOGNIZED_SECRET", secret)

    result = main(["--version-id", "data_version:00000000-0000-4000-8000-000000000000"])

    assert result == 2
    output = capsys.readouterr().out.strip()
    assert json.loads(output) == {
        "error_code": "S1_08_RELEASE_VERIFICATION_FAILED",
        "message": "S1-08 release verification failed.",
        "status": "FAILED",
    }
    assert secret not in output
    assert "UNRECOGNIZED" not in output


def _file_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _gateways(
    *,
    bao_fails: bool = False,
) -> dict[DataUpstream, object]:
    modules = {
        upstream: SyntheticAkshareModule(upstream, _constant(10.0))
        for upstream in (DataUpstream.TENCENT, DataUpstream.SINA)
    }
    return {
        DataUpstream.BAOSTOCK: FakeBaostockGateway(_constant(10.0), fail=bao_fails),
        **{
            upstream: AkshareMarketDataGateway(
                AkshareDailyBarAdapter(
                    upstream,
                    runner=FakeRunner(module),
                    module_loader=lambda module=module: module,
                    clock=lambda: _NOW,
                )
            )
            for upstream, module in modules.items()
        },
    }


def _create_fixed_release(
    tmp_path: Path,
    *,
    bao_fails: bool = False,
) -> tuple[object, str]:
    settings = _settings(tmp_path)
    runtime = _build_runtime(settings, _gateways(bao_fails=bao_fails))
    enqueued = runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=date(2026, 6, 11),
            end_date=date(2026, 8, 9),
            datasets=(DatasetKind.DAILY_BAR_UNADJUSTED.value,),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key=S107K4_IDEMPOTENCY_KEY,
        requester="test",
    )
    runtime.worker.run(enqueued.job_id)
    assert runtime.jobs.get_job(enqueued.job_id).status is JobStatus.SUCCEEDED
    with runtime.data_uow_factory() as uow:
        version = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )[0]
        uow.rollback()
    data_engine = runtime.data_uow_factory._session_factory.kw["bind"]
    job_engine = runtime.jobs._transactions._session_factory.kw["bind"]
    data_engine.dispose()
    job_engine.dispose()
    del runtime
    gc.collect()
    database = settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    assert not database.with_name("quantking.db-wal").exists()
    assert not database.with_name("quantking.db-shm").exists()
    return settings, str(version.version_id)


def test_s108_release_verifier_is_strict_and_read_only(tmp_path: Path) -> None:
    settings, version_id = _create_fixed_release(tmp_path)
    before = _file_snapshot(settings.paths.data_root)

    result = verify_s108_release(
        data_root=settings.paths.data_root,
        version_id=version_id,
    )

    assert result["status"] == "VERIFIED"
    assert result["scope"] == ["000001.SZ", "600000.SH"]
    assert result["endpoint_invocations"] == {
        "BAOSTOCK": 4,
        "SINA": 0,
        "TENCENT": 4,
    }
    assert result["estimated_http_request_upper_bound"] == 20
    assert result["estimated_http_requests_for_actual_inventory"] == 12
    assert result["data_center_explicit_version_read"] == {
        "lineage_batch_count": 2,
        "preview_first_key": ["000001.SZ", "2026-06-11"],
        "preview_has_next": True,
        "version_id": version_id,
    }
    assert _file_snapshot(settings.paths.data_root) == before


def test_s108_release_verifier_accepts_a_failure_with_bc_consensus(tmp_path: Path) -> None:
    settings, version_id = _create_fixed_release(tmp_path, bao_fails=True)

    result = verify_s108_release(
        data_root=settings.paths.data_root,
        version_id=version_id,
    )

    assert result["endpoint_invocations"] == {
        "BAOSTOCK": 1,
        "SINA": 4,
        "TENCENT": 4,
    }
    assert result["estimated_http_requests_for_actual_inventory"] == 20


def test_s108_release_verifier_missing_database_does_not_create_any_file(tmp_path: Path) -> None:
    data_root = (tmp_path / "missing-database").resolve()
    data_root.mkdir()
    before = _file_snapshot(data_root)

    with pytest.raises(S108ReleaseVerificationError, match="database is missing"):
        verify_s108_release(
            data_root=data_root,
            version_id="data_version:00000000-0000-4000-8000-000000000000",
        )

    assert _file_snapshot(data_root) == before


@pytest.mark.parametrize(
    "version_id",
    [
        "latest",
        "../data_version:00000000-0000-4000-8000-000000000000",
        "job:00000000-0000-4000-8000-000000000000",
        "DATA_VERSION:00000000-0000-4000-8000-000000000000",
    ],
)
def test_s108_release_verifier_rejects_non_explicit_ids_without_creating_root(
    tmp_path: Path,
    version_id: str,
) -> None:
    data_root = (tmp_path / "absent").resolve()

    with pytest.raises(S108ReleaseVerificationError, match="version_id"):
        verify_s108_release(data_root=data_root, version_id=version_id)

    assert not data_root.exists()
