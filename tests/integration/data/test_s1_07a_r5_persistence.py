"""S1-07A-R5 final-TOCTOU, SQL-gate and historical-row attacks."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DBAPIError

from quantking.common.errors import InvalidDataTransitionError
from quantking.data.adapters.persistence.assessment import (
    SqliteSourceAssessmentPersistenceAdapter,
)
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.validation.authorization import (
    FakeRunnerTrustStore,
    SignerTrustPolicy,
)
from quantking.data.validation.testing import build_deep_fake_assessment
from quantking.jobs.adapters.persistence.sqlite import initialize_database

_ROOT = Path(__file__).resolve().parents[3]
_R3 = "20260729_0010"
_R5 = "20260729_0012"
_LEGACY_COLUMNS = (
    "assessment_id",
    "source",
    "dataset",
    "evidence_kind",
    "semantic_contract_verified",
    "schema_unit_verified",
    "coverage_adequate",
    "quality_passed",
    "fixed_sample_differences_adequate",
    "stability_runs",
    "status",
    "blocking_reason",
    "assessment_config_version",
    "sample_spec_version",
    "rule_version",
    "evidence_relative_path",
    "evidence_sha256",
    "signer_profiles_json",
    "result_attestation_sha256s_json",
    "created_at",
)
_R5_INSERT = (
    "INSERT INTO source_candidate_assessments "
    "(assessment_id,source,dataset,evidence_kind,semantic_contract_verified,"
    "schema_unit_verified,coverage_adequate,quality_passed,"
    "fixed_sample_differences_adequate,stability_runs,status,blocking_reason,"
    "assessment_config_version,sample_spec_version,rule_version,"
    "evidence_relative_path,evidence_sha256,signer_profiles_json,"
    "result_attestation_sha256s_json,created_at,persistence_trust_status,"
    "sealed_evidence_relative_path,sealed_evidence_sha256) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _config(database: Path) -> Config:
    configuration = Config(str(_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return configuration


def _adapter(*, root: Path, database: Path):
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=500)
    adapter = SqliteSourceAssessmentPersistenceAdapter(
        engine=engine,
        validation_root=root,
        signer=FakeRunnerTrustStore(),
        signer_trust_policy=SignerTrustPolicy.synthetic_only(),
    )
    return adapter, engine


def _directory(root: Path) -> Path:
    evidence = build_deep_fake_assessment(root=root)
    return (root / evidence.evidence_relative_path).parent


def _row_values(
    *,
    assessment_id: str = "data_assessment:11111111-1111-4111-8111-111111111111",
    evidence_kind: str = "SYNTHETIC",
    status: str = "BLOCKED_EXTERNAL_VALIDATION",
) -> tuple[object, ...]:
    return (
        assessment_id,
        "XTQUANT",
        "exchange_calendar",
        evidence_kind,
        0,
        0,
        0,
        0,
        0,
        0,
        status,
        "fixed sample is framework-only",
        "validation.v1",
        "s1-07.fixed-sample.v2",
        "quality-rules.v1",
        "missing/_assessment",
        "a" * 64,
        '["SYNTHETIC"]' if evidence_kind == "SYNTHETIC" else '["REAL"]',
        '["' + "b" * 64 + '"]',
        "2026-07-29T00:00:00Z",
    )


def _insert_legacy(connection: sqlite3.Connection, values: tuple[object, ...]) -> None:
    connection.execute(
        "INSERT INTO source_candidate_assessments "
        "(assessment_id,source,dataset,evidence_kind,semantic_contract_verified,"
        "schema_unit_verified,coverage_adequate,quality_passed,"
        "fixed_sample_differences_adequate,stability_runs,status,blocking_reason,"
        "assessment_config_version,sample_spec_version,rule_version,"
        "evidence_relative_path,evidence_sha256,signer_profiles_json,"
        "result_attestation_sha256s_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )


def _insert_r5(connection: sqlite3.Connection, values: tuple[object, ...]) -> None:
    connection.execute(
        _R5_INSERT,
        _r5_values(values),
    )


def _r5_values(values: tuple[object, ...]) -> tuple[object, ...]:
    return (
        *values,
        "SEALED_VERIFIED_V1",
        ".assessment-seals/forged.zip",
        "c" * 64,
    )


@pytest.mark.parametrize("nested", (False, True))
def test_real_process_addition_in_last_commit_window_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    root = tmp_path / "validation"
    directory = _directory(root)
    adapter, engine = _adapter(root=root, database=tmp_path / f"add-{nested}.db")
    original = adapter._verify_unchanged
    calls = 0
    attack_result: list[subprocess.CompletedProcess[str]] = []

    def verify_then_race(*args: object, **kwargs: object) -> None:
        nonlocal calls
        original(*args, **kwargs)
        calls += 1
        if calls != 2:
            return
        target = directory / ("late/nested.bin" if nested else "late.bin")
        code = (
            "from pathlib import Path; "
            f"p=Path({str(target)!r}); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_bytes(b'late')"
        )
        attack_result.append(
            subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                check=False,
                text=True,
            )
        )

    monkeypatch.setattr(adapter, "_verify_unchanged", verify_then_race)
    saved = None
    failure = None
    try:
        try:
            saved = adapter.persist_from_directory(directory)
        except InvalidDataTransitionError as error:
            failure = error
        assert attack_result
        with engine.connect() as connection:
            count = connection.exec_driver_sql(
                "SELECT count(*) FROM source_candidate_assessments"
            ).scalar_one()
        if attack_result[0].returncode == 0:
            assert failure is not None
            assert count == 0
        else:
            assert failure is None
            assert saved is not None
            assert count == 1
    finally:
        engine.dispose()


def test_real_process_cannot_recreate_claimed_directory_in_commit_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "validation"
    directory = _directory(root)
    adapter, engine = _adapter(root=root, database=tmp_path / "recreate.db")
    original = adapter._verify_unchanged
    calls = 0
    results: list[subprocess.CompletedProcess[str]] = []

    def verify_then_recreate(*args: object, **kwargs: object) -> None:
        nonlocal calls
        original(*args, **kwargs)
        calls += 1
        if calls == 2:
            code = (
                "from pathlib import Path; "
                f"p=Path({str(directory)!r}); p.unlink(); p.mkdir(); "
                "(p/'late.bin').write_bytes(b'late')"
            )
            results.append(
                subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    check=False,
                    text=True,
                )
            )

    monkeypatch.setattr(adapter, "_verify_unchanged", verify_then_recreate)
    try:
        saved = adapter.persist_from_directory(directory)
        assert saved.status.value == "BLOCKED_EXTERNAL_VALIDATION"
        assert results and results[0].returncode != 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("artifact", "operation"),
    (
        ("manifest", "replace"),
        ("manifest", "delete"),
        ("manifest", "rename"),
        ("attestation", "replace"),
        ("canonical", "replace"),
        ("quality", "replace"),
    ),
)
def test_real_process_cannot_change_claimed_artifacts_after_final_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    operation: str,
) -> None:
    root = tmp_path / "validation"
    directory = _directory(root)
    if artifact == "manifest":
        target = directory / "_assessment" / "manifest.json"
    elif artifact == "attestation":
        target = next(directory.glob("*/runner/result-attestation.json"))
    elif artifact == "canonical":
        target = next(directory.glob("*/canonical/content.parquet"))
    else:
        target = next(
            path for path in directory.rglob("manifest.json") if "quality-root" in path.parts
        )
    adapter, engine = _adapter(root=root, database=tmp_path / f"{artifact}-{operation}.db")
    original = adapter._verify_final
    results: list[subprocess.CompletedProcess[str]] = []

    def verify_then_attack(*args: object, **kwargs: object):
        verified = original(*args, **kwargs)
        if operation == "delete":
            action = "p.unlink()"
        elif operation == "rename":
            action = "p.replace(p.with_suffix('.moved'))"
        else:
            action = "p.write_bytes(p.read_bytes()+b'x')"
        code = f"from pathlib import Path; p=Path({str(target)!r}); {action}"
        results.append(
            subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                check=False,
                text=True,
            )
        )
        return verified

    monkeypatch.setattr(adapter, "_verify_final", verify_then_attack)
    try:
        saved = adapter.persist_from_directory(directory)
        assert saved.status.value == "BLOCKED_EXTERNAL_VALIDATION"
        assert results and results[0].returncode != 0
    finally:
        engine.dispose()


def test_direct_sql_cannot_insert_legal_synthetic_blocked_shape(tmp_path: Path) -> None:
    database = tmp_path / "direct.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.DatabaseError):
        _insert_r5(connection, _row_values())


@pytest.mark.parametrize(
    ("evidence_kind", "status"),
    (
        ("REAL", "PRODUCTION_READY"),
        ("SYNTHETIC", "BLOCKED_EXTERNAL_VALIDATION"),
    ),
)
def test_0010_historical_rows_are_immutably_quarantined(
    tmp_path: Path,
    evidence_kind: str,
    status: str,
) -> None:
    database = tmp_path / f"history-{evidence_kind}.db"
    configuration = _config(database)
    command.upgrade(configuration, _R3)
    values = _row_values(evidence_kind=evidence_kind, status=status)
    with sqlite3.connect(database) as connection:
        _insert_legacy(connection, values)
        connection.commit()
        before = connection.execute(
            "SELECT " + ",".join(_LEGACY_COLUMNS) + " FROM source_candidate_assessments"
        ).fetchall()
    command.upgrade(configuration, _R5)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (_R5,)
        assert connection.execute(
            "SELECT count(*) FROM source_candidate_assessments"
        ).fetchone() == (0,)
        quarantined = connection.execute(
            "SELECT " + ",".join(_LEGACY_COLUMNS) + " FROM source_candidate_assessment_quarantine"
        ).fetchall()
        assert quarantined == before
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM source_candidate_assessment_quarantine")
    with pytest.raises(Exception, match=r"verified|quarantined|assessment"):
        command.downgrade(configuration, "20260729_0011")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (_R5,)
        assert connection.execute(
            "SELECT count(*) FROM source_candidate_assessment_quarantine"
        ).fetchone() == (1,)


def test_mixed_historical_rows_are_all_quarantined(tmp_path: Path) -> None:
    database = tmp_path / "mixed.db"
    configuration = _config(database)
    command.upgrade(configuration, _R3)
    with sqlite3.connect(database) as connection:
        _insert_legacy(
            connection,
            _row_values(evidence_kind="REAL", status="PRODUCTION_READY"),
        )
        _insert_legacy(
            connection,
            _row_values(assessment_id="data_assessment:22222222-2222-4222-8222-222222222222"),
        )
        connection.commit()
    command.upgrade(configuration, _R5)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM source_candidate_assessments"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM source_candidate_assessment_quarantine"
        ).fetchone() == (2,)


def test_cross_connection_and_post_transaction_gate_reuse_fail(tmp_path: Path) -> None:
    root = tmp_path / "validation"
    directory = _directory(root)
    adapter, engine = _adapter(root=root, database=tmp_path / "reuse.db")
    try:
        adapter.persist_from_directory(directory)
        for suffix in ("cross", "post"):
            values = _row_values(
                assessment_id=(
                    "data_assessment:11111111-1111-4111-8111-"
                    + ("2" if suffix == "cross" else "3") * 12
                )
            )
            with (
                sqlite3.connect(tmp_path / "reuse.db") as connection,
                pytest.raises(sqlite3.DatabaseError),
            ):
                _insert_r5(connection, values)
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.exec_driver_sql(
                _R5_INSERT,
                _r5_values(
                    _row_values(
                        assessment_id=("data_assessment:44444444-4444-4444-8444-444444444444")
                    )
                ),
            )
    finally:
        engine.dispose()


def test_read_fails_closed_when_managed_evidence_changes(tmp_path: Path) -> None:
    from quantking.data.adapters.persistence.assessment import (
        SealedAssessmentReadVerifier,
    )

    root = tmp_path / "validation"
    directory = _directory(root)
    adapter, engine = _adapter(root=root, database=tmp_path / "read.db")
    saved = adapter.persist_from_directory(directory)
    unverified_factory = DataSqliteUnitOfWorkFactory(engine)
    with (
        unverified_factory() as uow,
        pytest.raises(InvalidDataTransitionError, match="verifier is unavailable"),
    ):
        uow.data.get_source_candidate_assessment(str(saved.assessment_id))
    reader = SealedAssessmentReadVerifier(
        validation_root=root,
        signer=FakeRunnerTrustStore(),
        signer_trust_policy=SignerTrustPolicy.synthetic_only(),
    )
    factory = DataSqliteUnitOfWorkFactory(engine, assessment_read_verifier=reader)
    try:
        with engine.begin() as connection:
            row = connection.exec_driver_sql(
                "SELECT sealed_evidence_relative_path FROM source_candidate_assessments"
            ).one()
        seal = root / str(row[0])
        seal.write_bytes(seal.read_bytes() + b"tampered")
        with (
            factory() as uow,
            pytest.raises(InvalidDataTransitionError, match="assessment evidence"),
        ):
            uow.data.get_source_candidate_assessment(str(saved.assessment_id))
    finally:
        engine.dispose()


def test_exception_consumes_gate_and_rolls_back_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "validation"
    directory = _directory(root)
    adapter, engine = _adapter(root=root, database=tmp_path / "exception.db")
    original = adapter._insert_assessment

    def fail_after_flush(*args: object, **kwargs: object):
        original(*args, **kwargs)
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(adapter, "_insert_assessment", fail_after_flush)
    try:
        with pytest.raises(InvalidDataTransitionError, match="failed safely"):
            adapter.persist_from_directory(directory)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM source_candidate_assessments"
                ).scalar_one()
                == 0
            )
        with (
            sqlite3.connect(tmp_path / "exception.db") as connection,
            pytest.raises(sqlite3.DatabaseError),
        ):
            _insert_r5(
                connection,
                _row_values(assessment_id="data_assessment:33333333-3333-4333-8333-333333333333"),
            )
    finally:
        engine.dispose()


def test_expired_connection_gate_rolls_back_without_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quantking.data.adapters.persistence.assessment as persistence

    root = tmp_path / "validation"
    directory = _directory(root)
    adapter, engine = _adapter(root=root, database=tmp_path / "expired.db")
    original = persistence._SQLiteWriteGate.arm

    def expire(self: object, expected: tuple[object, ...]) -> None:
        original(self, expected)  # type: ignore[arg-type]
        self._deadline = 0.0  # type: ignore[attr-defined]

    monkeypatch.setattr(persistence._SQLiteWriteGate, "arm", expire)
    try:
        with pytest.raises(InvalidDataTransitionError, match="failed safely"):
            adapter.persist_from_directory(directory)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM source_candidate_assessments"
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_empty_0011_upgrade_downgrade_upgrade_and_unique_head(tmp_path: Path) -> None:
    database = tmp_path / "empty.db"
    configuration = _config(database)
    command.upgrade(configuration, _R5)
    command.downgrade(configuration, "20260729_0011")
    command.upgrade(configuration, _R5)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (_R5,)
    assert ScriptDirectory.from_config(configuration).get_heads() == [_R5]
