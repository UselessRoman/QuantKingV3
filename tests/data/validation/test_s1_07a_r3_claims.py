"""S1-07A-R3 persistent authorization claim attacks."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.models import DatasetKind


def _authorization(tmp_path: Path):
    from quantking.data.validation.authorization import FakeAuthorizationAuthority
    from quantking.data.validation.samples import load_packaged_fixed_sample

    authority = FakeAuthorizationAuthority()
    authorization = authority.issue(
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
        sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,
    )
    return authority, authorization


def _claim(store: object, authority: object, authorization: object, root: Path) -> str:
    from quantking.data.validation.authorization import AuthorizationClaimError
    from quantking.data.validation.samples import load_packaged_fixed_sample

    try:
        store.claim(
            authorization,
            invocation_id=new_entity_id("data_invocation"),
            verifier=authority,
            source="XTQUANT",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=root,
            sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,
            at=UtcTimestamp(datetime.now(UTC)),
        )
    except AuthorizationClaimError as error:
        return str(error)
    return "CLAIMED"


def test_two_new_store_instances_share_one_persistent_claim(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import AuthorizationClaimStore

    authority, authorization = _authorization(tmp_path)
    claim_root = tmp_path / "claims"
    first = AuthorizationClaimStore(claim_root=claim_root)
    second = AuthorizationClaimStore(claim_root=claim_root)
    assert _claim(first, authority, authorization, tmp_path) == "CLAIMED"
    assert _claim(second, authority, authorization, tmp_path) == ("AUTHORIZATION_ALREADY_CONSUMED")
    text = next(claim_root.glob("*.json")).read_text(encoding="utf-8")
    for field in (
        "authorization_id",
        "invocation_id",
        "source",
        "dataset",
        "output_root",
        "sample_sha256",
        "claimed_at",
        "expires_at",
        "status",
    ):
        assert f'"{field}"' in text
    assert "signature" not in text
    assert "token" not in text


def test_two_threads_on_distinct_instances_have_one_winner(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import AuthorizationClaimStore

    authority, authorization = _authorization(tmp_path)
    stores = (
        AuthorizationClaimStore(claim_root=tmp_path / "claims"),
        AuthorizationClaimStore(claim_root=tmp_path / "claims"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda store: _claim(store, authority, authorization, tmp_path),
                stores,
            )
        )
    assert sorted(results) == ["AUTHORIZATION_ALREADY_CONSUMED", "CLAIMED"]


def test_claim_store_rejects_reparse_root(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import AuthorizationClaimStore

    authority, authorization = _authorization(tmp_path)
    target = tmp_path / "target-claims"
    target.mkdir()
    link = tmp_path / "linked-claims"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("directory reparse creation is unavailable")
    assert (
        _claim(
            AuthorizationClaimStore(claim_root=link),
            authority,
            authorization,
            tmp_path,
        )
        == "AUTHORIZATION_CLAIM_STORE_UNSAFE"
    )


def test_two_processes_have_one_winner_and_restart_remembers_claim(
    tmp_path: Path,
) -> None:
    _authority, authorization = _authorization(tmp_path)
    authorization_file = tmp_path / "authorization.json"
    authorization_file.write_bytes(authorization.to_bytes())
    script = (
        "from pathlib import Path;"
        "from datetime import UTC,datetime;"
        "from quantking.common.ids import new_entity_id;"
        "from quantking.common.time import UtcTimestamp;"
        "from quantking.data.models import DatasetKind;"
        "from quantking.data.validation.authorization import "
        "AuthorizationClaimStore,AuthorizationClaimError,"
        "ExecutionAuthorization,FakeAuthorizationAuthority;"
        "from quantking.data.validation.samples import load_packaged_fixed_sample;"
        "import sys;"
        "a=ExecutionAuthorization.from_bytes(Path(sys.argv[1]).read_bytes());"
        "s=AuthorizationClaimStore(claim_root=Path(sys.argv[2]));"
        "\ntry:\n s.claim(a,invocation_id=new_entity_id('data_invocation'),"
        "verifier=FakeAuthorizationAuthority(),source='XTQUANT',"
        "dataset=DatasetKind.EXCHANGE_CALENDAR,output_root=Path(sys.argv[3]),"
        "sample_sha256=load_packaged_fixed_sample().sample_spec_sha256,"
        "at=UtcTimestamp(datetime.now(UTC)));print('CLAIMED')"
        "\nexcept AuthorizationClaimError as e:\n print(str(e))"
    )
    command = [
        sys.executable,
        "-c",
        script,
        str(authorization_file),
        str(tmp_path / "claims"),
        str(tmp_path),
    ]
    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _index in range(2)
    ]
    results = [process.communicate(timeout=20) for process in processes]
    assert all(process.returncode == 0 for process in processes)
    assert sorted(stdout.strip() for stdout, _stderr in results) == [
        "AUTHORIZATION_ALREADY_CONSUMED",
        "CLAIMED",
    ]
    restarted = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
    assert restarted.stdout.strip() == "AUTHORIZATION_ALREADY_CONSUMED"


def test_blocked_public_runtime_does_not_consume_persistent_claim(tmp_path: Path) -> None:
    from quantking.data.validation.authorization import AuthorizationClaimStore
    from quantking.data.validation.cli import (
        REAL_EXECUTION_ACK,
        ValidationExecutionError,
        execute,
    )

    authority, authorization = _authorization(tmp_path)
    claim_root = tmp_path / "claims"
    with pytest.raises(ValidationExecutionError) as first:
        execute(
            source="XTQUANT",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
            acknowledgement=REAL_EXECUTION_ACK,
            authorization=authorization,
            authorization_verifier=authority,
            invocation_id=new_entity_id("data_invocation"),
            authorization_claim_store=AuthorizationClaimStore(claim_root=claim_root),
        )
    assert first.value.code == "PROVIDER_PACKAGE_VERSION_UNRESOLVED"
    assert (
        _claim(AuthorizationClaimStore(claim_root=claim_root), authority, authorization, tmp_path)
        == "CLAIMED"
    )


def test_legacy_fake_runner_cannot_bypass_public_runtime_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantking.data.validation.authorization import AuthorizationClaimStore
    from quantking.data.validation.cli import (
        REAL_EXECUTION_ACK,
        ValidationExecutionError,
        execute,
    )

    class ExplodingRunner:
        def execute(self, **_kwargs: object) -> Path:
            raise RuntimeError("synthetic runner execution failure")

    module = ModuleType("quantking.data.validation.runners.xtquant")
    module.create_runner = lambda: ExplodingRunner()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    authority, authorization = _authorization(tmp_path)
    claim_root = tmp_path / "claims"
    with pytest.raises(ValidationExecutionError) as captured:
        execute(
            source="XTQUANT",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
            acknowledgement=REAL_EXECUTION_ACK,
            authorization=authorization,
            authorization_verifier=authority,
            invocation_id=new_entity_id("data_invocation"),
            authorization_claim_store=AuthorizationClaimStore(claim_root=claim_root),
        )
    assert getattr(captured.value, "code", None) == "PROVIDER_PACKAGE_VERSION_UNRESOLVED"
    assert (
        _claim(AuthorizationClaimStore(claim_root=claim_root), authority, authorization, tmp_path)
        == "CLAIMED"
    )
