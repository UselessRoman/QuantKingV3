"""S1-07C1C0-R1 controlled offline XTData ST observation operation.

This is a versioned extension of the C1A/C1B REAL control family, not a
parallel lightweight authorization route.  It reuses C1B's external-path
checks, C1A's structured identity and signed claim receipt, and the explicit
XTQuant module loader.  The only SDK call remains fixed in the adapter.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from quantking.common.errors import InvalidIdError
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.sources.xtquant import ExplicitXtQuantModuleLoader
from quantking.data.adapters.sources.xtquant_c1c0_runner import read_fixed_st_observation
from quantking.data.adapters.sources.xtquant_metadata import ExplicitXtQuantPackageMetadataProbe
from quantking.data.validation.c1b_operation import (
    C1BOperationError,
)
from quantking.data.validation.c1b_operation import (
    _absolute_path as _c1b_absolute_path,
)
from quantking.data.validation.c1b_operation import (
    _is_same_or_descendant as _c1b_is_same_or_descendant,
)
from quantking.data.validation.contracts_v3 import ValidationAttemptIdentityV3
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    RealAuthorizationError,
    RealClaimReceipt,
    RealClaimReceiptIssuer,
    RealClaimReceiptVerifier,
    RealVerifierTrustAnchor,
)

C1C0_PLAN_SCHEMA = "s1-07c1c0-r1-miniqmt-xtdata-plan.v1"
C1C0_AUTHORIZATION_SCHEMA = "s1-07c1c0-r1-miniqmt-xtdata-authorization.v1"
C1C0_OBSERVATION_SCHEMA = "s1-07c1c0-r1-provider-observation.v1"
C1C0_PURPOSE = "S1-07C1C0_R1_MINIQMT_XTDATA_ST_OBSERVATION"
C1C0_EXECUTION_ACK = "I_ACKNOWLEDGE_C1C0_R1_AUTHORIZED_XTDATA_ST_OBSERVATION"
C1C0_RUNNER_CONTRACT_ID = "xtquant.xtdata.get_his_st_data.603377-sh.v1"
C1C0_RUNNER_PROFILE = "miniqmt-xtdata-readonly-st-observation.c1c0-r1.v1"
C1C0_RUNNER_VERSION = "c1c0-r1.xtdata-runner.v1"
C1C0_ADAPTER_VERSION = "xtquant-xtdata-readonly.c1c0-r1.v1"
_SIGN_DOMAIN = "quantking.s1-07c1c0-r1.authorization.v1"
_ARTIFACT_DOMAIN = "quantking.s1-07c1c0-r1.observation.v1"
_FIXED_REQUEST = {"code": "603377.SH", "interface": "xtdata.get_his_st_data"}
_PLAN_FIELDS = frozenset(
    {
        "adapter_version",
        "authorization_allowed_key_ids",
        "authorization_private_key_file",
        "authorization_public_key_file",
        "claim_nonce",
        "claim_root",
        "expires_at",
        "fixed_request",
        "identity",
        "issued_context",
        "output_root",
        "package_version",
        "plan_sha256",
        "runner_allowed_key_ids",
        "runner_contract_id",
        "runner_private_key_file",
        "runner_profile",
        "runner_public_key_file",
        "runner_version",
        "schema_version",
        "site_packages_root",
        "valid_from",
    }
)
_AUTH_FIELDS = frozenset({"authorization_key_id", "plan", "purpose", "schema_version", "signature"})
_OBSERVATION_FILES = frozenset(
    {
        "authorization.json",
        "claim-receipt.json",
        "observation.json",
        "plan.json",
        "response.canonical.json",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "adapter_version",
        "authorization_sha256",
        "claim_receipt_sha256",
        "conclusion",
        "files",
        "event_order",
        "fixed_request",
        "observation_sha256",
        "package_version",
        "plan_sha256",
        "response_sha256",
        "runner_contract_id",
        "runner_key_id",
        "runner_profile",
        "runner_version",
        "schema_version",
        "signature",
    }
)
_ALLOWED_STATUS = frozenset({"ST", "*ST", "PT"})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha(value: bytes) -> str:
    return sha256(value).hexdigest()


class C1C0OperationError(ValueError):
    """Stable failure before a C1C0-R1 observation can be trusted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _external_path(path: Path, *, field: str, require_file: bool = False) -> Path:
    """Use the accepted C1B repository/reparse-point path policy unchanged."""
    try:
        return _c1b_absolute_path(
            str(path), field=field, require_file=require_file, require_external_to_repository=True
        )
    except C1BOperationError as error:
        raise C1C0OperationError("C1C0_PATH_UNSAFE", f"{field} is unsafe") from error


