"""S1-07C1C0-R2 controlled offline XTData ST observation operation.

The C1C0 authorization is an extension of a verified C1B authorization, which
in turn carries the accepted C1A runtime envelope.  C1C0 never treats that
context as a C1B dispatch: only its own schema and acknowledgement can reach
the XTData adapter.  This module does not import ``xtquant``; the SDK boundary
is reached only after the durable, verified claim has been written.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.sources.xtquant import ExplicitXtQuantModuleLoader
from quantking.data.adapters.sources.xtquant_c1c0_runner import (
    FIXED_ST_REQUEST,
    RUNNER_CONTRACT_ID,
    RUNNER_PROFILE,
    RUNNER_VERSION,
    read_fixed_st_observation,
)
from quantking.data.adapters.sources.xtquant_metadata import ExplicitXtQuantPackageMetadataProbe
from quantking.data.validation import c1b_operation
from quantking.data.validation.c1b_operation import (
    C1BExecutionAuthorization,
    C1BExecutionPlan,
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
    RealRuntimeAuthorizationVerifier,
    RealVerifierTrustAnchor,
)

C1C0_PLAN_SCHEMA = "s1-07c1c0-r2-miniqmt-xtdata-plan.v1"
C1C0_AUTHORIZATION_SCHEMA = "s1-07c1c0-r2-miniqmt-xtdata-authorization.v1"
C1C0_OBSERVATION_SCHEMA = "s1-07c1c0-r2-provider-observation.v1"
C1C0_PURPOSE = "S1-07C1C0_R2_MINIQMT_XTDATA_ST_OBSERVATION"
C1C0_EXECUTION_ACK = "I_ACKNOWLEDGE_C1C0_R2_AUTHORIZED_XTDATA_ST_OBSERVATION"
C1C0_ADAPTER_VERSION = "xtquant-xtdata-readonly.c1c0-r2.v1"
C1C0_RUNNER_CONTRACT_ID = RUNNER_CONTRACT_ID
C1C0_RUNNER_PROFILE = RUNNER_PROFILE
C1C0_RUNNER_VERSION = RUNNER_VERSION

_SIGN_DOMAIN = "quantking.s1-07c1c0-r2.authorization.v1"
_ARTIFACT_DOMAIN = "quantking.s1-07c1c0-r2.observation.v1"
_CONCLUSION = "BLOCKED/CONTRACT_INCOMPLETE"
_EVENT_ORDER = ["PRECHECK", "CLAIM", "IMPORT", "CALL", "OUTPUT"]
_PLAN_FIELDS = frozenset(
    {
        "adapter_version",
        "c1b_authorization",
        "c1b_authorization_sha256",
        "fixed_request",
        "package_version",
        "plan_sha256",
        "purpose",
        "runner_contract_id",
        "runner_profile",
        "runner_version",
        "schema_version",
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
_OBSERVATION_FIELDS = frozenset(
    {
        "adapter_version",
        "conclusion",
        "fixed_request",
        "package_version",
        "parsed",
        "runner_contract_id",
        "runner_profile",
        "runner_version",
        "schema_version",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "adapter_version",
        "authorization_sha256",
        "c1b_authorization_sha256",
        "claim_receipt_sha256",
        "conclusion",
        "event_order",
        "files",
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
_C1A_PLAN_FIELDS = frozenset(
    {
        "case_ids",
        "dataset",
        "identity",
        "output_scope",
        "plan_sha256",
        "promotion_profile",
        "runner_key_id",
        "sample_spec_sha256",
        "source",
        "source_dataset_contract",
        "versions",
    }
)
_ALLOWED_STATUS = frozenset({"ST", "*ST", "PT"})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha(value: bytes) -> str:
    return sha256(value).hexdigest()


def _is_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


class C1C0OperationError(ValueError):
    """Stable failure before a C1C0-R2 observation can be trusted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _external_path(path: Path, *, field: str, require_file: bool = False) -> Path:
    """Apply the accepted C1B repository and reparse-point path policy."""
    try:
        return _c1b_absolute_path(
            str(path), field=field, require_file=require_file, require_external_to_repository=True
        )
    except C1BOperationError as error:
        raise C1C0OperationError("C1C0_PATH_UNSAFE", f"{field} is unsafe") from error


def _direct_child(root: Path, *parts: str) -> Path:
    """Construct a verified descendant only from typed identity values and constants."""
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


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except (OSError, RuntimeError):
        return False


