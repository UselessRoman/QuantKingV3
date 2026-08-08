"""S1-07C1B's only formal entry for the fixed XTQuant validation unit.

This module is deliberately an offline-ready orchestration layer around the
accepted C1A-R3 failure-only control chain.  It neither imports ``xtquant`` nor
implements a successful provider result.  Its purpose is to turn an explicitly
reviewed, canonical plan into one canonical authorization file, then to accept
only that file and an exact acknowledgement at execution time.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.persistence.assessment import (
    SqliteRealSourceAssessmentPersistenceAdapter,
)
from quantking.data.adapters.persistence.sqlite import create_data_engine
from quantking.data.adapters.sources.xtquant_metadata import ExplicitXtQuantPackageMetadataProbe
from quantking.data.models import DatasetKind
from quantking.data.validation.contracts_v3 import ValidationAttemptIdentityV3
from quantking.data.validation.models import ValidationVersions
from quantking.data.validation.real_artifacts import (
    RealValidationEvidenceVerifier,
    RealValidationEvidenceWriter,
)
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    RealAuthorizationClaimStore,
    RealAuthorizationError,
    RealClaimReceiptIssuer,
    RealExecutionAuthorizationAuthority,
    RealExecutionAuthorizationVerifier,
    RealRuntimeAuthorizationAuthority,
    RealRuntimeAuthorizationEnvelope,
    RealRuntimeAuthorizationFileLoader,
    RealRuntimeAuthorizationVerifier,
    RealVerifierTrustAnchor,
)
from quantking.data.validation.real_runtime import (
    ExplicitRealXtQuantTrustResolver,
    RealXtQuantExecutionError,
    RealXtQuantValidationPlan,
    RealXtQuantValidationRuntime,
)

C1B_PLAN_SCHEMA = "s1-07c1b-xtquant-trading-status-plan.v1"
C1B_AUTHORIZATION_SCHEMA = "s1-07c1b-xtquant-trading-status-authorization.v1"
C1B_PURPOSE = "S1-07C1B_XTQUANT_TRADING_STATUS_REAL_OPERATION"
C1B_EXECUTION_ACK = "I_ACKNOWLEDGE_C1B_AUTHORIZED_XTQUANT_TRADING_STATUS_EXECUTION"

_DOMAIN = "quantking.s1-07c1b.xtquant-trading-status-authorization.v1"
_SOURCE = "XTQUANT"
_DATASET = DatasetKind.TRADING_STATUS
_CASE_ID = "st_warning_603377_sh"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_KEY_ID = re.compile(r"^real-ed25519-sha256-[0-9a-f]{64}$")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha(payload: bytes) -> str:
    return sha256(payload).hexdigest()


class C1BOperationError(ValueError):
    """Sanitized error raised before C1B can perform an unsafe operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _repository_root() -> Path:
    """Find this source checkout's Git root without trusting the current directory."""
    try:
        source = Path(__file__).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise C1BOperationError(
            "C1B_REPOSITORY_BOUNDARY_UNAVAILABLE", "repository boundary cannot be resolved"
        ) from error
    for candidate in source.parents:
        marker = candidate / ".git"
        try:
            if marker.exists():
                if marker.is_symlink() or is_reparse_point(marker):
                    raise C1BOperationError(
                        "C1B_REPOSITORY_BOUNDARY_UNAVAILABLE",
                        "repository boundary is unsafe",
                    )
                return _absolute_path(str(candidate), field="repository root")
        except C1BOperationError:
            raise
        except OSError as error:
            raise C1BOperationError(
                "C1B_REPOSITORY_BOUNDARY_UNAVAILABLE", "repository boundary cannot be inspected"
            ) from error
    raise C1BOperationError(
        "C1B_REPOSITORY_BOUNDARY_UNAVAILABLE", "repository boundary is unavailable"
    )


def _is_same_or_descendant(candidate: Path, parent: Path) -> bool:
    """Compare resolved Windows paths case-insensitively without prefix confusion."""
    normalized_candidate = ntpath.normcase(ntpath.normpath(str(candidate)))
    normalized_parent = ntpath.normcase(ntpath.normpath(str(parent)))
    try:
        return ntpath.commonpath([normalized_candidate, normalized_parent]) == normalized_parent
    except ValueError:
        return False


