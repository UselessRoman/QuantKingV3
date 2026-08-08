"""S1-07C1C0's isolated, one-call XTData observation runner.

This is deliberately not an evolution of C1B.  C1B remains a failure-only
contract.  A C1C0 authorization has its own schema, purpose and signature
domain, so a C1B file cannot dispatch this runner.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from quantking.data.adapters.sources.xtquant_c1c0_runner import read_fixed_st_observation
from quantking.data.adapters.sources.xtquant_metadata import ExplicitXtQuantPackageMetadataProbe
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    RealAuthorizationError,
    RealVerifierTrustAnchor,
)

C1C0_PLAN_SCHEMA = "s1-07c1c0-miniqmt-xtdata-plan.v1"
C1C0_AUTHORIZATION_SCHEMA = "s1-07c1c0-miniqmt-xtdata-authorization.v1"
C1C0_OBSERVATION_SCHEMA = "s1-07c1c0-provider-observation.v1"
C1C0_PURPOSE = "S1-07C1C0_MINIQMT_XTDATA_ST_OBSERVATION"
C1C0_RUNNER_CONTRACT_ID = "xtquant.xtdata.get_his_st_data.603377-sh.v1"
C1C0_RUNNER_PROFILE = "miniqmt-xtdata-readonly-st-observation.c1c0.v1"
C1C0_RUNNER_VERSION = "c1c0.xtdata-runner.v1"
C1C0_ADAPTER_VERSION = "xtquant-xtdata-readonly.c1c0.v1"
_SIGN_DOMAIN = "quantking.s1-07c1c0.authorization.v1"
_ARTIFACT_DOMAIN = "quantking.s1-07c1c0.observation.v1"
_FIXED_CODE = "603377.SH"
_ALLOWED_STATUS = frozenset({"ST", "*ST", "PT"})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha(value: bytes) -> str:
    return sha256(value).hexdigest()


class C1C0OperationError(ValueError):
    """A sanitized C1C0 precheck, runner, or evidence error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _absolute(path: Path, *, field: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise C1C0OperationError("C1C0_PARAMETER_INVALID", f"{field} must be an absolute path")
    return path.resolve(strict=False)


@dataclass(frozen=True, slots=True)
class C1C0Plan:
    """Canonical pre-claim binding, including the non-overridable read call."""

    authorization_id: str
    invocation_id: str
    run_id: str
    site_packages_root: Path
    package_version: str
    output_root: Path
    claim_root: Path
    authorization_public_key_file: Path
    authorization_allowed_key_ids: tuple[str, ...]
    runner_private_key_file: Path
    runner_public_key_file: Path
    runner_allowed_key_ids: tuple[str, ...]
    runner_contract_id: str
    runner_profile: str
    runner_version: str
    adapter_version: str
    plan_sha256: str

    def unsigned(self) -> dict[str, object]:
        return {
            "adapter_version": self.adapter_version,
            "authorization_allowed_key_ids": list(self.authorization_allowed_key_ids),
            "authorization_id": self.authorization_id,
            "authorization_public_key_file": str(self.authorization_public_key_file),
            "claim_root": str(self.claim_root),
            "fixed_request": {"code": _FIXED_CODE, "interface": "xtdata.get_his_st_data"},
            "invocation_id": self.invocation_id,
            "output_root": str(self.output_root),
            "package_version": self.package_version,
            "runner_allowed_key_ids": list(self.runner_allowed_key_ids),
            "runner_contract_id": self.runner_contract_id,
            "runner_private_key_file": str(self.runner_private_key_file),
            "runner_profile": self.runner_profile,
            "runner_version": self.runner_version,
            "runner_public_key_file": str(self.runner_public_key_file),
            "run_id": self.run_id,
            "schema_version": C1C0_PLAN_SCHEMA,
            "site_packages_root": str(self.site_packages_root),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned(), "plan_sha256": self.plan_sha256}

    def to_bytes(self) -> bytes:
        return _canonical(self.to_dict())


def _validate_plan(plan: C1C0Plan) -> None:
    if (
        plan.runner_contract_id != C1C0_RUNNER_CONTRACT_ID
        or plan.runner_profile != C1C0_RUNNER_PROFILE
        or plan.runner_version != C1C0_RUNNER_VERSION
        or plan.adapter_version != C1C0_ADAPTER_VERSION
        or not plan.package_version
        or not plan.authorization_id
        or not plan.invocation_id
        or not plan.run_id
        or tuple(sorted(plan.authorization_allowed_key_ids)) != plan.authorization_allowed_key_ids
        or tuple(sorted(plan.runner_allowed_key_ids)) != plan.runner_allowed_key_ids
        or not plan.authorization_allowed_key_ids
        or not plan.runner_allowed_key_ids
        or plan.claim_root == plan.output_root
    ):
        raise C1C0OperationError("C1C0_PLAN_INVALID", "C1C0 plan binding is invalid")
    if _sha(_canonical(plan.unsigned())) != plan.plan_sha256:
        raise C1C0OperationError("C1C0_PLAN_INVALID", "C1C0 plan SHA is invalid")


def prepare_plan(
    *,
    authorization_id: str,
    invocation_id: str,
    run_id: str,
    site_packages_root: Path,
    package_version: str,
    output_root: Path,
    claim_root: Path,
    authorization_public_key_file: Path,
    authorization_allowed_key_ids: tuple[str, ...],
    runner_private_key_file: Path,
    runner_public_key_file: Path,
    runner_allowed_key_ids: tuple[str, ...],
) -> C1C0Plan:
    """Create a C1C0 plan without importing XTQuant or creating a claim."""
    provisional = C1C0Plan(
        authorization_id=authorization_id,
        invocation_id=invocation_id,
        run_id=run_id,
        site_packages_root=_absolute(site_packages_root, field="site-packages root"),
        package_version=package_version,
        output_root=_absolute(output_root, field="output root"),
        claim_root=_absolute(claim_root, field="claim root"),
        authorization_public_key_file=_absolute(
            authorization_public_key_file, field="authorization public key"
        ),
        authorization_allowed_key_ids=tuple(authorization_allowed_key_ids),
        runner_private_key_file=_absolute(runner_private_key_file, field="runner private key"),
        runner_public_key_file=_absolute(runner_public_key_file, field="runner public key"),
        runner_allowed_key_ids=tuple(runner_allowed_key_ids),
        runner_contract_id=C1C0_RUNNER_CONTRACT_ID,
        runner_profile=C1C0_RUNNER_PROFILE,
        runner_version=C1C0_RUNNER_VERSION,
        adapter_version=C1C0_ADAPTER_VERSION,
        plan_sha256="",
    )
    plan = C1C0Plan(
        **{
            **{
                key: getattr(provisional, key)
                for key in provisional.__dataclass_fields__
                if key != "plan_sha256"
            },
            "plan_sha256": _sha(_canonical(provisional.unsigned())),
        }
    )
    _validate_plan(plan)
    return plan


@dataclass(frozen=True, slots=True)
class C1C0Authorization:
    plan: C1C0Plan
    authorization_key_id: str
    signature: str

    def unsigned(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "purpose": C1C0_PURPOSE,
            "authorization_key_id": self.authorization_key_id,
            "schema_version": C1C0_AUTHORIZATION_SCHEMA,
        }

    def to_bytes(self) -> bytes:
        return _canonical({**self.unsigned(), "signature": self.signature})


def issue_authorization(
    *, plan: C1C0Plan, authorization_private_key_file: Path
) -> C1C0Authorization:
    _validate_plan(plan)
    try:
        signer = ExplicitLocalRealKey(
            private_key_file=_absolute(
                authorization_private_key_file, field="authorization private key"
            )
        )
    except RealAuthorizationError as error:
        raise C1C0OperationError("C1C0_KEY_INVALID", "authorization key is invalid") from error
    unsigned = C1C0Authorization(plan=plan, authorization_key_id=signer.key_id, signature="0" * 128)
    return C1C0Authorization(
        plan=plan,
        authorization_key_id=signer.key_id,
        signature=signer.sign(domain=_SIGN_DOMAIN, payload=unsigned.unsigned()),
    )


def _authorization_from_bytes(payload: bytes) -> C1C0Authorization:
    try:
        raw: Any = json.loads(payload)
        if (
            payload != _canonical(raw)
            or set(raw)
            != {"plan", "purpose", "authorization_key_id", "schema_version", "signature"}
            or raw["purpose"] != C1C0_PURPOSE
            or raw["schema_version"] != C1C0_AUTHORIZATION_SCHEMA
        ):
            raise ValueError
        plan_raw = raw["plan"]
        declared = plan_raw.pop("plan_sha256")
        if not isinstance(plan_raw, dict):
            raise ValueError
        plan = C1C0Plan(
            authorization_id=plan_raw["authorization_id"],
            invocation_id=plan_raw["invocation_id"],
            run_id=plan_raw["run_id"],
            site_packages_root=_absolute(
                Path(plan_raw["site_packages_root"]), field="site-packages root"
            ),
            package_version=plan_raw["package_version"],
            output_root=_absolute(Path(plan_raw["output_root"]), field="output root"),
            claim_root=_absolute(Path(plan_raw["claim_root"]), field="claim root"),
            authorization_public_key_file=_absolute(
                Path(plan_raw["authorization_public_key_file"]), field="authorization public key"
            ),
            authorization_allowed_key_ids=tuple(plan_raw["authorization_allowed_key_ids"]),
            runner_private_key_file=_absolute(
                Path(plan_raw["runner_private_key_file"]), field="runner private key"
            ),
            runner_public_key_file=_absolute(
                Path(plan_raw["runner_public_key_file"]), field="runner public key"
            ),
            runner_allowed_key_ids=tuple(plan_raw["runner_allowed_key_ids"]),
            runner_contract_id=plan_raw["runner_contract_id"],
            runner_profile=plan_raw["runner_profile"],
            runner_version=plan_raw["runner_version"],
            adapter_version=plan_raw["adapter_version"],
            plan_sha256=declared,
        )
        _validate_plan(plan)
        return C1C0Authorization(
            plan=plan, authorization_key_id=raw["authorization_key_id"], signature=raw["signature"]
        )
    except (KeyError, TypeError, ValueError, C1C0OperationError) as error:
        raise C1C0OperationError(
            "C1C0_AUTHORIZATION_INVALID", "C1C0 authorization is invalid"
        ) from error


def _precheck(
    authorization: C1C0Authorization,
) -> tuple[RealVerifierTrustAnchor, ExplicitLocalRealKey]:
    plan = authorization.plan
    _validate_plan(plan)
    try:
        authorization_anchor = RealVerifierTrustAnchor(
            public_key_file=plan.authorization_public_key_file,
            allowed_key_ids=frozenset(plan.authorization_allowed_key_ids),
        )
        runner_anchor = RealVerifierTrustAnchor(
            public_key_file=plan.runner_public_key_file,
            allowed_key_ids=frozenset(plan.runner_allowed_key_ids),
        )
        runner = ExplicitLocalRealKey(private_key_file=plan.runner_private_key_file)
        version = ExplicitXtQuantPackageMetadataProbe(
            site_packages_root=plan.site_packages_root
        ).exact_version()
    except RealAuthorizationError as error:
        raise C1C0OperationError("C1C0_KEY_DRIFT", "C1C0 key binding changed") from error
    except ValueError as error:
        raise C1C0OperationError(
            "C1C0_PACKAGE_UNAVAILABLE", "XTQuant package metadata is unavailable"
        ) from error
    if (
        authorization.authorization_key_id != authorization_anchor.key_id
        or not authorization_anchor.verify(
            domain=_SIGN_DOMAIN, payload=authorization.unsigned(), signature=authorization.signature
        )
        or runner.key_id != runner_anchor.key_id
        or version != plan.package_version
    ):
        raise C1C0OperationError(
            "C1C0_PRECLAIM_BINDING_MISMATCH", "C1C0 package, key, or signature binding changed"
        )
    # This checks the exact controlled import origin without importing a package.
    if (
        not (plan.site_packages_root / "xtquant" / "__init__.py").is_file()
        or not (plan.site_packages_root / "xtquant" / "xtdata.py").is_file()
    ):
        raise C1C0OperationError(
            "C1C0_PACKAGE_PATH_MISMATCH", "XTQuant package path is not the authorized path"
        )
    return authorization_anchor, runner


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _claim(plan: C1C0Plan, authorization_bytes: bytes) -> bytes:
    receipt = _canonical(
        {
            "authorization_id": plan.authorization_id,
            "authorization_sha256": _sha(authorization_bytes),
            "invocation_id": plan.invocation_id,
            "plan_sha256": plan.plan_sha256,
            "run_id": plan.run_id,
            "schema_version": "s1-07c1c0-claim.v1",
        }
    )
    try:
        _write_exclusive(plan.claim_root / f"{plan.authorization_id}.json", receipt)
    except FileExistsError as error:
        raise C1C0OperationError(
            "AUTHORIZATION_ALREADY_CONSUMED", "authorization was already claimed"
        ) from error
    except OSError as error:
        raise C1C0OperationError("C1C0_CLAIM_FAILED", "C1C0 claim cannot be persisted") from error
    return receipt


def _parse_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    normalized = value.replace("-", "")
    if len(normalized) != 8 or not normalized.isdigit():
        raise ValueError
    return date(int(normalized[:4]), int(normalized[4:6]), int(normalized[6:])).isoformat()


def parse_st_intervals(response: object) -> dict[str, object]:
    """Strictly preserve only provider-observed ST/*ST/PT intervals.

    ``{}`` is intentionally *not* evidence that the issuer was never ST.
    """
    if response == {}:
        return {"intervals": [], "observation_state": "NO_DETERMINATE_DATA"}
    if (
        not isinstance(response, dict)
        or not response
        or any(key not in _ALLOWED_STATUS for key in response)
    ):
        raise ValueError("ST response has an unsupported shape")
    intervals: list[dict[str, str]] = []
    for status in sorted(response):
        rows = response[status]
        if not isinstance(rows, (list, tuple)):
            raise ValueError("ST response interval list is invalid")
        for row in rows:
            if isinstance(row, (list, tuple)) and len(row) == 2:
                start, end = row
            elif isinstance(row, dict) and set(row) == {"start", "end"}:
                start, end = row["start"], row["end"]
            else:
                raise ValueError("ST response interval is invalid")
            start_date, end_date = _parse_date(start), _parse_date(end)
            if end_date < start_date:
                raise ValueError("ST response interval is reversed")
            intervals.append({"end_date": end_date, "start_date": start_date, "status": status})
    return {
        "intervals": sorted(
            intervals, key=lambda item: (item["start_date"], item["end_date"], item["status"])
        ),
        "observation_state": "OBSERVED_INTERVALS" if intervals else "OBSERVED_EMPTY_INTERVALS",
    }


def _read_once(plan: C1C0Plan) -> object:
    """The only deferred SDK import and the only permitted provider invocation."""
    return read_fixed_st_observation(site_packages_root=plan.site_packages_root)


def _directory(plan: C1C0Plan) -> Path:
    return plan.output_root / "provider-observations" / "xtquant" / "trading_status" / plan.run_id


def _write_incomplete(plan: C1C0Plan, *, claim: bytes, category: str) -> Path:
    target = _directory(plan).with_name(_directory(plan).name + ".incomplete")
    try:
        target.mkdir(parents=True, exist_ok=False)
        (target / "failure.json").write_bytes(
            _canonical(
                {
                    "category": category,
                    "claim_sha256": _sha(claim),
                    "plan_sha256": plan.plan_sha256,
                    "retryable": False,
                }
            )
        )
    except OSError:
        pass
    return target


def _write_observation(
    *,
    authorization_bytes: bytes,
    authorization: C1C0Authorization,
    claim: bytes,
    response: object,
    observation: dict[str, object],
    runner: ExplicitLocalRealKey,
) -> Path:
    plan = authorization.plan
    directory = _directory(plan)
    if directory.exists():
        raise C1C0OperationError("C1C0_OUTPUT_EXISTS", "observation output already exists")
    directory.mkdir(parents=True, exist_ok=False)
    response_bytes = _canonical(response)
    files = {
        "authorization.json": authorization_bytes,
        "claim.json": claim,
        "observation.json": _canonical(
            {
                **observation,
                "conclusion": "BLOCKED/CONTRACT_INCOMPLETE",
                "fixed_request": {"code": _FIXED_CODE, "interface": "xtdata.get_his_st_data"},
                "runner_contract_id": plan.runner_contract_id,
                "runner_profile": plan.runner_profile,
            }
        ),
        "response.canonical.json": response_bytes,
        "request.json": _canonical(plan.to_dict()),
    }
    inventory = {name: _sha(payload) for name, payload in sorted(files.items())}
    unsigned = {
        "adapter_version": plan.adapter_version,
        "authorization_sha256": _sha(authorization_bytes),
        "claim_sha256": _sha(claim),
        "conclusion": "BLOCKED/CONTRACT_INCOMPLETE",
        "files": inventory,
        "package_version": plan.package_version,
        "plan_sha256": plan.plan_sha256,
        "response_sha256": _sha(response_bytes),
        "runner_contract_id": plan.runner_contract_id,
        "runner_key_id": runner.key_id,
        "runner_profile": plan.runner_profile,
        "runner_version": plan.runner_version,
        "schema_version": C1C0_OBSERVATION_SCHEMA,
        "site_packages_root": str(plan.site_packages_root),
    }
    artifact = {**unsigned, "signature": runner.sign(domain=_ARTIFACT_DOMAIN, payload=unsigned)}
    for name, payload in files.items():
        (directory / name).write_bytes(payload)
    (directory / "manifest.json").write_bytes(_canonical(artifact))
    return directory


def execute(*, authorization_file: Path) -> Path:
    """Perform PRECHECK → CLAIM → IMPORT and one fixed read; no caller controls it."""
    try:
        authorization_bytes = _absolute(authorization_file, field="authorization file").read_bytes()
    except OSError as error:
        raise C1C0OperationError(
            "C1C0_AUTHORIZATION_INVALID", "authorization cannot be read"
        ) from error
    authorization = _authorization_from_bytes(authorization_bytes)
    _authorization_anchor, runner = _precheck(authorization)
    claim = _claim(authorization.plan, authorization_bytes)
    try:
        response = _read_once(authorization.plan)
    except Exception as error:
        _write_incomplete(authorization.plan, claim=claim, category="C1C0_IMPORT_OR_CALL_FAILED")
        raise C1C0OperationError(
            "C1C0_IMPORT_OR_CALL_FAILED", "XTData read failed after claim"
        ) from error
    try:
        observation = parse_st_intervals(response)
        return _write_observation(
            authorization_bytes=authorization_bytes,
            authorization=authorization,
            claim=claim,
            response=response,
            observation=observation,
            runner=runner,
        )
    except Exception as error:
        _write_incomplete(authorization.plan, claim=claim, category="C1C0_RESPONSE_PARSE_FAILED")
        raise C1C0OperationError(
            "C1C0_RESPONSE_PARSE_FAILED", "XTData response failed strict parsing"
        ) from error


def verify_observation(
    *,
    directory: Path,
    authorization_public_key_file: Path,
    authorization_allowed_key_ids: frozenset[str],
    runner_public_key_file: Path,
    runner_allowed_key_ids: frozenset[str],
) -> dict[str, object]:
    """Deep-verify immutable lineage, response hashes, contracts, and signatures."""
    try:
        manifest_bytes = (directory / "manifest.json").read_bytes()
        manifest: Any = json.loads(manifest_bytes)
        if (
            manifest_bytes != _canonical(manifest)
            or manifest.get("schema_version") != C1C0_OBSERVATION_SCHEMA
        ):
            raise ValueError
        auth_bytes = (directory / "authorization.json").read_bytes()
        authorization = _authorization_from_bytes(auth_bytes)
        plan = authorization.plan
        auth_anchor = RealVerifierTrustAnchor(
            public_key_file=authorization_public_key_file,
            allowed_key_ids=authorization_allowed_key_ids,
        )
        runner_anchor = RealVerifierTrustAnchor(
            public_key_file=runner_public_key_file, allowed_key_ids=runner_allowed_key_ids
        )
        if auth_anchor.key_id != authorization.authorization_key_id or not auth_anchor.verify(
            domain=_SIGN_DOMAIN, payload=authorization.unsigned(), signature=authorization.signature
        ):
            raise ValueError
        files = manifest["files"]
        if not isinstance(files, dict) or any(
            _sha((directory / name).read_bytes()) != digest for name, digest in files.items()
        ):
            raise ValueError
        response = (directory / "response.canonical.json").read_bytes()
        if (
            manifest["response_sha256"] != _sha(response)
            or manifest["authorization_sha256"] != _sha(auth_bytes)
            or manifest["plan_sha256"] != plan.plan_sha256
        ):
            raise ValueError
        unsigned = dict(manifest)
        signature = unsigned.pop("signature")
        if (
            manifest["runner_contract_id"] != C1C0_RUNNER_CONTRACT_ID
            or manifest["runner_profile"] != C1C0_RUNNER_PROFILE
            or manifest["runner_version"] != C1C0_RUNNER_VERSION
            or manifest["runner_key_id"] != runner_anchor.key_id
            or not runner_anchor.verify(
                domain=_ARTIFACT_DOMAIN, payload=unsigned, signature=signature
            )
        ):
            raise ValueError
        parsed = parse_st_intervals(json.loads(response))
        if (
            json.loads((directory / "observation.json").read_bytes())["intervals"]
            != parsed["intervals"]
        ):
            raise ValueError
        return cast(dict[str, object], manifest)
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        C1C0OperationError,
        RealAuthorizationError,
    ) as error:
        raise C1C0OperationError(
            "C1C0_OBSERVATION_INVALID", "provider observation verification failed"
        ) from error


__all__ = [
    "C1C0_ADAPTER_VERSION",
    "C1C0_AUTHORIZATION_SCHEMA",
    "C1C0_OBSERVATION_SCHEMA",
    "C1C0_PLAN_SCHEMA",
    "C1C0_RUNNER_CONTRACT_ID",
    "C1C0_RUNNER_PROFILE",
    "C1C0_RUNNER_VERSION",
    "C1C0Authorization",
    "C1C0OperationError",
    "C1C0Plan",
    "execute",
    "issue_authorization",
    "parse_st_intervals",
    "prepare_plan",
    "verify_observation",
]
