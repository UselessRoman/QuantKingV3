"""S1-07A-R6 attacks between incomplete verification and finalized seal lock."""

from __future__ import annotations

import json
import os
import shutil
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

import quantking.data.adapters.persistence.assessment as persistence
from quantking.common.errors import InvalidDataTransitionError
from quantking.data.adapters.persistence.assessment import (
    SealedAssessmentReadVerifier,
    SqliteSourceAssessmentPersistenceAdapter,
)
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.center import DataCenterQueryService, DataCenterReadIntegrityError
from quantking.data.validation.authorization import (
    FakeRunnerTrustStore,
    SignerTrustPolicy,
)
from quantking.data.validation.testing import build_deep_fake_assessment
from quantking.jobs.adapters.persistence.sqlite import initialize_database


class _UnusedPort:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected port call: {name}")


class _NoJobs:
    def list_jobs_page(self, **filters: object) -> list[object]:
        del filters
        return []

    def count_jobs(self, **filters: object) -> int:
        del filters
        return 0


def _setup(tmp_path: Path, name: str):
    root = tmp_path / "validation"
    evidence = build_deep_fake_assessment(root=root)
    directory = (root / evidence.evidence_relative_path).parent
    database = tmp_path / f"{name}.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=500)
    signer = FakeRunnerTrustStore()
    policy = SignerTrustPolicy.synthetic_only()
    adapter = SqliteSourceAssessmentPersistenceAdapter(
        engine=engine,
        validation_root=root,
        signer=signer,
        signer_trust_policy=policy,
    )
    reader = SealedAssessmentReadVerifier(
        validation_root=root,
        signer=signer,
        signer_trust_policy=policy,
    )
    factory = DataSqliteUnitOfWorkFactory(
        engine,
        assessment_read_verifier=reader,
    )
    return root, directory, adapter, engine, factory


def _assert_failed_closed(
    *,
    root: Path,
    engine: object,
    factory: DataSqliteUnitOfWorkFactory,
) -> None:
    with engine.connect() as connection:  # type: ignore[attr-defined]
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM source_candidate_assessments"
            ).scalar_one()
            == 0
        )
    with factory() as uow:
        assert uow.data.list_source_candidate_assessments() == []
    center = DataCenterQueryService(
        data_uow_factory=factory,
        preview=_UnusedPort(),
        difference_verifier=_UnusedPort(),
        jobs=_NoJobs(),
    )
    assert all(item["assessment_status"] is None for item in center.sources())
    failures = list((root / ".assessment-failures").glob("*.json"))
    assert len(failures) == 1
    payload = failures[0].read_bytes()
    assert json.loads(payload) == {
        "category": "ASSESSMENT_PERSISTENCE_FAILED",
        "retryable": False,
    }
    rendered = payload.decode().lower()
    assert str(root).lower() not in rendered
    assert "traceback" not in rendered
    assert "token" not in rendered


def _rewrite_entry(archive_path: Path, *, needle: str) -> None:
    replacement = archive_path.with_suffix(".replacement")
    with (
        zipfile.ZipFile(archive_path, "r") as source,
        zipfile.ZipFile(replacement, "x", compression=zipfile.ZIP_STORED) as target,
    ):
        matched = False
        for info in source.infolist():
            payload = source.read(info)
            if needle in info.filename and not matched:
                payload = b"corrupted"
                matched = True
            target.writestr(info, payload)
    assert matched
    os.replace(replacement, archive_path)


@pytest.mark.parametrize(
    "attack",
    (
        "undeclared",
        "duplicate",
        "canonical",
        "quality",
    ),
)
def test_incomplete_zip_attack_after_verify_final_never_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    root, directory, adapter, engine, factory = _setup(tmp_path, attack)
    original = adapter._verify_final

    def verify_then_attack(*args: object, **kwargs: object):
        result = original(*args, **kwargs)
        incomplete = args[0]
        assert isinstance(incomplete, Path)
        if attack == "undeclared":
            with zipfile.ZipFile(incomplete, "a") as archive:
                archive.writestr("undeclared.bin", b"attack")
        elif attack == "duplicate":
            with zipfile.ZipFile(incomplete, "a") as archive:
                name = archive.infolist()[0].filename
                payload = archive.read(name)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, payload)
        elif attack == "canonical":
            _rewrite_entry(incomplete, needle="canonical/content.parquet")
        else:
            _rewrite_entry(incomplete, needle="quality-root/")
        return result

    monkeypatch.setattr(adapter, "_verify_final", verify_then_attack)
    try:
        with pytest.raises(InvalidDataTransitionError):
            adapter.persist_from_directory(directory)
        _assert_failed_closed(root=root, engine=engine, factory=factory)
    finally:
        engine.dispose()