def _direct_child(root: Path, *parts: str) -> Path:
    """Construct a verified descendant only from structured UUID-derived names."""
    candidate = root.joinpath(*parts)
    try:
        safe = _c1b_absolute_path(
            str(candidate), field="C1C0 artifact path", require_external_to_repository=True
        )
    except C1BOperationError as error:
        raise C1C0OperationError("C1C0_PATH_UNSAFE", "C1C0 artifact path is unsafe") from error
    if not _c1b_is_same_or_descendant(safe, root):
        raise C1C0OperationError("C1C0_PATH_ESCAPE", "C1C0 artifact path escapes its root")
    return safe


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(type(item) is not str for item in value):
        raise ValueError
    values = tuple(value)
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError
    return values


@dataclass(frozen=True, slots=True)
class C1C0Plan:
    """The complete pre-claim contract. All fields are signed exactly."""

    identity: ValidationAttemptIdentityV3
    site_packages_root: Path
    package_version: str
    output_root: Path
    claim_root: Path
    authorization_private_key_file: Path
    authorization_public_key_file: Path
    authorization_allowed_key_ids: tuple[str, ...]
    runner_private_key_file: Path
    runner_public_key_file: Path
    runner_allowed_key_ids: tuple[str, ...]
    valid_from: UtcTimestamp
    expires_at: UtcTimestamp
    claim_nonce: str
    plan_sha256: str

    def unsigned(self) -> dict[str, object]:
        return {
            "adapter_version": C1C0_ADAPTER_VERSION,
            "authorization_allowed_key_ids": list(self.authorization_allowed_key_ids),
            "authorization_private_key_file": str(self.authorization_private_key_file),
            "authorization_public_key_file": str(self.authorization_public_key_file),
            "claim_nonce": self.claim_nonce,
            "claim_root": str(self.claim_root),
            "expires_at": self.expires_at.to_iso(),
            "fixed_request": _FIXED_REQUEST,
            "identity": self.identity.to_dict(),
            "issued_context": {
                "case_id": "st_warning_603377_sh",
                "dataset": "trading_status",
                "source": "XTQUANT",
            },
            "output_root": str(self.output_root),
            "package_version": self.package_version,
            "runner_allowed_key_ids": list(self.runner_allowed_key_ids),
            "runner_contract_id": C1C0_RUNNER_CONTRACT_ID,
            "runner_private_key_file": str(self.runner_private_key_file),
            "runner_profile": C1C0_RUNNER_PROFILE,
            "runner_public_key_file": str(self.runner_public_key_file),
            "runner_version": C1C0_RUNNER_VERSION,
            "schema_version": C1C0_PLAN_SCHEMA,
            "site_packages_root": str(self.site_packages_root),
            "valid_from": self.valid_from.to_iso(),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned(), "plan_sha256": self.plan_sha256}

    def to_bytes(self) -> bytes:
        return _canonical(self.to_dict())

    @property
    def claim_file(self) -> Path:
        return _direct_child(self.claim_root, self.identity.authorization_id.value.hex + ".json")

    @property
    def output_directory(self) -> Path:
        return _direct_child(
            self.output_root,
            "provider-observations",
            "xtquant",
            "trading_status",
            self.identity.assessment_id.value.hex,
            self.identity.run_id.value.hex,
        )


def _validate_plan(plan: C1C0Plan) -> None:
    if (
        not isinstance(plan.identity, ValidationAttemptIdentityV3)
        or not isinstance(plan.package_version, str)
        or not plan.package_version
        or not isinstance(plan.claim_nonce, str)
        or len(plan.claim_nonce) != 64
        or any(character not in "0123456789abcdef" for character in plan.claim_nonce)
        or plan.expires_at.value <= plan.valid_from.value
        or plan.claim_root == plan.output_root
        or _sha(_canonical(plan.unsigned())) != plan.plan_sha256
    ):
        raise C1C0OperationError("C1C0_PLAN_INVALID", "C1C0 plan bindings are invalid")
    # Resolving these before claim proves that both names remain contained.
    _ = plan.claim_file, plan.output_directory