@dataclass(frozen=True, slots=True)
class C1C0Plan:
    """A signed C1C0 contract which embeds the complete C1B/C1A context."""

    c1b_authorization: C1BExecutionAuthorization
    c1b_authorization_sha256: str
    package_version: str
    plan_sha256: str

    @property
    def c1b_plan(self) -> C1BExecutionPlan:
        return self.c1b_authorization.plan

    @property
    def identity(self) -> ValidationAttemptIdentityV3:
        return self.c1b_plan.identity

    @property
    def claim_nonce(self) -> str:
        return self.c1b_plan.claim_nonce

    @property
    def valid_from(self) -> UtcTimestamp:
        return self.c1b_plan.valid_from

    @property
    def expires_at(self) -> UtcTimestamp:
        return self.c1b_plan.expires_at

    @property
    def site_packages_root(self) -> Path:
        return self.c1b_plan.xtquant_site_packages_root

    @property
    def claim_file(self) -> Path:
        return _direct_child(
            self.c1b_plan.claim_root, self.identity.authorization_id.value.hex + ".json"
        )

    @property
    def output_directory(self) -> Path:
        return _direct_child(
            self.c1b_plan.output_root,
            "provider-observations",
            "xtquant",
            "trading_status",
            self.identity.assessment_id.value.hex,
            self.identity.run_id.value.hex,
        )

    def unsigned(self) -> dict[str, object]:
        return {
            "adapter_version": C1C0_ADAPTER_VERSION,
            "c1b_authorization": json.loads(self.c1b_authorization.to_bytes()),
            "c1b_authorization_sha256": self.c1b_authorization_sha256,
            "fixed_request": FIXED_ST_REQUEST,
            "package_version": self.package_version,
            "purpose": C1C0_PURPOSE,
            "runner_contract_id": C1C0_RUNNER_CONTRACT_ID,
            "runner_profile": C1C0_RUNNER_PROFILE,
            "runner_version": C1C0_RUNNER_VERSION,
            "schema_version": C1C0_PLAN_SCHEMA,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned(), "plan_sha256": self.plan_sha256}

    def to_bytes(self) -> bytes:
        return _canonical(self.to_dict())