@pytest.mark.parametrize("attack", ("truncate", "replace"))
def test_finalized_seal_attack_before_exclusive_lock_never_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    root, directory, adapter, engine, factory = _setup(tmp_path, f"finalized-{attack}")
    original = adapter._finalize_seal

    def finalize_then_attack(*args: object, **kwargs: object):
        seal, sha = original(*args, **kwargs)
        if attack == "truncate":
            seal.write_bytes(b"truncated")
        else:
            replacement = seal.with_suffix(".replacement")
            with zipfile.ZipFile(replacement, "x") as archive:
                archive.writestr("replacement.bin", b"replacement")
            os.replace(replacement, seal)
        return seal, sha

    monkeypatch.setattr(adapter, "_finalize_seal", finalize_then_attack)
    try:
        with pytest.raises(InvalidDataTransitionError):
            adapter.persist_from_directory(directory)
        _assert_failed_closed(root=root, engine=engine, factory=factory)
    finally:
        engine.dispose()


def test_existing_same_sha_seal_is_reverified_under_lock_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, directory, adapter, engine, factory = _setup(tmp_path, "existing")
    relative = directory.relative_to(root)
    try:
        first = adapter.persist_from_directory(directory)
        retained = next(
            path / relative
            for path in (root / ".assessment-retained").iterdir()
            if (path / relative).is_dir()
        )
        directory.chmod(stat.S_IWRITE)
        directory.unlink()
        shutil.copytree(retained, directory, copy_function=shutil.copy2)
        existing = set((root / ".assessment-seals").glob("*.zip"))
        original = persistence._verify_sealed_archive
        finalized_calls: list[Path] = []

        def record_full_verify(*args: object, **kwargs: object):
            seal = kwargs["seal"]
            assert isinstance(seal, Path)
            if seal in existing:
                finalized_calls.append(seal)
            return original(*args, **kwargs)

        monkeypatch.setattr(persistence, "_verify_sealed_archive", record_full_verify)
        second = adapter.persist_from_directory(directory)

        assert second == first
        assert finalized_calls
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM source_candidate_assessments"
                ).scalar_one()
                == 1
            )
        with factory() as uow:
            assert uow.data.get_source_candidate_assessment(str(first.assessment_id)) == first
    finally:
        engine.dispose()


def test_database_owner_forgery_still_fails_closed_on_read(tmp_path: Path) -> None:
    _root, _directory, _adapter, engine, factory = _setup(tmp_path, "owner")
    assessment_id = "data_assessment:55555555-5555-4555-8555-555555555555"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER source_assessments_r5_write_gate")
            connection.exec_driver_sql(
                "INSERT INTO source_candidate_assessments "
                "(assessment_id,source,dataset,evidence_kind,"
                "semantic_contract_verified,schema_unit_verified,coverage_adequate,"
                "quality_passed,fixed_sample_differences_adequate,stability_runs,"
                "status,blocking_reason,assessment_config_version,sample_spec_version,"
                "rule_version,evidence_relative_path,evidence_sha256,"
                "signer_profiles_json,result_attestation_sha256s_json,created_at,"
                "persistence_trust_status,sealed_evidence_relative_path,"
                "sealed_evidence_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    assessment_id,
                    "XTQUANT",
                    "exchange_calendar",
                    "SYNTHETIC",
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    "BLOCKED_EXTERNAL_VALIDATION",
                    "forged",
                    "validation.v1",
                    "s1-07.fixed-sample.v2",
                    "quality-rules.v1",
                    "missing/_assessment",
                    "a" * 64,
                    '["SYNTHETIC"]',
                    '["' + "b" * 64 + '"]',
                    "2026-07-29T00:00:00Z",
                    "SEALED_VERIFIED_V1",
                    ".assessment-seals/missing.zip",
                    "c" * 64,
                ),
            )
        with (
            factory() as uow,
            pytest.raises(InvalidDataTransitionError, match="assessment evidence"),
        ):
            uow.data.get_source_candidate_assessment(assessment_id)
        center = DataCenterQueryService(
            data_uow_factory=factory,
            preview=_UnusedPort(),
            difference_verifier=_UnusedPort(),
            jobs=_NoJobs(),
        )
        with pytest.raises(DataCenterReadIntegrityError, match="assessment evidence"):
            center.sources()
    finally:
        engine.dispose()