def prepare_plan(
    *,
    assessment_id: str,
    authorization_id: str,
    invocation_id: str,
    session_id: str,
    run_id: str,
    site_packages_root: Path,
    package_version: str,
    output_root: Path,
    claim_root: Path,
    authorization_private_key_file: Path,
    authorization_public_key_file: Path,
    authorization_allowed_key_ids: tuple[str, ...],
    runner_private_key_file: Path,
    runner_public_key_file: Path,
    runner_allowed_key_ids: tuple[str, ...],
    valid_from: str,
    expires_at: str,
    claim_nonce: str,
) -> C1C0Plan:
    """Build a structured, external-path plan without importing an SDK."""
    try:
        identity = ValidationAttemptIdentityV3.from_dict(
            {
                "assessment_id": assessment_id,
                "authorization_id": authorization_id,
                "invocation_id": invocation_id,
                "session_id": session_id,
                "run_id": run_id,
            }
        )
        provisional = C1C0Plan(
            identity=identity,
            site_packages_root=_external_path(site_packages_root, field="site-packages root"),
            package_version=package_version,
            output_root=_external_path(output_root, field="output root"),
            claim_root=_external_path(claim_root, field="claim root"),
            authorization_private_key_file=_external_path(
                authorization_private_key_file,
                field="authorization private key",
                require_file=True,
            ),
            authorization_public_key_file=_external_path(
                authorization_public_key_file, field="authorization public key", require_file=True
            ),
            authorization_allowed_key_ids=tuple(authorization_allowed_key_ids),
            runner_private_key_file=_external_path(
                runner_private_key_file, field="runner private key", require_file=True
            ),
            runner_public_key_file=_external_path(
                runner_public_key_file, field="runner public key", require_file=True
            ),
            runner_allowed_key_ids=tuple(runner_allowed_key_ids),
            valid_from=UtcTimestamp.from_iso(valid_from),
            expires_at=UtcTimestamp.from_iso(expires_at),
            claim_nonce=claim_nonce,
            plan_sha256="",
        )
        if (
            _string_list(list(provisional.authorization_allowed_key_ids))
            != provisional.authorization_allowed_key_ids
            or _string_list(list(provisional.runner_allowed_key_ids))
            != provisional.runner_allowed_key_ids
        ):
            raise ValueError
    except (InvalidIdError, TypeError, ValueError) as error:
        raise C1C0OperationError(
            "C1C0_PARAMETER_INVALID", "C1C0 plan parameters are invalid"
        ) from error
    plan = C1C0Plan(
        **{
            **{
                field: getattr(provisional, field)
                for field in provisional.__dataclass_fields__
                if field != "plan_sha256"
            },
            "plan_sha256": _sha(_canonical(provisional.unsigned())),
        }
    )
    _validate_plan(plan)
    return plan


def _plan_from_dict(raw: object) -> C1C0Plan:
    if (
        not isinstance(raw, dict)
        or set(raw) != _PLAN_FIELDS
        or any(key not in raw for key in _PLAN_FIELDS)
    ):
        raise ValueError
    if (
        type(raw["package_version"]) is not str
        or type(raw["claim_nonce"]) is not str
        or type(raw["plan_sha256"]) is not str
        or not isinstance(raw["identity"], dict)
        or not isinstance(raw["fixed_request"], dict)
        or raw["fixed_request"] != _FIXED_REQUEST
        or raw["issued_context"]
        != {"case_id": "st_warning_603377_sh", "dataset": "trading_status", "source": "XTQUANT"}
        or any(
            type(raw[name]) is not str
            for name in (
                "site_packages_root",
                "output_root",
                "claim_root",
                "authorization_private_key_file",
                "authorization_public_key_file",
                "runner_private_key_file",
                "runner_public_key_file",
                "valid_from",
                "expires_at",
            )
        )
    ):
        raise ValueError
    plan = C1C0Plan(
        identity=ValidationAttemptIdentityV3.from_dict(cast(Mapping[str, object], raw["identity"])),
        site_packages_root=_external_path(
            Path(raw["site_packages_root"]), field="site-packages root"
        ),
        package_version=raw["package_version"],
        output_root=_external_path(Path(raw["output_root"]), field="output root"),
        claim_root=_external_path(Path(raw["claim_root"]), field="claim root"),
        authorization_private_key_file=_external_path(
            Path(raw["authorization_private_key_file"]),
            field="authorization private key",
            require_file=True,
        ),
        authorization_public_key_file=_external_path(
            Path(raw["authorization_public_key_file"]),
            field="authorization public key",
            require_file=True,
        ),
        authorization_allowed_key_ids=_string_list(raw["authorization_allowed_key_ids"]),
        runner_private_key_file=_external_path(
            Path(raw["runner_private_key_file"]), field="runner private key", require_file=True
        ),
        runner_public_key_file=_external_path(
            Path(raw["runner_public_key_file"]), field="runner public key", require_file=True
        ),
        runner_allowed_key_ids=_string_list(raw["runner_allowed_key_ids"]),
        valid_from=UtcTimestamp.from_iso(raw["valid_from"]),
        expires_at=UtcTimestamp.from_iso(raw["expires_at"]),
        claim_nonce=raw["claim_nonce"],
        plan_sha256=raw["plan_sha256"],
    )
    if raw != plan.to_dict():
        raise ValueError
    _validate_plan(plan)
    return plan


