"""S1-07B-FINAL-R1 trust-boundary and public execution attacks."""

from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from quantking.common.time import TradingDate
from quantking.data.validation.samples import load_fixed_sample_v4


def _formal_draft():
    from scripts.build_s1_07b_reference_bundle import (
        EXPECTED_P2B_MANIFEST_SHA,
        EXPECTED_P2F_MANIFEST_SHA,
        EXPECTED_PDF_SHA,
        _build_draft,
    )

    return _build_draft(
        EXPECTED_PDF_SHA,
        EXPECTED_P2B_MANIFEST_SHA,
        EXPECTED_P2F_MANIFEST_SHA,
    )


def _publish_in_child(root: str, queue: object) -> None:
    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter

    try:
        result = AuthorityReferenceBundleWriter(root=Path(root)).write(
            _formal_draft(), sample=load_fixed_sample_v4()
        )
        queue.put(("OK", result.name))  # type: ignore[attr-defined]
    except Exception as error:  # pragma: no cover - child result asserted in parent
        queue.put(("ERROR", type(error).__name__))  # type: ignore[attr-defined]


def test_self_signed_local_owner_bundle_is_not_a_trust_anchor(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityBundleVerificationError,
        AuthorityReferenceBundleVerifier,
        AuthorityReferenceBundleWriter,
    )

    sample = load_fixed_sample_v4()
    formal = _formal_draft()
    attacker_reference = replace(
        formal.references[0],
        canonical_locator="https://attacker.invalid/notice?announcementId=1225449700",
        material_sha256="a" * 64,
        p2b_manifest_sha256="b" * 64,
        p2f_manifest_sha256="c" * 64,
    )
    attacker_facts = tuple(replace(fact, material_sha256="a" * 64) for fact in formal.facts)
    attacker = replace(
        formal,
        bundle_id="fabricated-authority-v1",
        references=(attacker_reference,),
        facts=attacker_facts,
    )
    directory = AuthorityReferenceBundleWriter(root=tmp_path).write(attacker, sample=sample)
    with pytest.raises(AuthorityBundleVerificationError, match="TRUST"):
        AuthorityReferenceBundleVerifier(root=tmp_path).verify(
            directory=directory,
            sample=sample,
        )


def test_controlled_formal_bundle_is_authority_approved(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityReferenceBundleVerifier,
        AuthorityReferenceBundleWriter,
    )
    from quantking.data.validation.samples import CaseEligibility

    sample = load_fixed_sample_v4()
    directory = AuthorityReferenceBundleWriter(root=tmp_path).write(_formal_draft(), sample=sample)
    verified = AuthorityReferenceBundleVerifier(root=tmp_path).verify(
        directory=directory,
        sample=sample,
    )
    assert verified.case_eligibilities["st_warning_603377_sh"] is CaseEligibility.AUTHORITY_APPROVED