def _absolute_path(
    value: object,
    *,
    field: str,
    require_file: bool = False,
    require_external_to_repository: bool = False,
) -> Path:
    if not isinstance(value, str) or not value:
        raise C1BOperationError("C1B_PARAMETER_INVALID", f"{field} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise C1BOperationError("C1B_PARAMETER_INVALID", f"{field} must be an absolute path")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if current.exists() and (current.is_symlink() or is_reparse_point(current)):
                raise C1BOperationError("C1B_PATH_UNSAFE", f"{field} contains a reparse point")
    except OSError as error:
        raise C1BOperationError("C1B_PATH_UNSAFE", f"{field} is unreadable") from error
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise C1BOperationError("C1B_PATH_UNSAFE", f"{field} cannot be resolved") from error
    if require_external_to_repository and _is_same_or_descendant(resolved, _repository_root()):
        raise C1BOperationError(
            "C1B_REPOSITORY_PATH_FORBIDDEN", f"{field} must be outside the repository"
        )
    if require_file and (not path.is_file() or path.is_symlink()):
        raise C1BOperationError(
            "C1B_PARAMETER_INVALID", f"{field} must name an existing regular file"
        )
    return resolved


def _external_canonical_output_path(path: Path, *, field: str) -> Path:
    safe = _absolute_path(str(path), field=field, require_external_to_repository=True)
    if not safe.parent.is_dir():
        raise C1BOperationError("C1B_PARAMETER_INVALID", f"{field} parent must already exist")
    if safe.exists():
        raise C1BOperationError("C1B_OUTPUT_EXISTS", f"{field} already exists")
    return safe


def preflight_plan_output(*, plan_file: Path) -> None:
    """Reject unsafe plan destinations before plan construction or signing material reads."""
    _external_canonical_output_path(plan_file, field="plan file")


def preflight_authorization_output(*, authorization_file: Path) -> None:
    """Reject unsafe authorization destinations before the authorization is signed."""
    _external_canonical_output_path(authorization_file, field="authorization file")


def _assert_private_keys_outside_repository(keys: C1BKeyConfiguration) -> None:
    _absolute_path(
        str(keys.authorization_private_key_file),
        field="authorization private key",
        require_external_to_repository=True,
    )
    _absolute_path(
        str(keys.runner_private_key_file),
        field="runner private key",
        require_external_to_repository=True,
    )


def _write_canonical_exclusive(path: Path, payload: bytes, *, field: str) -> None:
    safe = _external_canonical_output_path(path, field=field)
    try:
        descriptor = os.open(safe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise C1BOperationError("C1B_OUTPUT_EXISTS", f"{field} already exists") from error
    except OSError as error:
        raise C1BOperationError("C1B_OUTPUT_UNAVAILABLE", f"{field} cannot be created") from error
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    except OSError as error:
        raise C1BOperationError("C1B_OUTPUT_UNAVAILABLE", f"{field} cannot be written") from error
    finally:
        os.close(descriptor)


def _load_canonical(path: Path, *, field: str) -> bytes:
    safe = _absolute_path(
        str(path), field=field, require_file=True, require_external_to_repository=True
    )
    try:
        return safe.read_bytes()
    except OSError as error:
        raise C1BOperationError("C1B_INPUT_UNAVAILABLE", f"{field} cannot be read") from error


def _allowlist(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise C1BOperationError(
            "C1B_PARAMETER_INVALID", f"{field} must be a non-empty key allowlist"
        )
    values = tuple(value)
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        raise C1BOperationError("C1B_PARAMETER_INVALID", f"{field} must be sorted and unique")
    if any(_KEY_ID.fullmatch(item) is None for item in values):
        raise C1BOperationError("C1B_PARAMETER_INVALID", f"{field} contains an invalid key ID")
    return values


@dataclass(frozen=True, slots=True)
class C1BKeyConfiguration:
    """Explicit independent signing and verification key locations and trust."""

    authorization_private_key_file: Path
    authorization_public_key_file: Path
    authorization_allowed_key_ids: tuple[str, ...]
    runner_private_key_file: Path
    runner_public_key_file: Path
    runner_allowed_key_ids: tuple[str, ...]
    authorization_key_id: str
    runner_key_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "authorization_allowed_key_ids": list(self.authorization_allowed_key_ids),
            "authorization_key_id": self.authorization_key_id,
            "authorization_private_key_file": str(self.authorization_private_key_file),
            "authorization_public_key_file": str(self.authorization_public_key_file),
            "runner_allowed_key_ids": list(self.runner_allowed_key_ids),
            "runner_key_id": self.runner_key_id,
            "runner_private_key_file": str(self.runner_private_key_file),
            "runner_public_key_file": str(self.runner_public_key_file),
        }

    @classmethod
    def from_dict(cls, raw: object) -> C1BKeyConfiguration:
        if not isinstance(raw, dict) or set(raw) != {
            "authorization_allowed_key_ids",
            "authorization_key_id",
            "authorization_private_key_file",
            "authorization_public_key_file",
            "runner_allowed_key_ids",
            "runner_key_id",
            "runner_private_key_file",
            "runner_public_key_file",
        }:
            raise C1BOperationError("C1B_PLAN_INVALID", "C1B key configuration is invalid")
        authorization_ids = _allowlist(
            raw["authorization_allowed_key_ids"], field="authorization allowlist"
        )
        runner_ids = _allowlist(raw["runner_allowed_key_ids"], field="runner allowlist")
        authorization_key_id = raw["authorization_key_id"]
        runner_key_id = raw["runner_key_id"]
        if (
            not isinstance(authorization_key_id, str)
            or not isinstance(runner_key_id, str)
            or _KEY_ID.fullmatch(authorization_key_id) is None
            or _KEY_ID.fullmatch(runner_key_id) is None
        ):
            raise C1BOperationError("C1B_PLAN_INVALID", "C1B key identities are invalid")
        return cls(
            authorization_private_key_file=_absolute_path(
                raw["authorization_private_key_file"],
                field="authorization private key",
                require_external_to_repository=True,
            ),
            authorization_public_key_file=_absolute_path(
                raw["authorization_public_key_file"], field="authorization public key"
            ),
            authorization_allowed_key_ids=authorization_ids,
            runner_private_key_file=_absolute_path(
                raw["runner_private_key_file"],
                field="runner private key",
                require_external_to_repository=True,
            ),
            runner_public_key_file=_absolute_path(
                raw["runner_public_key_file"], field="runner public key"
            ),
            runner_allowed_key_ids=runner_ids,
            authorization_key_id=authorization_key_id,
            runner_key_id=runner_key_id,
        )


@dataclass(frozen=True, slots=True)
class C1BExecutionPlan:
    """Canonical reviewed plan for exactly one C1B authorization attempt."""

    identity: ValidationAttemptIdentityV3
    xtquant_site_packages_root: Path
    reference_bundle_root: Path
    reference_bundle_id: str
    reference_bundle_sha256: str
    output_root: Path
    claim_root: Path
    metadata_database: Path
    sqlite_busy_timeout_ms: int
    keys: C1BKeyConfiguration
    valid_from: UtcTimestamp
    expires_at: UtcTimestamp
    claim_nonce: str
    versions: ValidationVersions
    c1a_plan: Mapping[str, object]
    c1a_plan_sha256: str
    plan_sha256: str

    def __post_init__(self) -> None:
        if self.expires_at.value <= self.valid_from.value:
            raise C1BOperationError("C1B_PLAN_INVALID", "authorization validity window is invalid")
        if _HEX64.fullmatch(self.claim_nonce) is None:
            raise C1BOperationError("C1B_PLAN_INVALID", "claim nonce is invalid")
        if (
            _HEX64.fullmatch(self.c1a_plan_sha256) is None
            or _HEX64.fullmatch(self.plan_sha256) is None
        ):
            raise C1BOperationError("C1B_PLAN_INVALID", "plan SHA is invalid")
        if self.reference_bundle_id != "st-603377-authority-v3" or (
            _HEX64.fullmatch(self.reference_bundle_sha256) is None
        ):
            raise C1BOperationError("C1B_PLAN_INVALID", "reference bundle is invalid")
        if not 1 <= self.sqlite_busy_timeout_ms <= 60_000:
            raise C1BOperationError("C1B_PLAN_INVALID", "SQLite busy timeout is invalid")
        if self.claim_root == self.output_root or self.claim_root.is_relative_to(self.output_root):
            raise C1BOperationError("C1B_PLAN_INVALID", "claim root must be outside output root")
        if self.output_root.is_relative_to(self.claim_root):
            raise C1BOperationError("C1B_PLAN_INVALID", "output root must be outside claim root")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "case_id": _CASE_ID,
            "claim_nonce": self.claim_nonce,
            "claim_root": str(self.claim_root),
            "c1a_plan": dict(self.c1a_plan),
            "c1a_plan_sha256": self.c1a_plan_sha256,
            "dataset": _DATASET.value,
            "expires_at": self.expires_at.to_iso(),
            "identity": self.identity.to_dict(),
            "keys": self.keys.to_dict(),
            "metadata_database": str(self.metadata_database),
            "output_root": str(self.output_root),
            "purpose": C1B_PURPOSE,
            "reference_bundle_id": self.reference_bundle_id,
            "reference_bundle_root": str(self.reference_bundle_root),
            "reference_bundle_sha256": self.reference_bundle_sha256,
            "schema_version": C1B_PLAN_SCHEMA,
            "source": _SOURCE,
            "sqlite_busy_timeout_ms": self.sqlite_busy_timeout_ms,
            "valid_from": self.valid_from.to_iso(),
            "versions": self.versions.to_dict(),
            "xtquant_site_packages_root": str(self.xtquant_site_packages_root),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned_payload(), "plan_sha256": self.plan_sha256}

    def to_bytes(self) -> bytes:
        return _canonical(self.to_dict())

    def review(self) -> dict[str, object]:
        """Public review facts only; never emit paths to private keys or nonce/signatures."""
        return {
            "authorization_id": str(self.identity.authorization_id),
            "c1a_plan_sha256": self.c1a_plan_sha256,
            "case_id": _CASE_ID,
            "dataset": _DATASET.value,
            "expires_at": self.expires_at.to_iso(),
            "identity": self.identity.to_dict(),
            "output_root": str(self.output_root),
            "plan_sha256": self.plan_sha256,
            "reference_bundle_id": self.reference_bundle_id,
            "reference_bundle_sha256": self.reference_bundle_sha256,
            "source": _SOURCE,
            "valid_from": self.valid_from.to_iso(),
            "versions": self.versions.to_dict(),
        }

    @classmethod
    def from_bytes(cls, payload: bytes) -> C1BExecutionPlan:
        try:
            raw: Any = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise C1BOperationError("C1B_PLAN_INVALID", "C1B plan is invalid JSON") from error
        expected = {
            "case_id",
            "claim_nonce",
            "claim_root",
            "c1a_plan",
            "c1a_plan_sha256",
            "dataset",
            "expires_at",
            "identity",
            "keys",
            "metadata_database",
            "output_root",
            "plan_sha256",
            "purpose",
            "reference_bundle_id",
            "reference_bundle_root",
            "reference_bundle_sha256",
            "schema_version",
            "source",
            "sqlite_busy_timeout_ms",
            "valid_from",
            "versions",
            "xtquant_site_packages_root",
        }
        if not isinstance(raw, dict) or payload != _canonical(raw) or set(raw) != expected:
            raise C1BOperationError("C1B_PLAN_INVALID", "C1B plan is not canonical")
        unsigned = dict(raw)
        declared_sha = unsigned.pop("plan_sha256")
        if not isinstance(declared_sha, str) or _sha(_canonical(unsigned)) != declared_sha:
            raise C1BOperationError("C1B_PLAN_INVALID", "C1B plan SHA is invalid")
        if (
            raw["schema_version"] != C1B_PLAN_SCHEMA
            or raw["purpose"] != C1B_PURPOSE
            or raw["source"] != _SOURCE
            or raw["dataset"] != _DATASET.value
            or raw["case_id"] != _CASE_ID
            or not isinstance(raw["identity"], dict)
            or not isinstance(raw["versions"], dict)
            or not isinstance(raw["c1a_plan"], dict)
            or not isinstance(raw["sqlite_busy_timeout_ms"], int)
        ):
            raise C1BOperationError("C1B_PLAN_INVALID", "C1B plan scope is invalid")
        try:
            return cls(
                identity=ValidationAttemptIdentityV3.from_dict(raw["identity"]),
                xtquant_site_packages_root=_absolute_path(
                    raw["xtquant_site_packages_root"], field="XTQuant site-packages root"
                ),
                reference_bundle_root=_absolute_path(
                    raw["reference_bundle_root"], field="reference bundle root"
                ),
                reference_bundle_id=raw["reference_bundle_id"],
                reference_bundle_sha256=raw["reference_bundle_sha256"],
                output_root=_absolute_path(raw["output_root"], field="output root"),
                claim_root=_absolute_path(raw["claim_root"], field="claim root"),
                metadata_database=_absolute_path(
                    raw["metadata_database"], field="metadata database"
                ),
                sqlite_busy_timeout_ms=raw["sqlite_busy_timeout_ms"],
                keys=C1BKeyConfiguration.from_dict(raw["keys"]),
                valid_from=UtcTimestamp.from_iso(raw["valid_from"]),
                expires_at=UtcTimestamp.from_iso(raw["expires_at"]),
                claim_nonce=raw["claim_nonce"],
                versions=ValidationVersions(**raw["versions"]),
                c1a_plan=raw["c1a_plan"],
                c1a_plan_sha256=raw["c1a_plan_sha256"],
                plan_sha256=declared_sha,
            )
        except (C1BOperationError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, C1BOperationError):
                raise
            raise C1BOperationError("C1B_PLAN_INVALID", "C1B plan fields are invalid") from error


@dataclass(frozen=True, slots=True)
class C1BExecutionAuthorization:
    """C1B wrapper that binds the reviewed plan to the accepted C1A envelope."""

    plan: C1BExecutionPlan
    c1a_authorization: RealRuntimeAuthorizationEnvelope
    authorization_key_id: str
    signature: str

    def __post_init__(self) -> None:
        if (
            _KEY_ID.fullmatch(self.authorization_key_id) is None
            or _SIGNATURE.fullmatch(self.signature) is None
        ):
            raise C1BOperationError("C1B_AUTHORIZATION_INVALID", "C1B authorization is invalid")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "authorization_key_id": self.authorization_key_id,
            "c1a_authorization": json.loads(self.c1a_authorization.to_bytes()),
            "plan": self.plan.to_dict(),
            "purpose": C1B_PURPOSE,
            "schema_version": C1B_AUTHORIZATION_SCHEMA,
        }

    def to_bytes(self) -> bytes:
        return _canonical({**self.unsigned_payload(), "signature": self.signature})

    def review(self) -> dict[str, object]:
        return {**self.plan.review(), "authorization_schema": C1B_AUTHORIZATION_SCHEMA}

    @classmethod
    def from_bytes(cls, payload: bytes) -> C1BExecutionAuthorization:
        try:
            raw: Any = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise C1BOperationError(
                "C1B_AUTHORIZATION_INVALID", "C1B authorization is invalid JSON"
            ) from error
        expected = {
            "authorization_key_id",
            "c1a_authorization",
            "plan",
            "purpose",
            "schema_version",
            "signature",
        }
        if not isinstance(raw, dict) or payload != _canonical(raw) or set(raw) != expected:
            raise C1BOperationError(
                "C1B_AUTHORIZATION_INVALID", "C1B authorization is not canonical"
            )
        if (
            raw["schema_version"] != C1B_AUTHORIZATION_SCHEMA
            or raw["purpose"] != C1B_PURPOSE
            or not isinstance(raw["authorization_key_id"], str)
            or not isinstance(raw["plan"], dict)
            or not isinstance(raw["c1a_authorization"], dict)
            or not isinstance(raw["signature"], str)
        ):
            raise C1BOperationError(
                "C1B_AUTHORIZATION_INVALID", "C1B authorization scope is invalid"
            )
        try:
            return cls(
                plan=C1BExecutionPlan.from_bytes(_canonical(raw["plan"])),
                c1a_authorization=RealRuntimeAuthorizationEnvelope.from_bytes(
                    _canonical(raw["c1a_authorization"])
                ),
                authorization_key_id=raw["authorization_key_id"],
                signature=raw["signature"],
            )
        except (C1BOperationError, RealAuthorizationError, ValueError) as error:
            if isinstance(error, C1BOperationError):
                raise
            raise C1BOperationError(
                "C1B_AUTHORIZATION_INVALID", "C1B authorization fields are invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class C1BExecutionResult:
    directory: Path
    event_order: tuple[str, ...]
    assessment_status: str
    plan_sha256: str

    def review(self) -> dict[str, object]:
        return {
            "assessment_status": self.assessment_status,
            "directory": str(self.directory),
            "event_order": list(self.event_order),
            "plan_sha256": self.plan_sha256,
        }


def _key_configuration(
    *,
    authorization_private_key_file: Path,
    authorization_public_key_file: Path,
    authorization_allowed_key_ids: tuple[str, ...],
    runner_private_key_file: Path,
    runner_public_key_file: Path,
    runner_allowed_key_ids: tuple[str, ...],
) -> C1BKeyConfiguration:
    authorization_private = _absolute_path(
        str(authorization_private_key_file),
        field="authorization private key",
        require_file=True,
        require_external_to_repository=True,
    )
    authorization_public = _absolute_path(
        str(authorization_public_key_file), field="authorization public key", require_file=True
    )
    runner_private = _absolute_path(
        str(runner_private_key_file),
        field="runner private key",
        require_file=True,
        require_external_to_repository=True,
    )
    runner_public = _absolute_path(
        str(runner_public_key_file), field="runner public key", require_file=True
    )
    authorization_ids = _allowlist(
        list(authorization_allowed_key_ids), field="authorization allowlist"
    )
    runner_ids = _allowlist(list(runner_allowed_key_ids), field="runner allowlist")
    try:
        authorization_signer = ExplicitLocalRealKey(private_key_file=authorization_private)
        authorization_anchor = RealVerifierTrustAnchor(
            public_key_file=authorization_public, allowed_key_ids=frozenset(authorization_ids)
        )
        runner_signer = ExplicitLocalRealKey(private_key_file=runner_private)
        runner_anchor = RealVerifierTrustAnchor(
            public_key_file=runner_public, allowed_key_ids=frozenset(runner_ids)
        )
    except RealAuthorizationError as error:
        raise C1BOperationError("C1B_KEY_INVALID", "C1B key configuration is invalid") from error
    if (
        authorization_signer.key_id != authorization_anchor.key_id
        or runner_signer.key_id != runner_anchor.key_id
    ):
        raise C1BOperationError("C1B_KEY_INVALID", "C1B key pairs do not match")
    return C1BKeyConfiguration(
        authorization_private_key_file=authorization_private,
        authorization_public_key_file=authorization_public,
        authorization_allowed_key_ids=authorization_ids,
        runner_private_key_file=runner_private,
        runner_public_key_file=runner_public,
        runner_allowed_key_ids=runner_ids,
        authorization_key_id=authorization_anchor.key_id,
        runner_key_id=runner_anchor.key_id,
    )


def _runtime_for_plan(plan: C1BExecutionPlan) -> RealXtQuantValidationRuntime:
    keys = _key_configuration(
        authorization_private_key_file=plan.keys.authorization_private_key_file,
        authorization_public_key_file=plan.keys.authorization_public_key_file,
        authorization_allowed_key_ids=plan.keys.authorization_allowed_key_ids,
        runner_private_key_file=plan.keys.runner_private_key_file,
        runner_public_key_file=plan.keys.runner_public_key_file,
        runner_allowed_key_ids=plan.keys.runner_allowed_key_ids,
    )
    if (
        keys.authorization_key_id != plan.keys.authorization_key_id
        or keys.runner_key_id != plan.keys.runner_key_id
    ):
        raise C1BOperationError("C1B_KEY_SUBSTITUTION", "C1B key identity changed")
    try:
        authorization_anchor = RealVerifierTrustAnchor(
            public_key_file=keys.authorization_public_key_file,
            allowed_key_ids=frozenset(keys.authorization_allowed_key_ids),
        )
        return RealXtQuantValidationRuntime(
            authorization_file_loader=RealRuntimeAuthorizationFileLoader(
                verifier=RealRuntimeAuthorizationVerifier(trust_anchor=authorization_anchor)
            ),
            authorization_verifier=RealExecutionAuthorizationVerifier(
                trust_anchor=authorization_anchor
            ),
            claim_store=RealAuthorizationClaimStore(
                claim_root=plan.claim_root,
                receipt_issuer=RealClaimReceiptIssuer(
                    private_key_file=keys.authorization_private_key_file
                ),
            ),
            trust_resolver=ExplicitRealXtQuantTrustResolver(
                reference_bundle_root=plan.reference_bundle_root,
                runner_public_key_file=keys.runner_public_key_file,
                allowed_runner_key_ids=frozenset(keys.runner_allowed_key_ids),
                authorization_public_key_file=keys.authorization_public_key_file,
                allowed_authorization_key_ids=frozenset(keys.authorization_allowed_key_ids),
            ),
            runner_signer=ExplicitLocalRealKey(private_key_file=keys.runner_private_key_file),
            package_metadata_probe=ExplicitXtQuantPackageMetadataProbe(
                site_packages_root=plan.xtquant_site_packages_root
            ),
        )
    except (RealAuthorizationError, ValueError) as error:
        raise C1BOperationError(
            "C1B_RUNTIME_NOT_READY", "C1B trust configuration is invalid"
        ) from error


def _assert_plan_bindings(
    plan: C1BExecutionPlan, *, reject_consumed: bool
) -> RealXtQuantValidationPlan:
    if _sha(_canonical(plan.unsigned_payload())) != plan.plan_sha256:
        raise C1BOperationError("C1B_PLAN_BINDING_MISMATCH", "C1B plan SHA no longer matches")
    if not plan.metadata_database.is_file() or plan.metadata_database.is_symlink():
        raise C1BOperationError(
            "C1B_METADATA_DATABASE_UNAVAILABLE",
            "metadata database must be an existing regular file",
        )
    runtime = _runtime_for_plan(plan)
    try:
        c1a_plan = runtime.plan_for_authorization(
            output_root=plan.output_root, identity=plan.identity
        )
    except RealXtQuantExecutionError as error:
        raise C1BOperationError(error.code, "C1B precheck rejected the fixed C1A plan") from None
    except (RealAuthorizationError, ValueError) as error:
        raise C1BOperationError(
            "C1B_PLAN_BINDING_MISMATCH", "C1B plan bindings are invalid"
        ) from error
    if (
        plan.reference_bundle_id != c1a_plan.context.promotion_profile.reference_bundle_id
        or plan.reference_bundle_sha256
        != c1a_plan.context.promotion_profile.reference_bundle_manifest_sha256
        or plan.versions.to_dict() != c1a_plan.versions.to_dict()
        or plan.c1a_plan_sha256 != c1a_plan.plan_sha256
        or _canonical(dict(plan.c1a_plan)) != c1a_plan.plan_bytes
    ):
        raise C1BOperationError(
            "C1B_PLAN_BINDING_MISMATCH", "C1B plan does not match the fixed C1A plan"
        )
    if reject_consumed:
        claim = plan.claim_root / f"{plan.identity.authorization_id.value.hex}.json"
        try:
            if claim.exists():
                raise C1BOperationError(
                    "AUTHORIZATION_ALREADY_CONSUMED", "authorization ID is already claimed"
                )
        except OSError as error:
            raise C1BOperationError(
                "AUTHORIZATION_CLAIM_STORE_UNSAFE", "claim root is unavailable"
            ) from error
    return c1a_plan


def prepare_plan(
    *,
    source: str,
    dataset: DatasetKind,
    case_id: str,
    xtquant_site_packages_root: Path,
    reference_bundle_root: Path,
    reference_bundle_id: str,
    reference_bundle_sha256: str,
    output_root: Path,
    claim_root: Path,
    metadata_database: Path,
    sqlite_busy_timeout_ms: int,
    assessment_id: str,
    session_id: str,
    invocation_id: str,
    run_id: str,
    authorization_id: str,
    authorization_private_key_file: Path,
    authorization_public_key_file: Path,
    authorization_allowed_key_ids: tuple[str, ...],
    runner_private_key_file: Path,
    runner_public_key_file: Path,
    runner_allowed_key_ids: tuple[str, ...],
    valid_from: str,
    expires_at: str,
    claim_nonce: str,
    adapter_version: str,
    package_version: str,
    code_version: str,
    schema_version: str,
    rule_version: str,
    threshold_version: str,
    sample_spec_version: str,
    config_version: str,
) -> C1BExecutionPlan:
    """Build, but never issue or execute, one reviewed C1B plan."""
    if source != _SOURCE or dataset is not _DATASET or case_id != _CASE_ID:
        raise C1BOperationError(
            "C1B_FIXED_SCOPE_REQUIRED", "C1B accepts only its fixed source unit"
        )
    try:
        identity = ValidationAttemptIdentityV3.from_dict(
            {
                "assessment_id": assessment_id,
                "authorization_id": authorization_id,
                "invocation_id": invocation_id,
                "run_id": run_id,
                "session_id": session_id,
            }
        )
        versions = ValidationVersions(
            adapter_version=adapter_version,
            package_version=package_version,
            code_version=code_version,
            schema_version=schema_version,
            rule_version=rule_version,
            threshold_version=threshold_version,
            sample_spec_version=sample_spec_version,
            config_version=config_version,
        )
        parsed_valid_from = UtcTimestamp.from_iso(valid_from)
        parsed_expires_at = UtcTimestamp.from_iso(expires_at)
    except (TypeError, ValueError) as error:
        raise C1BOperationError(
            "C1B_PARAMETER_INVALID", "C1B IDs, time, or versions are invalid"
        ) from error
    keys = _key_configuration(
        authorization_private_key_file=authorization_private_key_file,
        authorization_public_key_file=authorization_public_key_file,
        authorization_allowed_key_ids=authorization_allowed_key_ids,
        runner_private_key_file=runner_private_key_file,
        runner_public_key_file=runner_public_key_file,
        runner_allowed_key_ids=runner_allowed_key_ids,
    )
    provisional = C1BExecutionPlan(
        identity=identity,
        xtquant_site_packages_root=_absolute_path(
            str(xtquant_site_packages_root), field="XTQuant site-packages root"
        ),
        reference_bundle_root=_absolute_path(
            str(reference_bundle_root), field="reference bundle root"
        ),
        reference_bundle_id=reference_bundle_id,
        reference_bundle_sha256=reference_bundle_sha256,
        output_root=_absolute_path(str(output_root), field="output root"),
        claim_root=_absolute_path(str(claim_root), field="claim root"),
        metadata_database=_absolute_path(str(metadata_database), field="metadata database"),
        sqlite_busy_timeout_ms=sqlite_busy_timeout_ms,
        keys=keys,
        valid_from=parsed_valid_from,
        expires_at=parsed_expires_at,
        claim_nonce=claim_nonce,
        versions=versions,
        c1a_plan={},
        c1a_plan_sha256="0" * 64,
        plan_sha256="0" * 64,
    )
    if not provisional.metadata_database.is_file():
        raise C1BOperationError(
            "C1B_METADATA_DATABASE_UNAVAILABLE",
            "metadata database must be an existing regular file",
        )
    runtime = _runtime_for_plan(provisional)
    try:
        c1a_plan = runtime.plan_for_authorization(
            output_root=provisional.output_root, identity=provisional.identity
        )
    except RealXtQuantExecutionError as error:
        raise C1BOperationError(error.code, "C1B plan precheck failed") from None
    if (
        reference_bundle_id != c1a_plan.context.promotion_profile.reference_bundle_id
        or reference_bundle_sha256
        != c1a_plan.context.promotion_profile.reference_bundle_manifest_sha256
        or versions.to_dict() != c1a_plan.versions.to_dict()
    ):
        raise C1BOperationError(
            "C1B_PLAN_BINDING_MISMATCH", "explicit C1B bindings do not match C1A"
        )
    c1a_raw = json.loads(c1a_plan.plan_bytes)
    unsigned = C1BExecutionPlan(
        **{
            field: getattr(provisional, field)
            for field in provisional.__dataclass_fields__
            if field not in {"c1a_plan", "c1a_plan_sha256", "plan_sha256"}
        },
        c1a_plan=c1a_raw,
        c1a_plan_sha256=c1a_plan.plan_sha256,
        plan_sha256="0" * 64,
    )
    digest = _sha(_canonical(unsigned.unsigned_payload()))
    return C1BExecutionPlan(
        **{
            field: getattr(unsigned, field)
            for field in unsigned.__dataclass_fields__
            if field != "plan_sha256"
        },
        plan_sha256=digest,
    )


def write_plan(*, plan: C1BExecutionPlan, plan_file: Path) -> None:
    preflight_plan_output(plan_file=plan_file)
    _assert_private_keys_outside_repository(plan.keys)
    _write_canonical_exclusive(plan_file, plan.to_bytes(), field="plan file")


def load_plan(*, plan_file: Path) -> C1BExecutionPlan:
    return C1BExecutionPlan.from_bytes(_load_canonical(plan_file, field="plan file"))


def issue_authorization(*, plan: C1BExecutionPlan) -> C1BExecutionAuthorization:
    """Issue only after rebuilding every plan binding; no claim or runner import occurs."""
    c1a_plan = _assert_plan_bindings(plan, reject_consumed=False)
    keys = plan.keys
    try:
        execution = RealExecutionAuthorizationAuthority(
            private_key_file=keys.authorization_private_key_file
        ).issue(
            authorization_id=plan.identity.authorization_id,
            source=_SOURCE,
            dataset=_DATASET,
            output_root=plan.output_root,
            sample_sha256=c1a_plan.context.sample.sample_spec_sha256,
            valid_from=plan.valid_from,
            expires_at=plan.expires_at,
        )
        c1a_authorization = RealRuntimeAuthorizationAuthority(
            private_key_file=keys.authorization_private_key_file
        ).issue(
            authorization=execution,
            identity=plan.identity,
            selected_case_ids=c1a_plan.selected_case_ids,
            reference_bundle_id=plan.reference_bundle_id,
            reference_bundle_sha256=plan.reference_bundle_sha256,
            source_dataset_contract=c1a_plan.context.source_dataset_contract,
            promotion_profile=c1a_plan.context.promotion_profile,
            output_scope=str(plan.output_root),
            frozen_versions=plan.versions.to_dict(),
            plan_sha256=plan.c1a_plan_sha256,
            claim_nonce=plan.claim_nonce,
            runner_key_id=keys.runner_key_id,
        )
        signer = ExplicitLocalRealKey(private_key_file=keys.authorization_private_key_file)
    except RealAuthorizationError as error:
        raise C1BOperationError(
            "C1B_AUTHORIZATION_ISSUE_FAILED", "C1B authorization cannot be issued"
        ) from error
    unsigned = C1BExecutionAuthorization(
        plan=plan,
        c1a_authorization=c1a_authorization,
        authorization_key_id=signer.key_id,
        signature="0" * 128,
    )
    return C1BExecutionAuthorization(
        plan=plan,
        c1a_authorization=c1a_authorization,
        authorization_key_id=signer.key_id,
        signature=signer.sign(domain=_DOMAIN, payload=unsigned.unsigned_payload()),
    )


def write_authorization(
    *, authorization: C1BExecutionAuthorization, authorization_file: Path
) -> None:
    preflight_authorization_output(authorization_file=authorization_file)
    _assert_private_keys_outside_repository(authorization.plan.keys)
    _write_canonical_exclusive(
        authorization_file, authorization.to_bytes(), field="authorization file"
    )


def _verify_c1b_authorization(
    authorization: C1BExecutionAuthorization,
) -> RealXtQuantValidationPlan:
    plan = authorization.plan
    now = utc_now()
    if not plan.valid_from.value <= now.value < plan.expires_at.value:
        raise C1BOperationError(
            "AUTHORIZATION_INVALID_OR_EXPIRED", "authorization is outside its validity window"
        )
    try:
        anchor = RealVerifierTrustAnchor(
            public_key_file=plan.keys.authorization_public_key_file,
            allowed_key_ids=frozenset(plan.keys.authorization_allowed_key_ids),
        )
    except RealAuthorizationError as error:
        raise C1BOperationError(
            "C1B_KEY_SUBSTITUTION", "authorization public key is invalid"
        ) from error
    if (
        authorization.authorization_key_id != plan.keys.authorization_key_id
        or authorization.authorization_key_id != anchor.key_id
        or not anchor.verify(
            domain=_DOMAIN,
            payload=authorization.unsigned_payload(),
            signature=authorization.signature,
        )
    ):
        raise C1BOperationError(
            "C1B_AUTHORIZATION_UNTRUSTED", "C1B authorization signature is not trusted"
        )
    if not RealRuntimeAuthorizationVerifier(trust_anchor=anchor).verify_real_file_authorization(
        authorization.c1a_authorization
    ):
        raise C1BOperationError(
            "C1B_AUTHORIZATION_UNTRUSTED", "embedded C1A authorization is not trusted"
        )
    c1a_plan = _assert_plan_bindings(plan, reject_consumed=True)
    envelope = authorization.c1a_authorization
    if (
        envelope.identity != plan.identity
        or envelope.authorization.authorization_id != plan.identity.authorization_id
        or envelope.authorization.source != _SOURCE
        or envelope.authorization.dataset is not _DATASET
        or envelope.authorization.output_root != str(plan.output_root)
        or envelope.authorization.valid_from != plan.valid_from
        or envelope.authorization.expires_at != plan.expires_at
        or envelope.selected_case_ids != (_CASE_ID,)
        or envelope.reference_bundle_id != plan.reference_bundle_id
        or envelope.reference_bundle_sha256 != plan.reference_bundle_sha256
        or envelope.source_dataset_contract != c1a_plan.context.source_dataset_contract
        or envelope.promotion_profile != c1a_plan.context.promotion_profile
        or envelope.output_scope != str(plan.output_root)
        or dict(envelope.frozen_versions) != plan.versions.to_dict()
        or envelope.plan_sha256 != plan.c1a_plan_sha256
        or envelope.claim_nonce != plan.claim_nonce
        or envelope.runner_key_id != plan.keys.runner_key_id
        or envelope.authorization_key_id != plan.keys.authorization_key_id
    ):
        raise C1BOperationError(
            "C1B_AUTHORIZATION_BINDING_MISMATCH", "authorization does not match the reviewed plan"
        )
    return c1a_plan


def load_authorization(*, authorization_file: Path) -> C1BExecutionAuthorization:
    authorization = C1BExecutionAuthorization.from_bytes(
        _load_canonical(authorization_file, field="authorization file")
    )
    _verify_c1b_authorization(authorization)
    return authorization


def execute(*, authorization_file: Path, acknowledgement: str) -> C1BExecutionResult:
    """Run C1B only from one trusted file and one exact acknowledgement string."""
    if acknowledgement != C1B_EXECUTION_ACK:
        raise C1BOperationError(
            "C1B_ACKNOWLEDGEMENT_REQUIRED", "exact C1B acknowledgement is required"
        )
    authorization = load_authorization(authorization_file=authorization_file)
    plan = authorization.plan
    runtime = _runtime_for_plan(plan)
    # C1A intentionally accepts only a canonical file.  This short-lived file
    # is created outside the authorized output and claim roots and is removed
    # before returning; it is never logged or included in Git artifacts.
    with tempfile.TemporaryDirectory(prefix="quantking-c1b-") as temporary:
        inner_file = Path(temporary) / "c1a-runtime-authorization.json"
        inner_file.write_bytes(authorization.c1a_authorization.to_bytes())
        try:
            run = runtime.execute(authorization_file=inner_file)
        except RealXtQuantExecutionError as error:
            raise C1BOperationError(error.code, "C1B execution stopped safely") from None
    expected_prefix = ("PRECHECK", "CLAIM", "IMPORT", "OUTPUT")
    if run.event_order != expected_prefix:
        raise C1BOperationError("C1B_RUNTIME_ORDER_INVALID", "C1A execution order is invalid")
    try:
        trust = runtime._trust_resolver.resolve()
        verifier = RealValidationEvidenceVerifier(
            validation_root=plan.output_root,
            context=trust.context,
            runner_verifier_anchor=trust.runner_verifier_anchor,
            authorization_verifier_anchor=trust.authorization_verifier_anchor,
        )
        verified = verifier.verify_run(directory=run.directory)
    except Exception:
        raise C1BOperationError("C1B_VERIFY_FAILED", "C1B output verification failed") from None
    if verified.identity != plan.identity:
        raise C1BOperationError("C1B_VERIFY_FAILED", "C1B output identity is invalid")
    try:
        RealValidationEvidenceWriter(validation_root=plan.output_root).finalize_assessment(
            assessment_directory=run.directory.parent, verifier=verifier
        )
    except Exception:
        raise C1BOperationError("C1B_SEAL_FAILED", "C1B assessment finalization failed") from None
    engine = create_data_engine(plan.metadata_database, busy_timeout_ms=plan.sqlite_busy_timeout_ms)
    try:
        saved = SqliteRealSourceAssessmentPersistenceAdapter(
            engine=engine,
            validation_root=plan.output_root,
            context=trust.context,
            runner_verifier_anchor=trust.runner_verifier_anchor,
            authorization_verifier_anchor=trust.authorization_verifier_anchor,
        ).persist_from_directory(run.directory.parent)
    except Exception:
        raise C1BOperationError("C1B_PERSIST_FAILED", "C1B persistence failed safely") from None
    finally:
        engine.dispose()
    return C1BExecutionResult(
        directory=run.directory,
        event_order=("PRECHECK", "CLAIM", "IMPORT", "OUTPUT", "VERIFY", "SEAL", "PERSIST"),
        assessment_status=saved.status.value,
        plan_sha256=plan.plan_sha256,
    )


__all__ = [
    "C1B_AUTHORIZATION_SCHEMA",
    "C1B_EXECUTION_ACK",
    "C1B_PLAN_SCHEMA",
    "C1B_PURPOSE",
    "C1BExecutionAuthorization",
    "C1BExecutionPlan",
    "C1BExecutionResult",
    "C1BOperationError",
    "execute",
    "issue_authorization",
    "load_authorization",
    "load_plan",
    "prepare_plan",
    "write_authorization",
    "write_plan",
]