@dataclass(frozen=True, slots=True)
class C1C0Authorization:
    plan: C1C0Plan
    authorization_key_id: str
    signature: str

    def unsigned(self) -> dict[str, object]:
        return {
            "authorization_key_id": self.authorization_key_id,
            "plan": self.plan.to_dict(),
            "purpose": C1C0_PURPOSE,
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
            private_key_file=_external_path(
                authorization_private_key_file, field="authorization private key", require_file=True
            )
        )
        anchor = RealVerifierTrustAnchor(
            public_key_file=plan.authorization_public_key_file,
            allowed_key_ids=frozenset(plan.authorization_allowed_key_ids),
        )
    except RealAuthorizationError as error:
        raise C1C0OperationError(
            "C1C0_KEY_INVALID", "authorization signing key is invalid"
        ) from error
    if signer.key_id != anchor.key_id:
        raise C1C0OperationError("C1C0_KEY_INVALID", "authorization signing key changed")
    unsigned = C1C0Authorization(plan=plan, authorization_key_id=signer.key_id, signature="0" * 128)
    return C1C0Authorization(
        plan=plan,
        authorization_key_id=signer.key_id,
        signature=signer.sign(domain=_SIGN_DOMAIN, payload=unsigned.unsigned()),
    )


def write_authorization(*, authorization: C1C0Authorization, authorization_file: Path) -> None:
    target = _external_path(authorization_file, field="authorization file")
    if target.exists():
        raise C1C0OperationError("C1C0_OUTPUT_EXISTS", "authorization output already exists")
    _write_exclusive(target, authorization.to_bytes())


def _authorization_from_bytes(payload: bytes) -> C1C0Authorization:
    try:
        raw: Any = json.loads(payload)
        if (
            not isinstance(raw, dict)
            or set(raw) != _AUTH_FIELDS
            or payload != _canonical(raw)
            or any(
                type(raw[field]) is not str
                for field in ("authorization_key_id", "purpose", "schema_version", "signature")
            )
        ):
            raise ValueError
        if raw["purpose"] != C1C0_PURPOSE or raw["schema_version"] != C1C0_AUTHORIZATION_SCHEMA:
            raise ValueError
        authorization = C1C0Authorization(
            plan=_plan_from_dict(raw["plan"]),
            authorization_key_id=raw["authorization_key_id"],
            signature=raw["signature"],
        )
        if authorization.to_bytes() != payload:
            raise ValueError
        return authorization
    except (KeyError, TypeError, ValueError, C1C0OperationError) as error:
        raise C1C0OperationError(
            "C1C0_AUTHORIZATION_INVALID", "C1C0 authorization is invalid"
        ) from error