@pytest.mark.parametrize("mutation", ("locator", "pdf", "p2b", "p2f", "approval"))
def test_resigned_lineage_or_approval_mutation_is_not_trusted(
    tmp_path: Path, mutation: str
) -> None:
    from quantking.data.adapters.warehouse.manifest import compute_sha256
    from quantking.data.validation.authority_bundle import (
        AuthorityBundleVerificationError,
        AuthorityReferenceBundleVerifier,
        AuthorityReferenceBundleWriter,
    )

    draft = _formal_draft()
    reference = draft.references[0]
    facts = draft.facts
    decisions = draft.review_decisions
    if mutation == "locator":
        reference = replace(
            reference,
            canonical_locator="https://attacker.invalid/doc?announcementId=1225449700",
        )
    elif mutation == "pdf":
        reference = replace(reference, material_sha256="d" * 64)
        facts = tuple(replace(fact, material_sha256="d" * 64) for fact in facts)
    elif mutation == "p2b":
        reference = replace(reference, p2b_manifest_sha256="e" * 64)
    elif mutation == "p2f":
        reference = replace(reference, p2f_manifest_sha256="f" * 64)
    else:
        statement = "LOCAL_OWNER attacker-controlled approval"
        decisions = (
            replace(
                decisions[0],
                approval_statement=statement,
                approval_statement_sha256=compute_sha256(
                    json.dumps(
                        {"approval_statement": statement},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                ),
            ),
        )
    attacked = replace(draft, references=(reference,), facts=facts, review_decisions=decisions)
    directory = AuthorityReferenceBundleWriter(root=tmp_path).write(
        attacked, sample=load_fixed_sample_v4()
    )
    with pytest.raises(AuthorityBundleVerificationError, match="TRUST"):
        AuthorityReferenceBundleVerifier(root=tmp_path).verify(
            directory=directory, sample=load_fixed_sample_v4()
        )


def test_usable_boundary_cannot_precede_known_from() -> None:
    formal = _formal_draft()
    with pytest.raises(ValueError, match="usable_for_decisions_after"):
        replace(
            formal.facts[0],
            usable_for_decisions_after="2020-01-01 market close",
            known_from_date=TradingDate.from_iso("2026-07-31"),
        )


def test_failed_bundle_attempt_is_preserved_with_sanitized_forensics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantking.data.validation import authority_bundle as module
    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter

    def fail(*_args: object, **_kwargs: object) -> object:
        raise module.AuthorityBundleVerificationError("AUTHORITY_BUNDLE_TEST_FAILURE")

    monkeypatch.setattr(module.AuthorityReferenceBundleVerifier, "verify_structure", fail)
    with pytest.raises(module.AuthorityBundleVerificationError):
        AuthorityReferenceBundleWriter(root=tmp_path).write(
            _formal_draft(), sample=load_fixed_sample_v4()
        )
    attempts = list((tmp_path / "reference-bundles").glob("*.incomplete"))
    assert len(attempts) == 1
    failure = json.loads((attempts[0] / "failure.json").read_bytes())
    inventory = json.loads((attempts[0] / "input_inventory.json").read_bytes())
    assert failure["error_code"] == "AUTHORITY_BUNDLE_TEST_FAILURE"
    assert failure["bundle_id"] == "st-603377-authority-v3"
    assert failure["sample_sha256"] == load_fixed_sample_v4().sample_spec_sha256
    assert "attempt_id" in failure and "failed_at" in failure
    assert inventory["bundle_id"] == failure["bundle_id"]
    assert len(inventory["files"]) == 4
    assert "Traceback" not in json.dumps(failure)


def test_cli_has_no_second_runner_import_or_v2_sample_path() -> None:
    import inspect

    from quantking.data.validation import cli

    source = inspect.getsource(cli)
    assert "importlib.import_module" not in source
    assert "load_packaged_fixed_sample" not in source
    assert ".execute(" in inspect.getsource(cli.execute)


def test_public_execute_blocks_before_claim_runner_import_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quantking.data.models import DatasetKind
    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter
    from quantking.data.validation.cli import (
        REAL_EXECUTION_ACK,
        ValidationExecutionError,
        execute,
    )
    from quantking.data.validation.runtime import (
        ControlledRunnerRegistry,
        LocalPackageVersionProbe,
        RealValidationRuntime,
    )

    AuthorityReferenceBundleWriter(root=tmp_path).write(
        _formal_draft(), sample=load_fixed_sample_v4()
    )

    class ClaimSpy:
        calls = 0

        def claim(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1

    claim = ClaimSpy()
    runtime = RealValidationRuntime(
        registry=ControlledRunnerRegistry.s1_07b(),
        package_probe=LocalPackageVersionProbe(overrides={"xtquant": "1.0.0"}),
        claim_store=claim,  # type: ignore[arg-type]
        reference_bundle_root=tmp_path,
    )
    imports: list[str] = []
    monkeypatch.setattr(
        "quantking.data.validation.runtime._import_runner",
        lambda name: imports.append(name),
    )
    output = tmp_path / "never-created"
    with pytest.raises(ValidationExecutionError) as captured:
        execute(
            source="XTQUANT",
            dataset=DatasetKind.TRADING_STATUS,
            output_root=output,
            acknowledgement=REAL_EXECUTION_ACK,
            runtime=runtime,
            case_ids=("st_warning_603377_sh",),
            reference_bundle_root=tmp_path,
        )
    assert captured.value.code == "PROVIDER_RUNNER_NOT_ENABLED"
    assert claim.calls == 0
    assert imports == []
    assert not output.exists()


def test_concurrent_same_bundle_publication_is_idempotent(tmp_path: Path) -> None:
    from quantking.data.validation.authority_bundle import AuthorityReferenceBundleWriter

    def publish() -> Path:
        return AuthorityReferenceBundleWriter(root=tmp_path).write(
            _formal_draft(), sample=load_fixed_sample_v4()
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: publish(), range(2)))
    assert results[0] == results[1]
    assert not list((tmp_path / "reference-bundles").glob("*.incomplete"))


def test_concurrent_conflicting_publication_preserves_winner_and_forensics(
    tmp_path: Path,
) -> None:
    from quantking.data.validation.authority_bundle import (
        AuthorityBundleIdempotencyConflictError,
        AuthorityReferenceBundleWriter,
    )

    formal = _formal_draft()
    conflicting = replace(
        formal,
        references=(replace(formal.references[0], authority="CNINFO altered label"),),
    )

    def publish(draft: object) -> str:
        try:
            AuthorityReferenceBundleWriter(root=tmp_path).write(
                draft,  # type: ignore[arg-type]
                sample=load_fixed_sample_v4(),
            )
            return "OK"
        except AuthorityBundleIdempotencyConflictError:
            return "CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (formal, conflicting)))
    assert sorted(results) == ["CONFLICT", "OK"]
    attempts = list((tmp_path / "reference-bundles").glob("*.incomplete"))
    assert len(attempts) == 1
    failure = json.loads((attempts[0] / "failure.json").read_bytes())
    assert failure["error_code"] == "AUTHORITY_BUNDLE_PUBLICATION_FAILED"
    assert (tmp_path / "reference-bundles" / formal.bundle_id / "COMPLETE").is_file()


def test_concurrent_process_publication_has_one_consistent_final(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_publish_in_child, args=(str(tmp_path), queue)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=5) for _ in range(2)) == [
        ("OK", "st-603377-authority-v3"),
        ("OK", "st-603377-authority-v3"),
    ]
    assert not list((tmp_path / "reference-bundles").glob("*.incomplete"))