def _verify_embedded_c1b_context(
    authorization: C1BExecutionAuthorization,
) -> tuple[RealVerifierTrustAnchor, RealVerifierTrustAnchor]:
    """Verify the full signed C1B wrapper and its signed C1A envelope publicly.

    This deliberately does not call C1B's runtime builder: that builder opens
    private keys and is inappropriate for an artifact's independent read side.
    The complete C1B plan and C1A envelope are instead verified directly from
    their canonical signed bytes and public trust anchor.
    """
    plan = authorization.plan
    envelope = authorization.c1a_authorization
    try:
        authorization_anchor = RealVerifierTrustAnchor(
            public_key_file=plan.keys.authorization_public_key_file,
            allowed_key_ids=frozenset(plan.keys.authorization_allowed_key_ids),
        )
        runner_anchor = RealVerifierTrustAnchor(
            public_key_file=plan.keys.runner_public_key_file,
            allowed_key_ids=frozenset(plan.keys.runner_allowed_key_ids),
        )
    except RealAuthorizationError as error:
        raise C1C0OperationError(
            "C1C0_CONTEXT_UNTRUSTED", "C1B trust anchors are invalid"
        ) from error
    if (
        authorization.authorization_key_id != plan.keys.authorization_key_id
        or authorization.authorization_key_id != authorization_anchor.key_id
        or not authorization_anchor.verify(
            domain=c1b_operation._DOMAIN,
            payload=authorization.unsigned_payload(),
            signature=authorization.signature,
        )
        or plan.keys.runner_key_id != runner_anchor.key_id
        or not RealRuntimeAuthorizationVerifier(
            trust_anchor=authorization_anchor
        ).verify_real_file_authorization(envelope)
    ):
        raise C1C0OperationError("C1C0_CONTEXT_UNTRUSTED", "C1B or C1A signature is untrusted")
    try:
        c1a_plan = dict(plan.c1a_plan)
        if set(c1a_plan) != _C1A_PLAN_FIELDS:
            raise ValueError
        c1a_unsigned = dict(c1a_plan)
        c1a_declared_sha = c1a_unsigned.pop("plan_sha256")
        if (
            not _is_sha(c1a_declared_sha)
            or _sha(_canonical(c1a_unsigned)) != c1a_declared_sha
            or c1a_declared_sha != plan.c1a_plan_sha256
            or c1a_declared_sha != envelope.plan_sha256
            or c1a_plan["source"] != "XTQUANT"
            or c1a_plan["dataset"] != "trading_status"
            or c1a_plan["case_ids"] != ["st_warning_603377_sh"]
            or c1a_plan["identity"] != plan.identity.to_dict()
            or c1a_plan["output_scope"] != str(plan.output_root.resolve(strict=False))
            or c1a_plan["runner_key_id"] != plan.keys.runner_key_id
            or c1a_plan["versions"] != plan.versions.to_dict()
            or c1a_plan["promotion_profile"] != envelope.promotion_profile.to_dict()
            or c1a_plan["source_dataset_contract"] != envelope.source_dataset_contract.to_dict()
            or c1a_plan["sample_spec_sha256"] != envelope.authorization.sample_sha256
            or envelope.identity != plan.identity
            or envelope.authorization.authorization_id != plan.identity.authorization_id
            or envelope.authorization.source != "XTQUANT"
            or envelope.authorization.dataset.value != "trading_status"
            or envelope.authorization.output_root != str(plan.output_root)
            or envelope.authorization.valid_from != plan.valid_from
            or envelope.authorization.expires_at != plan.expires_at
            or envelope.selected_case_ids != ("st_warning_603377_sh",)
            or envelope.reference_bundle_id != plan.reference_bundle_id
            or envelope.reference_bundle_sha256 != plan.reference_bundle_sha256
            or envelope.output_scope != str(plan.output_root)
            or dict(envelope.frozen_versions) != plan.versions.to_dict()
            or envelope.claim_nonce != plan.claim_nonce
            or envelope.runner_key_id != plan.keys.runner_key_id
            or envelope.authorization_key_id != plan.keys.authorization_key_id
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
        raise C1C0OperationError(
            "C1C0_CONTEXT_BINDING_MISMATCH", "C1B/C1A authorization context is unbound"
        ) from error
    return authorization_anchor, runner_anchor


def _validate_plan(plan: C1C0Plan) -> tuple[RealVerifierTrustAnchor, RealVerifierTrustAnchor]:
    if (
        not isinstance(plan.c1b_authorization, C1BExecutionAuthorization)
        or not _is_sha(plan.c1b_authorization_sha256)
        or not isinstance(plan.package_version, str)
        or not plan.package_version
        or not _is_sha(plan.plan_sha256)
        or _sha(plan.c1b_authorization.to_bytes()) != plan.c1b_authorization_sha256
        or plan.package_version != plan.c1b_plan.versions.package_version
        or _sha(_canonical(plan.unsigned())) != plan.plan_sha256
        or plan.expires_at.value <= plan.valid_from.value
        or plan.c1b_plan.claim_root == plan.c1b_plan.output_root
        or plan.c1b_plan.claim_root.is_relative_to(plan.c1b_plan.output_root)
        or plan.c1b_plan.output_root.is_relative_to(plan.c1b_plan.claim_root)
    ):
        raise C1C0OperationError("C1C0_PLAN_INVALID", "C1C0 plan bindings are invalid")
    # Resolve all final destinations before signing, claiming, importing, or writing.
    _ = plan.claim_file, plan.output_directory
    return _verify_embedded_c1b_context(plan.c1b_authorization)


def prepare_plan(*, c1b_authorization: C1BExecutionAuthorization) -> C1C0Plan:
    """Version a C1C0 plan only from an already issued C1B/C1A authorization."""
    try:
        provisional = C1C0Plan(
            c1b_authorization=c1b_authorization,
            c1b_authorization_sha256=_sha(c1b_authorization.to_bytes()),
            package_version=c1b_authorization.plan.versions.package_version,
            plan_sha256="0" * 64,
        )
        plan = C1C0Plan(
            c1b_authorization=provisional.c1b_authorization,
            c1b_authorization_sha256=provisional.c1b_authorization_sha256,
            package_version=provisional.package_version,
            plan_sha256=_sha(_canonical(provisional.unsigned())),
        )
        _validate_plan(plan)
    except (
        AttributeError,
        C1BOperationError,
        RealAuthorizationError,
        TypeError,
        ValueError,
    ) as error:
        if isinstance(error, C1C0OperationError):
            raise
        raise C1C0OperationError(
            "C1C0_PARAMETER_INVALID", "C1C0 plan parameters are invalid"
        ) from error
    return plan


def _plan_from_dict(raw: object) -> C1C0Plan:
    if not isinstance(raw, dict) or set(raw) != _PLAN_FIELDS:
        raise ValueError
    if (
        not isinstance(raw["c1b_authorization"], dict)
        or any(
            type(raw[field]) is not str
            for field in _PLAN_FIELDS - {"c1b_authorization", "fixed_request"}
        )
        or raw["schema_version"] != C1C0_PLAN_SCHEMA
        or raw["purpose"] != C1C0_PURPOSE
        or raw["adapter_version"] != C1C0_ADAPTER_VERSION
        or raw["fixed_request"] != FIXED_ST_REQUEST
        or raw["runner_contract_id"] != C1C0_RUNNER_CONTRACT_ID
        or raw["runner_profile"] != C1C0_RUNNER_PROFILE
        or raw["runner_version"] != C1C0_RUNNER_VERSION
    ):
        raise ValueError
    authorization = C1BExecutionAuthorization.from_bytes(_canonical(raw["c1b_authorization"]))
    plan = C1C0Plan(
        c1b_authorization=authorization,
        c1b_authorization_sha256=raw["c1b_authorization_sha256"],
        package_version=raw["package_version"],
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


def _authorization_signer(
    plan: C1C0Plan, authorization_anchor: RealVerifierTrustAnchor
) -> RealClaimReceiptIssuer:
    try:
        signer = ExplicitLocalRealKey(
            private_key_file=plan.c1b_plan.keys.authorization_private_key_file
        )
        issuer = RealClaimReceiptIssuer(
            private_key_file=plan.c1b_plan.keys.authorization_private_key_file
        )
    except RealAuthorizationError as error:
        raise C1C0OperationError(
            "C1C0_PRECLAIM_BINDING_MISMATCH", "authorization claim signer is unavailable"
        ) from error
    if signer.key_id != authorization_anchor.key_id or issuer.key_id != authorization_anchor.key_id:
        raise C1C0OperationError(
            "C1C0_PRECLAIM_BINDING_MISMATCH", "authorization claim signer changed"
        )
    return issuer


def issue_authorization(*, plan: C1C0Plan) -> C1C0Authorization:
    """Sign only with the same C1B/C1A authorization key already bound in plan."""
    authorization_anchor, _runner_anchor = _validate_plan(plan)
    issuer = _authorization_signer(plan, authorization_anchor)
    unsigned = C1C0Authorization(plan=plan, authorization_key_id=issuer.key_id, signature="0" * 128)
    try:
        signer = ExplicitLocalRealKey(
            private_key_file=plan.c1b_plan.keys.authorization_private_key_file
        )
    except RealAuthorizationError as error:
        raise C1C0OperationError(
            "C1C0_KEY_INVALID", "authorization signing key is invalid"
        ) from error
    return C1C0Authorization(
        plan=plan,
        authorization_key_id=issuer.key_id,
        signature=signer.sign(domain=_SIGN_DOMAIN, payload=unsigned.unsigned()),
    )


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


def write_authorization(*, authorization: C1C0Authorization, authorization_file: Path) -> None:
    target = _external_path(authorization_file, field="authorization file")
    if target.exists():
        raise C1C0OperationError("C1C0_OUTPUT_EXISTS", "authorization output already exists")
    authorization_anchor, _runner_anchor = _validate_plan(authorization.plan)
    _verify_outer_authorization(authorization, authorization_anchor)
    _write_exclusive(target, authorization.to_bytes())


def _authorization_from_bytes(payload: bytes) -> C1C0Authorization:
    try:
        raw: Any = json.loads(payload)
        if (
            not isinstance(raw, dict)
            or set(raw) != _AUTH_FIELDS
            or payload != _canonical(raw)
            or not isinstance(raw["plan"], dict)
            or any(type(raw[field]) is not str for field in _AUTH_FIELDS - {"plan"})
            or raw["purpose"] != C1C0_PURPOSE
            or raw["schema_version"] != C1C0_AUTHORIZATION_SCHEMA
        ):
            raise ValueError
        authorization = C1C0Authorization(
            plan=_plan_from_dict(raw["plan"]),
            authorization_key_id=raw["authorization_key_id"],
            signature=raw["signature"],
        )
        if authorization.to_bytes() != payload:
            raise ValueError
        return authorization
    except (C1BOperationError, KeyError, TypeError, ValueError, C1C0OperationError) as error:
        raise C1C0OperationError(
            "C1C0_AUTHORIZATION_INVALID", "C1C0 authorization is invalid"
        ) from error


def _verify_outer_authorization(
    authorization: C1C0Authorization, authorization_anchor: RealVerifierTrustAnchor
) -> None:
    if (
        authorization.authorization_key_id != authorization_anchor.key_id
        or not authorization_anchor.verify(
            domain=_SIGN_DOMAIN,
            payload=authorization.unsigned(),
            signature=authorization.signature,
        )
    ):
        raise C1C0OperationError("C1C0_AUTHORIZATION_UNTRUSTED", "C1C0 authorization is untrusted")


def _staging_directory(plan: C1C0Plan) -> Path:
    final = plan.output_directory
    return _direct_child(final.parent, final.name + ".incomplete")


def _precheck(
    authorization: C1C0Authorization,
) -> tuple[
    RealVerifierTrustAnchor, RealVerifierTrustAnchor, RealClaimReceiptIssuer, ExplicitLocalRealKey
]:
    authorization_anchor, runner_anchor = _validate_plan(authorization.plan)
    _verify_outer_authorization(authorization, authorization_anchor)
    plan = authorization.plan
    if not plan.valid_from.value <= utc_now().value < plan.expires_at.value:
        raise C1C0OperationError(
            "AUTHORIZATION_INVALID_OR_EXPIRED", "authorization is outside its validity window"
        )
    if (
        plan.claim_file.exists()
        or plan.output_directory.exists()
        or _staging_directory(plan).exists()
    ):
        raise C1C0OperationError(
            "C1C0_OUTPUT_EXISTS", "claim or observation destination already exists"
        )
    issuer = _authorization_signer(plan, authorization_anchor)
    try:
        runner = ExplicitLocalRealKey(private_key_file=plan.c1b_plan.keys.runner_private_key_file)
        version = ExplicitXtQuantPackageMetadataProbe(
            site_packages_root=plan.site_packages_root
        ).exact_version()
    except (RealAuthorizationError, ValueError) as error:
        raise C1C0OperationError(
            "C1C0_PRECLAIM_BINDING_MISMATCH", "runner or package binding changed"
        ) from error
    if runner.key_id != runner_anchor.key_id or version != plan.package_version:
        raise C1C0OperationError("C1C0_PRECLAIM_BINDING_MISMATCH", "signed binding changed")
    return authorization_anchor, runner_anchor, issuer, runner


def _verify_claim(
    *,
    receipt: RealClaimReceipt,
    plan: C1C0Plan,
    authorization_sha256: str,
    authorization_anchor: RealVerifierTrustAnchor,
) -> None:
    if (
        not RealClaimReceiptVerifier(trust_anchor=authorization_anchor).verify(receipt)
        or receipt.authorization_id != plan.identity.authorization_id
        or receipt.invocation_id != plan.identity.invocation_id
        or receipt.run_id != plan.identity.run_id
        or receipt.claim_nonce != plan.claim_nonce
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.authorization_envelope_sha256 != authorization_sha256
        or not plan.valid_from.value <= receipt.claimed_at.value < plan.expires_at.value
    ):
        raise C1C0OperationError("C1C0_CLAIM_INVALID", "C1C0 claim receipt is unbound")


def _claim(
    *,
    plan: C1C0Plan,
    authorization_bytes: bytes,
    authorization_anchor: RealVerifierTrustAnchor,
    issuer: RealClaimReceiptIssuer,
) -> bytes:
    try:
        claimed_at = utc_now()
        receipt = issuer.issue(
            authorization_id=plan.identity.authorization_id,
            invocation_id=plan.identity.invocation_id,
            run_id=plan.identity.run_id,
            claimed_at=claimed_at,
            claim_nonce=plan.claim_nonce,
            authorization_envelope_sha256=_sha(authorization_bytes),
            plan_sha256=plan.plan_sha256,
        )
        _verify_claim(
            receipt=receipt,
            plan=plan,
            authorization_sha256=_sha(authorization_bytes),
            authorization_anchor=authorization_anchor,
        )
        payload = receipt.to_bytes()
        if RealClaimReceipt.from_bytes(payload) != receipt:
            raise RealAuthorizationError("claim canonicalization changed")
        _write_exclusive(plan.claim_file, payload)
        persisted = RealClaimReceipt.from_bytes(plan.claim_file.read_bytes())
        _verify_claim(
            receipt=persisted,
            plan=plan,
            authorization_sha256=_sha(authorization_bytes),
            authorization_anchor=authorization_anchor,
        )
        if persisted != receipt:
            raise RealAuthorizationError("persisted claim receipt changed")
        return persisted.to_bytes()
    except (OSError, RealAuthorizationError, C1C0OperationError) as error:
        if isinstance(error, C1C0OperationError):
            raise
        raise C1C0OperationError("C1C0_CLAIM_FAILED", "C1C0 claim cannot be persisted") from error


def _date(raw: object) -> str:
    if type(raw) is not str:
        raise ValueError
    compact = raw.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError
    return date(int(compact[:4]), int(compact[4:6]), int(compact[6:])).isoformat()


def parse_st_intervals(response: object) -> dict[str, object]:
    """Accept exactly ``{ST|*ST|PT: [[start, end], ...]}`` without rewriting it."""
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
    occupied: list[tuple[str, str]] = []
    for status, rows in response.items():
        if not isinstance(rows, list):
            raise ValueError("ST response rows are invalid")
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("ST response interval is invalid")
            start, end = _date(row[0]), _date(row[1])
            item = (status, start, end)
            if (
                end <= start
                or item in seen
                or any(
                    not (end <= old_start or start >= old_end) for old_start, old_end in occupied
                )
            ):
                raise ValueError("ST response intervals are ambiguous")
            seen.add(item)
            occupied.append((start, end))
            intervals.append({"status": status, "start_date": start, "end_date": end})
    return {
        "intervals": intervals,
        "observation_state": "OBSERVED_INTERVALS" if intervals else "OBSERVED_EMPTY_INTERVALS",
    }


def _observation_payload(plan: C1C0Plan, parsed: dict[str, object]) -> dict[str, object]:
    return {
        "adapter_version": C1C0_ADAPTER_VERSION,
        "conclusion": _CONCLUSION,
        "fixed_request": FIXED_ST_REQUEST,
        "package_version": plan.package_version,
        "parsed": parsed,
        "runner_contract_id": C1C0_RUNNER_CONTRACT_ID,
        "runner_profile": C1C0_RUNNER_PROFILE,
        "runner_version": C1C0_RUNNER_VERSION,
        "schema_version": C1C0_OBSERVATION_SCHEMA,
    }


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


def _manifest_unsigned(
    *,
    plan: C1C0Plan,
    authorization_bytes: bytes,
    claim: bytes,
    response_bytes: bytes,
    observation_bytes: bytes,
    files: dict[str, str],
    runner_key_id: str,
) -> dict[str, object]:
    return {
        "adapter_version": C1C0_ADAPTER_VERSION,
        "authorization_sha256": _sha(authorization_bytes),
        "c1b_authorization_sha256": plan.c1b_authorization_sha256,
        "claim_receipt_sha256": _sha(claim),
        "conclusion": _CONCLUSION,
        "event_order": _EVENT_ORDER,
        "files": files,
        "fixed_request": FIXED_ST_REQUEST,
        "observation_sha256": _sha(observation_bytes),
        "package_version": plan.package_version,
        "plan_sha256": plan.plan_sha256,
        "response_sha256": _sha(response_bytes),
        "runner_contract_id": C1C0_RUNNER_CONTRACT_ID,
        "runner_key_id": runner_key_id,
        "runner_profile": C1C0_RUNNER_PROFILE,
        "runner_version": C1C0_RUNNER_VERSION,
        "schema_version": C1C0_OBSERVATION_SCHEMA,
    }


def _verify_observation_directory(*, directory: Path, allow_staging: bool) -> dict[str, object]:
    try:
        safe_directory = _external_path(directory, field="observation directory")
        names = {path.name for path in safe_directory.iterdir()}
        if names != _OBSERVATION_FILES | {"manifest.json"}:
            raise ValueError
        authorization_bytes = (safe_directory / "authorization.json").read_bytes()
        authorization = _authorization_from_bytes(authorization_bytes)
        plan = authorization.plan
        expected_directory = _staging_directory(plan) if allow_staging else plan.output_directory
        if not _same_path(safe_directory, expected_directory):
            raise ValueError
        authorization_anchor, runner_anchor = _validate_plan(plan)
        _verify_outer_authorization(authorization, authorization_anchor)
        claim_bytes = (safe_directory / "claim-receipt.json").read_bytes()
        claim = RealClaimReceipt.from_bytes(claim_bytes)
        _verify_claim(
            receipt=claim,
            plan=plan,
            authorization_sha256=_sha(authorization_bytes),
            authorization_anchor=authorization_anchor,
        )
        manifest_bytes = (safe_directory / "manifest.json").read_bytes()
        manifest: Any = json.loads(manifest_bytes)
        if (
            not isinstance(manifest, dict)
            or manifest_bytes != _canonical(manifest)
            or set(manifest) != _MANIFEST_FIELDS
            or not isinstance(manifest["files"], dict)
            or set(manifest["files"]) != _OBSERVATION_FILES
            or any(not _is_sha(value) for value in manifest["files"].values())
            or any(
                _sha((safe_directory / name).read_bytes()) != digest
                for name, digest in manifest["files"].items()
            )
        ):
            raise ValueError
        unsigned = dict(manifest)
        signature = unsigned.pop("signature")
        if (
            type(signature) is not str
            or manifest["runner_key_id"] != runner_anchor.key_id
            or not runner_anchor.verify(
                domain=_ARTIFACT_DOMAIN, payload=unsigned, signature=signature
            )
        ):
            raise ValueError
        response_bytes = (safe_directory / "response.canonical.json").read_bytes()
        response = json.loads(response_bytes)
        parsed = parse_st_intervals(response)
        observation_bytes = (safe_directory / "observation.json").read_bytes()
        observation: Any = json.loads(observation_bytes)
        if (
            response_bytes != _canonical(response)
            or observation_bytes != _canonical(observation)
            or not isinstance(observation, dict)
            or set(observation) != _OBSERVATION_FIELDS
            or observation != _observation_payload(plan, parsed)
            or (safe_directory / "plan.json").read_bytes() != plan.to_bytes()
            or manifest
            != {
                **_manifest_unsigned(
                    plan=plan,
                    authorization_bytes=authorization_bytes,
                    claim=claim_bytes,
                    response_bytes=response_bytes,
                    observation_bytes=observation_bytes,
                    files=cast(dict[str, str], manifest["files"]),
                    runner_key_id=manifest["runner_key_id"],
                ),
                "signature": signature,
            }
        ):
            raise ValueError
        return cast(dict[str, object], manifest)
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        C1BOperationError,
        C1C0OperationError,
        RealAuthorizationError,
    ) as error:
        raise C1C0OperationError(
            "C1C0_OBSERVATION_INVALID", "provider observation verification failed"
        ) from error


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
    try:
        staging.mkdir(parents=True, exist_ok=False)
        response_bytes = _canonical(response)
        observation_bytes = _canonical(_observation_payload(plan, parsed))
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
        unsigned = _manifest_unsigned(
            plan=plan,
            authorization_bytes=authorization_bytes,
            claim=claim,
            response_bytes=response_bytes,
            observation_bytes=observation_bytes,
            files=inventory,
            runner_key_id=runner.key_id,
        )
        _write_exclusive(
            _direct_child(staging, "manifest.json"),
            _canonical(
                {**unsigned, "signature": runner.sign(domain=_ARTIFACT_DOMAIN, payload=unsigned)}
            ),
        )
        # Use the same deep verifier before release, while separately asserting
        # that staging has exactly the sole permitted incomplete location.
        _verify_observation_directory(directory=staging, allow_staging=True)
        os.replace(staging, final)
        try:
            verify_observation(directory=final)
        except C1C0OperationError:
            os.replace(final, staging)
            raise
        return final
    except (OSError, TypeError, ValueError) as error:
        raise C1C0OperationError(
            "C1C0_OUTPUT_PUBLISH_FAILED", "C1C0 observation could not be published"
        ) from error


def execute(*, authorization_file: Path, acknowledgement: str) -> Path:
    """Run only C1C0-R2: PRECHECK → CLAIM → IMPORT → CALL → OUTPUT."""
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
    authorization_anchor, _runner_anchor, issuer, runner = _precheck(authorization)
    claim = _claim(
        plan=authorization.plan,
        authorization_bytes=authorization_bytes,
        authorization_anchor=authorization_anchor,
        issuer=issuer,
    )
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
    """Independently verify a published final artifact from public anchors only."""
    return _verify_observation_directory(directory=directory, allow_staging=False)


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