def _precheck(
    authorization: C1C0Authorization,
) -> tuple[RealVerifierTrustAnchor, ExplicitLocalRealKey]:
    plan = authorization.plan
    _validate_plan(plan)
    if not plan.valid_from.value <= utc_now().value < plan.expires_at.value:
        raise C1C0OperationError(
            "AUTHORIZATION_INVALID_OR_EXPIRED", "authorization is outside its validity window"
        )
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
    except (RealAuthorizationError, ValueError) as error:
        raise C1C0OperationError(
            "C1C0_PRECLAIM_BINDING_MISMATCH", "C1C0 trust or package binding changed"
        ) from error
    if (
        authorization.authorization_key_id != authorization_anchor.key_id
        or not authorization_anchor.verify(
            domain=_SIGN_DOMAIN, payload=authorization.unsigned(), signature=authorization.signature
        )
        or runner.key_id != runner_anchor.key_id
        or version != plan.package_version
    ):
        raise C1C0OperationError("C1C0_PRECLAIM_BINDING_MISMATCH", "C1C0 signed binding changed")
    return authorization_anchor, runner


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _claim(plan: C1C0Plan, authorization_bytes: bytes) -> bytes:
    try:
        issuer = RealClaimReceiptIssuer(private_key_file=plan.authorization_private_key_file)
        receipt = issuer.issue(
            authorization_id=plan.identity.authorization_id,
            invocation_id=plan.identity.invocation_id,
            run_id=plan.identity.run_id,
            claimed_at=utc_now(),
            claim_nonce=plan.claim_nonce,
            authorization_envelope_sha256=_sha(authorization_bytes),
            plan_sha256=plan.plan_sha256,
        ).to_bytes()
        _write_exclusive(plan.claim_file, receipt)
        return receipt
    except (OSError, RealAuthorizationError) as error:
        raise C1C0OperationError("C1C0_CLAIM_FAILED", "C1C0 claim cannot be persisted") from error


def _date(raw: object) -> str:
    if type(raw) is not str:
        raise ValueError
    compact = raw.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError
    return date(int(compact[:4]), int(compact[4:6]), int(compact[6:])).isoformat()


def parse_st_intervals(response: object) -> dict[str, object]:
    """Accept exactly ``{status: [[start, end], ...]}``, preserving provider order."""
    if response == {}:
        return {"intervals": [], "observation_state": "INDETERMINATE"}
    if (
        not isinstance(response, dict)
        or not response
        or any(type(key) is not str or key not in _ALLOWED_STATUS for key in response)
    ):
        raise ValueError("ST response shape is invalid")
    intervals: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for status, rows in response.items():
        if not isinstance(rows, list):
            raise ValueError("ST response rows are invalid")
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("ST response interval is invalid")
            start, end = _date(row[0]), _date(row[1])
            item = (status, start, end)
            if end <= start or item in seen:
                raise ValueError("ST response interval is non-unique or non-positive")
            if any(not (end <= old[1] or start >= old[2]) for old in seen):
                raise ValueError("ST response intervals overlap")
            seen.add(item)
            intervals.append({"status": status, "start_date": start, "end_date": end})
    return {
        "intervals": intervals,
        "observation_state": "OBSERVED_INTERVALS" if intervals else "OBSERVED_EMPTY_INTERVALS",
    }


def _staging_directory(plan: C1C0Plan) -> Path:
    final = plan.output_directory
    return _direct_child(final.parent, final.name + ".incomplete")


def _write_failure(plan: C1C0Plan, claim: bytes, category: str) -> None:
    staging = _staging_directory(plan)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        failure = _direct_child(staging, "failure.json")
        if not failure.exists():
            _write_exclusive(
                failure,
                _canonical(
                    {
                        "category": category,
                        "claim_receipt_sha256": _sha(claim),
                        "plan_sha256": plan.plan_sha256,
                        "retryable": False,
                    }
                ),
            )
    except OSError:
        return


def _publish_observation(
    *,
    authorization_bytes: bytes,
    authorization: C1C0Authorization,
    claim: bytes,
    response: object,
    parsed: dict[str, object],
    runner: ExplicitLocalRealKey,
) -> Path:
    plan = authorization.plan
    final, staging = plan.output_directory, _staging_directory(plan)
    if final.exists() or staging.exists():
        raise C1C0OperationError("C1C0_OUTPUT_EXISTS", "C1C0 observation output already exists")
    staging.mkdir(parents=True, exist_ok=False)
    response_bytes = _canonical(response)
    observation_bytes = _canonical(
        {
            "conclusion": "BLOCKED/CONTRACT_INCOMPLETE",
            "fixed_request": _FIXED_REQUEST,
            "parsed": parsed,
            "runner_contract_id": C1C0_RUNNER_CONTRACT_ID,
            "runner_profile": C1C0_RUNNER_PROFILE,
            "runner_version": C1C0_RUNNER_VERSION,
        }
    )
    files = {
        "authorization.json": authorization_bytes,
        "claim-receipt.json": claim,
        "observation.json": observation_bytes,
        "plan.json": plan.to_bytes(),
        "response.canonical.json": response_bytes,
    }
    for name, payload in files.items():
        _write_exclusive(_direct_child(staging, name), payload)
    inventory = {name: _sha(payload) for name, payload in sorted(files.items())}
    unsigned = {
        "adapter_version": C1C0_ADAPTER_VERSION,
        "authorization_sha256": _sha(authorization_bytes),
        "claim_receipt_sha256": _sha(claim),
        "conclusion": "BLOCKED/CONTRACT_INCOMPLETE",
        "event_order": ["PRECHECK", "CLAIM", "IMPORT", "CALL", "OUTPUT"],
        "files": inventory,
        "fixed_request": _FIXED_REQUEST,
        "observation_sha256": _sha(observation_bytes),
        "package_version": plan.package_version,
        "plan_sha256": plan.plan_sha256,
        "response_sha256": _sha(response_bytes),
        "runner_contract_id": C1C0_RUNNER_CONTRACT_ID,
        "runner_key_id": runner.key_id,
        "runner_profile": C1C0_RUNNER_PROFILE,
        "runner_version": C1C0_RUNNER_VERSION,
        "schema_version": C1C0_OBSERVATION_SCHEMA,
    }
    _write_exclusive(
        _direct_child(staging, "manifest.json"),
        _canonical(
            {**unsigned, "signature": runner.sign(domain=_ARTIFACT_DOMAIN, payload=unsigned)}
        ),
    )
    try:
        if {item.name for item in staging.iterdir()} != _OBSERVATION_FILES | {
            "manifest.json"
        } or any(
            _sha((staging / name).read_bytes()) != digest for name, digest in inventory.items()
        ):
            raise OSError
        if not _c1b_is_same_or_descendant(
            staging.resolve(strict=True), plan.output_root
        ) or not _c1b_is_same_or_descendant(final.resolve(strict=False), plan.output_root):
            raise OSError
        os.replace(staging, final)
    except OSError as error:
        raise C1C0OperationError(
            "C1C0_OUTPUT_PUBLISH_FAILED", "C1C0 observation could not be published"
        ) from error
    return final


def execute(*, authorization_file: Path, acknowledgement: str) -> Path:
    """Perform the sole C1C0-R1 dispatch: PRECHECK → CLAIM → IMPORT → CALL → OUTPUT."""
    if acknowledgement != C1C0_EXECUTION_ACK:
        raise C1C0OperationError(
            "C1C0_ACKNOWLEDGEMENT_REQUIRED", "exact C1C0 acknowledgement is required"
        )
    try:
        authorization_bytes = _external_path(
            authorization_file, field="authorization file", require_file=True
        ).read_bytes()
    except OSError as error:
        raise C1C0OperationError(
            "C1C0_AUTHORIZATION_INVALID", "authorization cannot be read"
        ) from error
    authorization = _authorization_from_bytes(authorization_bytes)
    _anchor, runner = _precheck(authorization)
    claim = _claim(authorization.plan, authorization_bytes)
    try:
        response = read_fixed_st_observation(
            site_packages_root=authorization.plan.site_packages_root,
            module_loader=ExplicitXtQuantModuleLoader,
        )
    except Exception as error:
        _write_failure(authorization.plan, claim, "C1C0_IMPORT_OR_CALL_FAILED")
        raise C1C0OperationError(
            "C1C0_IMPORT_OR_CALL_FAILED", "XTData read failed after claim"
        ) from error
    try:
        parsed = parse_st_intervals(response)
        return _publish_observation(
            authorization_bytes=authorization_bytes,
            authorization=authorization,
            claim=claim,
            response=response,
            parsed=parsed,
            runner=runner,
        )
    except Exception as error:
        _write_failure(authorization.plan, claim, "C1C0_RESPONSE_OR_OUTPUT_FAILED")
        if isinstance(error, C1C0OperationError):
            raise
        raise C1C0OperationError(
            "C1C0_RESPONSE_PARSE_FAILED", "XTData response failed strict parsing"
        ) from error


def verify_observation(*, directory: Path) -> dict[str, object]:
    """Deep verify exactly the published C1C0-R1 files using signed-plan anchors."""
    try:
        names = {path.name for path in directory.iterdir()}
        if names != _OBSERVATION_FILES | {"manifest.json"}:
            raise ValueError
        auth_bytes = (directory / "authorization.json").read_bytes()
        authorization = _authorization_from_bytes(auth_bytes)
        plan = authorization.plan
        auth_anchor = RealVerifierTrustAnchor(
            public_key_file=plan.authorization_public_key_file,
            allowed_key_ids=frozenset(plan.authorization_allowed_key_ids),
        )
        runner_anchor = RealVerifierTrustAnchor(
            public_key_file=plan.runner_public_key_file,
            allowed_key_ids=frozenset(plan.runner_allowed_key_ids),
        )
        if authorization.authorization_key_id != auth_anchor.key_id or not auth_anchor.verify(
            domain=_SIGN_DOMAIN, payload=authorization.unsigned(), signature=authorization.signature
        ):
            raise ValueError
        claim = RealClaimReceipt.from_bytes((directory / "claim-receipt.json").read_bytes())
        if (
            not RealClaimReceiptVerifier(trust_anchor=auth_anchor).verify(claim)
            or claim.authorization_id != plan.identity.authorization_id
            or claim.invocation_id != plan.identity.invocation_id
            or claim.run_id != plan.identity.run_id
            or claim.plan_sha256 != plan.plan_sha256
            or claim.authorization_envelope_sha256 != _sha(auth_bytes)
        ):
            raise ValueError
        manifest_bytes = (directory / "manifest.json").read_bytes()
        manifest: Any = json.loads(manifest_bytes)
        if (
            not isinstance(manifest, dict)
            or manifest_bytes != _canonical(manifest)
            or set(manifest) != _MANIFEST_FIELDS
        ):
            raise ValueError
        if (
            not isinstance(manifest["files"], dict)
            or set(manifest["files"]) != _OBSERVATION_FILES
            or any(type(value) is not str for value in manifest["files"].values())
        ):
            raise ValueError
        if any(
            _sha((directory / name).read_bytes()) != digest
            for name, digest in manifest["files"].items()
        ):
            raise ValueError
        unsigned = dict(manifest)
        signature = unsigned.pop("signature")
        if manifest["runner_key_id"] != runner_anchor.key_id or not runner_anchor.verify(
            domain=_ARTIFACT_DOMAIN, payload=unsigned, signature=signature
        ):
            raise ValueError
        response_bytes = (directory / "response.canonical.json").read_bytes()
        response = json.loads(response_bytes)
        parsed = parse_st_intervals(response)
        observation_bytes = (directory / "observation.json").read_bytes()
        observation = json.loads(observation_bytes)
        if (
            response_bytes != _canonical(response)
            or (directory / "plan.json").read_bytes() != plan.to_bytes()
            or not isinstance(observation, dict)
            or set(observation)
            != {
                "conclusion",
                "fixed_request",
                "parsed",
                "runner_contract_id",
                "runner_profile",
                "runner_version",
            }
            or manifest["event_order"] != ["PRECHECK", "CLAIM", "IMPORT", "CALL", "OUTPUT"]
            or manifest["plan_sha256"] != plan.plan_sha256
            or manifest["authorization_sha256"] != _sha(auth_bytes)
            or manifest["claim_receipt_sha256"] != _sha(claim.to_bytes())
            or manifest["response_sha256"] != _sha(response_bytes)
            or manifest["observation_sha256"] != _sha(observation_bytes)
            or manifest["fixed_request"] != _FIXED_REQUEST
            or manifest["conclusion"] != "BLOCKED/CONTRACT_INCOMPLETE"
            or observation.get("parsed") != parsed
            or observation.get("conclusion") != "BLOCKED/CONTRACT_INCOMPLETE"
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
    "C1C0_EXECUTION_ACK",
    "C1C0OperationError",
    "C1C0Plan",
    "execute",
    "issue_authorization",
    "parse_st_intervals",
    "prepare_plan",
    "verify_observation",
    "write_authorization",
]
