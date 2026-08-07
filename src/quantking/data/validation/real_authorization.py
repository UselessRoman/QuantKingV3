"""C1A-R1 REAL-only authorization, Ed25519 trust, and claim lineage.

This module is intentionally separate from C0B's HMAC/Fake authorization
contracts.  REAL keys are asymmetric: writers load an explicit private PEM,
while verifiers load an explicit public PEM and compare its derived fingerprint
with a separately configured allowlist.  No caller supplied key id can make a
key REAL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.assessment.models import EvidenceKind
from quantking.data.models import DatasetKind
from quantking.data.validation.contracts_v3 import (
    FAKE_V3_RUNNER_KEY_ID,
    PromotionProfileV3,
    ResultAttestationV3,
    SourceDatasetContractV3,
    V3ResultAttestationSigner,
    V3RunnerTrustPolicy,
    ValidationAttemptIdentityV3,
    dataclass_replace,
)

REAL_RUNTIME_AUTHORIZATION_SCHEMA = "runtime-authorization-envelope.real.v2"
REAL_RUNTIME_AUTHORIZATION_PURPOSE = "S1-07C1A_R1_XTQUANT_TRADING_STATUS_REAL"
REAL_EXECUTION_AUTHORIZATION_SCHEMA = "real-execution-authorization.v1"
REAL_CLAIM_RECEIPT_SCHEMA = "real-authorization-claim-receipt.v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^real-ed25519-sha256-[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_VERSION_FIELDS = frozenset(
    {
        "adapter_version",
        "package_version",
        "code_version",
        "schema_version",
        "rule_version",
        "threshold_version",
        "sample_spec_version",
        "config_version",
    }
)
_KNOWN_FAKE_MATERIAL = frozenset(
    {
        b"quantking-s1-07a-r2-fake-authority-key",
        b"quantking-s1-07a-r2-fake-runner-key",
    }
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "real-ed25519-sha256-" + _sha(raw)


def _message(domain: str, payload: Mapping[str, object]) -> bytes:
    return _canonical({"domain": domain, "payload": dict(payload)})


class RealAuthorizationError(ValueError):
    """A REAL authorization key, signature, or canonical file is invalid."""


class ExplicitLocalRealKey:
    """An explicit private Ed25519 signer; it never supplies read trust."""

    def __init__(self, *, private_key_file: Path) -> None:
        if not isinstance(private_key_file, Path) or not private_key_file.is_absolute():
            raise RealAuthorizationError("REAL private key file must be an explicit absolute Path")
        if not _path_has_no_reparse_points(private_key_file):
            raise RealAuthorizationError("REAL private key file is unsafe")
        try:
            payload = private_key_file.read_bytes()
        except OSError as error:
            raise RealAuthorizationError("REAL private key file is unavailable") from error
        if payload in _KNOWN_FAKE_MATERIAL:
            raise RealAuthorizationError("Fake key material cannot be REAL")
        try:
            private = serialization.load_pem_private_key(payload, password=None)
        except (TypeError, ValueError) as error:
            raise RealAuthorizationError("REAL private key file is invalid") from error
        if not isinstance(private, Ed25519PrivateKey):
            raise RealAuthorizationError("REAL private key must be Ed25519")
        self._private = private
        self._key_id = _fingerprint(private.public_key())

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, *, domain: str, payload: Mapping[str, object]) -> str:
        return self._private.sign(_message(domain, payload)).hex()


class RealVerifierTrustAnchor:
    """Read-side public-key anchor with a verifier-owned static allowlist."""

    def __init__(self, *, public_key_file: Path, allowed_key_ids: frozenset[str]) -> None:
        if not isinstance(public_key_file, Path) or not public_key_file.is_absolute():
            raise RealAuthorizationError("REAL public key file must be an explicit absolute Path")
        if not _path_has_no_reparse_points(public_key_file):
            raise RealAuthorizationError("REAL public key file is unsafe")
        if not allowed_key_ids or any(
            not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
            or key_id == FAKE_V3_RUNNER_KEY_ID
            or key_id.startswith("quantking.fake-")
            for key_id in allowed_key_ids
        ):
            raise RealAuthorizationError("REAL verifier allowlist is invalid")
        try:
            payload = public_key_file.read_bytes()
        except OSError as error:
            raise RealAuthorizationError("REAL public key file is unavailable") from error
        if payload in _KNOWN_FAKE_MATERIAL:
            raise RealAuthorizationError("Fake key material cannot be REAL")
        try:
            public = serialization.load_pem_public_key(payload)
        except (TypeError, ValueError) as error:
            raise RealAuthorizationError("REAL public key file is invalid") from error
        if not isinstance(public, Ed25519PublicKey):
            raise RealAuthorizationError("REAL public key must be Ed25519")
        self._public = public
        self._key_id = _fingerprint(public)
        self._allowlist = frozenset(allowed_key_ids)
        if self._key_id not in self._allowlist:
            raise RealAuthorizationError("REAL verifier public key is not allowlisted")

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def allowed_key_ids(self) -> frozenset[str]:
        return self._allowlist

    def verify(self, *, domain: str, payload: Mapping[str, object], signature: str) -> bool:
        if self._key_id not in self._allowlist or not isinstance(signature, str):
            return False
        try:
            encoded = bytes.fromhex(signature)
            self._public.verify(encoded, _message(domain, payload))
        except (InvalidSignature, ValueError):
            return False
        return True


class LocalRealV3Signer(V3ResultAttestationSigner):
    """Compatibility signer for the legacy v3 protocol; no read anchor role."""

    _DOMAIN = "quantking.s1-07c1a-r1.result-attestation.v3"

    def __init__(self, *, private_key_file: Path) -> None:
        self._key = ExplicitLocalRealKey(private_key_file=private_key_file)

    @property
    def key_id(self) -> str:
        return self._key.key_id

    def sign(self, attestation: ResultAttestationV3) -> ResultAttestationV3:
        if attestation.signer_key_id != self.key_id:
            raise RealAuthorizationError("REAL result signer key ID does not match")
        return dataclass_replace(
            attestation,
            signature=self._key.sign(domain=self._DOMAIN, payload=attestation.unsigned_payload()),
        )

    def verify(self, attestation: ResultAttestationV3) -> bool:
        # This method exists solely for the legacy protocol interface.  Formal
        # R1 read paths use RealVerifierTrustAnchor instead of a private signer.
        return False


class RealV3RunnerTrustPolicy(V3RunnerTrustPolicy):
    """Legacy v3 policy with an anchor-owned key fingerprint allowlist."""

    def __init__(self, *, verifier_anchor: RealVerifierTrustAnchor) -> None:
        self._anchor = verifier_anchor

    def evidence_kind_for(self, *, attestation_key_id: str, verifier_key_id: str) -> EvidenceKind:
        if (
            attestation_key_id != self._anchor.key_id
            or verifier_key_id != self._anchor.key_id
            or attestation_key_id not in self._anchor.allowed_key_ids
        ):
            raise RealAuthorizationError("REAL runner key is not verifier-allowlisted")
        return EvidenceKind.REAL


@dataclass(frozen=True, slots=True)
class RealExecutionAuthorization:
    """Asymmetric, REAL-only execution authorization kept out of C0B's DTO."""

    authorization_id: EntityId
    source: str
    dataset: DatasetKind
    output_root: str
    sample_sha256: str
    valid_from: UtcTimestamp
    expires_at: UtcTimestamp
    signer_key_id: str
    signature: str

    def __post_init__(self) -> None:
        if self.authorization_id.namespace != "data_authorization":
            raise RealAuthorizationError("REAL authorization ID is invalid")
        if not _SOURCE.fullmatch(self.source) or not Path(self.output_root).is_absolute():
            raise RealAuthorizationError("REAL authorization scope is invalid")
        if not _HEX64.fullmatch(self.sample_sha256) or not _KEY_ID.fullmatch(self.signer_key_id):
            raise RealAuthorizationError("REAL authorization digest or signer is invalid")
        if not isinstance(self.signature, str) or not re.fullmatch(
            r"[0-9a-f]{128}", self.signature
        ):
            raise RealAuthorizationError("REAL authorization signature is invalid")
        if self.expires_at.value <= self.valid_from.value:
            raise RealAuthorizationError("REAL authorization expiry is invalid")

    def unsigned_payload(self) -> dict[str, str]:
        return {
            "authorization_id": str(self.authorization_id),
            "dataset": self.dataset.value,
            "expires_at": self.expires_at.to_iso(),
            "output_root": self.output_root,
            "sample_sha256": self.sample_sha256,
            "signer_key_id": self.signer_key_id,
            "source": self.source,
            "valid_from": self.valid_from.to_iso(),
        }

    def to_dict(self) -> dict[str, str]:
        return {**self.unsigned_payload(), "signature": self.signature}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> RealExecutionAuthorization:
        if set(raw) != {
            "authorization_id",
            "dataset",
            "expires_at",
            "output_root",
            "sample_sha256",
            "signer_key_id",
            "signature",
            "source",
            "valid_from",
        } or any(type(value) is not str for value in raw.values()):
            raise RealAuthorizationError("REAL execution authorization fields are invalid")
        try:
            return cls(
                authorization_id=parse_entity_id(str(raw["authorization_id"])),
                source=str(raw["source"]),
                dataset=DatasetKind(str(raw["dataset"])),
                output_root=str(raw["output_root"]),
                sample_sha256=str(raw["sample_sha256"]),
                valid_from=UtcTimestamp.from_iso(str(raw["valid_from"])),
                expires_at=UtcTimestamp.from_iso(str(raw["expires_at"])),
                signer_key_id=str(raw["signer_key_id"]),
                signature=str(raw["signature"]),
            )
        except (TypeError, ValueError) as error:
            raise RealAuthorizationError("REAL execution authorization is invalid") from error


class RealExecutionAuthorizationAuthority:
    """Private-key issuer.  Verification is intentionally a separate anchor."""

    _DOMAIN = "quantking.s1-07c1a-r1.execution-authorization.v1"

    def __init__(self, *, private_key_file: Path) -> None:
        self._key = ExplicitLocalRealKey(private_key_file=private_key_file)

    @property
    def key_id(self) -> str:
        return self._key.key_id

    def issue(
        self,
        *,
        authorization_id: EntityId,
        source: str,
        dataset: DatasetKind,
        output_root: Path,
        sample_sha256: str,
        valid_from: UtcTimestamp | None = None,
        expires_at: UtcTimestamp,
    ) -> RealExecutionAuthorization:
        unsigned = RealExecutionAuthorization(
            authorization_id=authorization_id,
            source=source,
            dataset=dataset,
            output_root=str(output_root.resolve(strict=False)),
            sample_sha256=sample_sha256,
            valid_from=valid_from or utc_now(),
            expires_at=expires_at,
            signer_key_id=self.key_id,
            signature="0" * 128,
        )
        return replace(
            unsigned,
            signature=self._key.sign(domain=self._DOMAIN, payload=unsigned.unsigned_payload()),
        )


class RealExecutionAuthorizationVerifier:
    _DOMAIN = RealExecutionAuthorizationAuthority._DOMAIN

    def __init__(self, *, trust_anchor: RealVerifierTrustAnchor) -> None:
        self._anchor = trust_anchor

    def verify(
        self,
        authorization: RealExecutionAuthorization,
        *,
        source: str,
        dataset: DatasetKind,
        output_root: Path,
        sample_sha256: str,
        at: UtcTimestamp,
    ) -> bool:
        return (
            authorization.signer_key_id == self._anchor.key_id
            and self._anchor.verify(
                domain=self._DOMAIN,
                payload=authorization.unsigned_payload(),
                signature=authorization.signature,
            )
            and authorization.source == source
            and authorization.dataset is dataset
            and authorization.output_root == str(output_root.resolve(strict=False))
            and authorization.sample_sha256 == sample_sha256
            and authorization.valid_from.value <= at.value < authorization.expires_at.value
        )


@dataclass(frozen=True, slots=True)
class RealRuntimeAuthorizationEnvelope:
    """Strict C1A-R1 canonical envelope with an asymmetric signer identity."""

    authorization: RealExecutionAuthorization
    identity: ValidationAttemptIdentityV3
    selected_case_ids: tuple[str, ...]
    reference_bundle_id: str
    reference_bundle_sha256: str
    source_dataset_contract: SourceDatasetContractV3
    promotion_profile: PromotionProfileV3
    output_scope: str
    frozen_versions: Mapping[str, str]
    plan_sha256: str
    claim_nonce: str
    authorization_key_id: str
    runner_key_id: str
    signature: str
    schema_version: str = REAL_RUNTIME_AUTHORIZATION_SCHEMA
    purpose: str = REAL_RUNTIME_AUTHORIZATION_PURPOSE

    def __post_init__(self) -> None:
        if (
            self.schema_version != REAL_RUNTIME_AUTHORIZATION_SCHEMA
            or self.purpose != REAL_RUNTIME_AUTHORIZATION_PURPOSE
        ):
            raise RealAuthorizationError("REAL authorization schema or purpose is invalid")
        if self.identity.authorization_id != self.authorization.authorization_id:
            raise RealAuthorizationError("REAL authorization identity is unbound")
        if (
            self.authorization.source != "XTQUANT"
            or self.authorization.dataset is not DatasetKind.TRADING_STATUS
            or self.source_dataset_contract.source != self.authorization.source
            or self.source_dataset_contract.dataset is not self.authorization.dataset
            or self.promotion_profile.source != self.authorization.source
            or self.promotion_profile.dataset is not self.authorization.dataset
            or self.selected_case_ids != ("st_warning_603377_sh",)
            or self.reference_bundle_id != self.promotion_profile.reference_bundle_id
            or self.reference_bundle_sha256
            != self.promotion_profile.reference_bundle_manifest_sha256
            or self.output_scope != self.authorization.output_root
        ):
            raise RealAuthorizationError("REAL authorization controlled bindings are invalid")
        if set(self.frozen_versions) != _VERSION_FIELDS or any(
            type(key) is not str or type(value) is not str
            for key, value in self.frozen_versions.items()
        ):
            raise RealAuthorizationError("REAL authorization versions are incomplete")
        object.__setattr__(
            self, "frozen_versions", MappingProxyType(dict(sorted(self.frozen_versions.items())))
        )
        if any(
            not isinstance(value, str) or not _HEX64.fullmatch(value)
            for value in (self.reference_bundle_sha256, self.plan_sha256, self.claim_nonce)
        ):
            raise RealAuthorizationError("REAL authorization digest is invalid")
        if (
            any(
                not isinstance(value, str) or not _KEY_ID.fullmatch(value)
                for value in (self.authorization_key_id, self.runner_key_id)
            )
            or not isinstance(self.signature, str)
            or not re.fullmatch(r"[0-9a-f]{128}", self.signature)
        ):
            raise RealAuthorizationError("REAL authorization key or signature is invalid")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.to_dict(),
            "authorization_key_id": self.authorization_key_id,
            "case_ids": list(self.selected_case_ids),
            "claim_nonce": self.claim_nonce,
            "frozen_versions": dict(self.frozen_versions),
            "identity": self.identity.to_dict(),
            "output_scope": self.output_scope,
            "plan_sha256": self.plan_sha256,
            "promotion_profile": self.promotion_profile.to_dict(),
            "purpose": self.purpose,
            "reference_bundle_id": self.reference_bundle_id,
            "reference_bundle_sha256": self.reference_bundle_sha256,
            "runner_key_id": self.runner_key_id,
            "schema_version": self.schema_version,
            "source_dataset_contract": self.source_dataset_contract.to_dict(),
        }

    def to_bytes(self) -> bytes:
        return _canonical({**self.unsigned_payload(), "signature": self.signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> RealRuntimeAuthorizationEnvelope:
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealAuthorizationError("REAL authorization is invalid JSON") from error
        expected = {
            "authorization",
            "authorization_key_id",
            "case_ids",
            "claim_nonce",
            "frozen_versions",
            "identity",
            "output_scope",
            "plan_sha256",
            "promotion_profile",
            "purpose",
            "reference_bundle_id",
            "reference_bundle_sha256",
            "runner_key_id",
            "schema_version",
            "signature",
            "source_dataset_contract",
        }
        if not isinstance(raw, dict) or payload != _canonical(raw) or set(raw) != expected:
            raise RealAuthorizationError("REAL authorization fields are invalid")
        try:
            authorization = raw["authorization"]
            identity = raw["identity"]
            contract = raw["source_dataset_contract"]
            profile = raw["promotion_profile"]
            frozen = raw["frozen_versions"]
            cases = raw["case_ids"]
            if not all(
                isinstance(value, dict)
                for value in (authorization, identity, contract, profile, frozen)
            ) or not isinstance(cases, list):
                raise ValueError
            candidate = cls(
                authorization=RealExecutionAuthorization.from_dict(authorization),
                identity=ValidationAttemptIdentityV3.from_dict(identity),
                selected_case_ids=tuple(cases),
                reference_bundle_id=raw["reference_bundle_id"],
                reference_bundle_sha256=raw["reference_bundle_sha256"],
                source_dataset_contract=SourceDatasetContractV3(
                    source=contract["source"],
                    dataset=DatasetKind(contract["dataset"]),
                    contract_version=contract["contract_version"],
                    semantic_rule_version=contract["semantic_rule_version"],
                    unit_rule_version=contract["unit_rule_version"],
                    coverage_rule_version=contract["coverage_rule_version"],
                    expected_schema_version=contract["expected_schema_version"],
                ),
                promotion_profile=PromotionProfileV3(
                    source=profile["source"],
                    dataset=DatasetKind(profile["dataset"]),
                    promotion_profile_version=profile["promotion_profile_version"],
                    promotion_config_version=profile["promotion_config_version"],
                    required_case_ids=tuple(profile["required_case_ids"]),
                    reference_bundle_id=profile["reference_bundle_id"],
                    reference_bundle_manifest_sha256=profile["reference_bundle_manifest_sha256"],
                    minimum_stability_runs=profile["minimum_stability_runs"],
                    maximum_total_differences=profile["maximum_total_differences"],
                ),
                output_scope=raw["output_scope"],
                frozen_versions=MappingProxyType(dict(frozen)),
                plan_sha256=raw["plan_sha256"],
                claim_nonce=raw["claim_nonce"],
                authorization_key_id=raw["authorization_key_id"],
                runner_key_id=raw["runner_key_id"],
                signature=raw["signature"],
                schema_version=raw["schema_version"],
                purpose=raw["purpose"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealAuthorizationError("REAL authorization is invalid") from error
        if candidate.to_bytes() != payload:
            raise RealAuthorizationError("REAL authorization is not canonical typed JSON")
        return candidate


class RealRuntimeAuthorizationFileVerifier(Protocol):
    def verify_real_file_authorization(
        self, envelope: RealRuntimeAuthorizationEnvelope
    ) -> bool: ...


class RealRuntimeAuthorizationVerifier(RealRuntimeAuthorizationFileVerifier):
    _DOMAIN = "quantking.s1-07c1a-r1.runtime-authorization.v2"

    def __init__(self, *, trust_anchor: RealVerifierTrustAnchor) -> None:
        self._anchor = trust_anchor

    def verify_real_file_authorization(self, envelope: RealRuntimeAuthorizationEnvelope) -> bool:
        return (
            envelope.authorization_key_id == self._anchor.key_id
            and envelope.authorization.signer_key_id == self._anchor.key_id
            and self._anchor.verify(
                domain=self._DOMAIN,
                payload=envelope.unsigned_payload(),
                signature=envelope.signature,
            )
        )


@dataclass(frozen=True, slots=True)
class LoadedRealRuntimeAuthorization:
    envelope: RealRuntimeAuthorizationEnvelope
    envelope_sha256: str
    raw_bytes: bytes
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _LOADED_REAL_AUTHORIZATION_SEAL or self.envelope_sha256 != _sha(
            self.raw_bytes
        ):
            raise RealAuthorizationError("REAL authorization must come from its file loader")


_LOADED_REAL_AUTHORIZATION_SEAL = object()


class RealRuntimeAuthorizationFileLoader:
    """The only R1 execution entrance for a canonical REAL file."""

    def __init__(self, *, verifier: RealRuntimeAuthorizationFileVerifier) -> None:
        self._verifier = verifier

    def load(self, path: Path) -> LoadedRealRuntimeAuthorization:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or not _path_has_no_reparse_points(path)
        ):
            raise RealAuthorizationError("REAL authorization file is unsafe")
        try:
            raw = path.read_bytes()
            envelope = RealRuntimeAuthorizationEnvelope.from_bytes(raw)
        except (OSError, ValueError) as error:
            raise RealAuthorizationError("REAL authorization file is invalid") from error
        if not self._verifier.verify_real_file_authorization(envelope):
            raise RealAuthorizationError("REAL authorization file is untrusted")
        return LoadedRealRuntimeAuthorization(
            envelope=envelope,
            envelope_sha256=_sha(raw),
            raw_bytes=raw,
            _seal=_LOADED_REAL_AUTHORIZATION_SEAL,
        )


class RealRuntimeAuthorizationAuthority:
    """Explicit private-key envelope issuer; it is never a verifier."""

    _DOMAIN = RealRuntimeAuthorizationVerifier._DOMAIN

    def __init__(self, *, private_key_file: Path) -> None:
        self._key = ExplicitLocalRealKey(private_key_file=private_key_file)

    @property
    def key_id(self) -> str:
        return self._key.key_id

    def issue(
        self,
        *,
        authorization: RealExecutionAuthorization,
        identity: ValidationAttemptIdentityV3,
        selected_case_ids: tuple[str, ...],
        reference_bundle_id: str,
        reference_bundle_sha256: str,
        source_dataset_contract: SourceDatasetContractV3,
        promotion_profile: PromotionProfileV3,
        output_scope: str,
        frozen_versions: Mapping[str, str],
        plan_sha256: str,
        claim_nonce: str,
        runner_key_id: str,
    ) -> RealRuntimeAuthorizationEnvelope:
        unsigned = RealRuntimeAuthorizationEnvelope(
            authorization=authorization,
            identity=identity,
            selected_case_ids=selected_case_ids,
            reference_bundle_id=reference_bundle_id,
            reference_bundle_sha256=reference_bundle_sha256,
            source_dataset_contract=source_dataset_contract,
            promotion_profile=promotion_profile,
            output_scope=output_scope,
            frozen_versions=frozen_versions,
            plan_sha256=plan_sha256,
            claim_nonce=claim_nonce,
            authorization_key_id=self.key_id,
            runner_key_id=runner_key_id,
            signature="0" * 128,
        )
        return replace(
            unsigned,
            signature=self._key.sign(domain=self._DOMAIN, payload=unsigned.unsigned_payload()),
        )


@dataclass(frozen=True, slots=True)
class RealClaimReceipt:
    authorization_id: EntityId
    invocation_id: EntityId
    run_id: EntityId
    claimed_at: UtcTimestamp
    claim_nonce: str
    authorization_envelope_sha256: str
    plan_sha256: str
    schema_version: str = REAL_CLAIM_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.authorization_id.namespace != "data_authorization"
            or self.invocation_id.namespace != "data_invocation"
            or self.run_id.namespace != "data_validation"
            or self.schema_version != REAL_CLAIM_RECEIPT_SCHEMA
            or any(
                not _HEX64.fullmatch(value)
                for value in (
                    self.claim_nonce,
                    self.authorization_envelope_sha256,
                    self.plan_sha256,
                )
            )
        ):
            raise RealAuthorizationError("REAL claim receipt is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "authorization_envelope_sha256": self.authorization_envelope_sha256,
            "authorization_id": str(self.authorization_id),
            "claim_nonce": self.claim_nonce,
            "claimed_at": self.claimed_at.to_iso(),
            "invocation_id": str(self.invocation_id),
            "plan_sha256": self.plan_sha256,
            "run_id": str(self.run_id),
            "schema_version": self.schema_version,
        }

    def to_bytes(self) -> bytes:
        return _canonical(self.to_dict())

    @property
    def sha256(self) -> str:
        return _sha(self.to_bytes())

    @classmethod
    def from_bytes(cls, payload: bytes) -> RealClaimReceipt:
        try:
            raw = json.loads(payload)
            expected = {
                "authorization_envelope_sha256",
                "authorization_id",
                "claim_nonce",
                "claimed_at",
                "invocation_id",
                "plan_sha256",
                "run_id",
                "schema_version",
            }
            if (
                not isinstance(raw, dict)
                or set(raw) != expected
                or payload != _canonical(raw)
                or any(type(item) is not str for item in raw.values())
            ):
                raise ValueError
            return cls(
                authorization_id=parse_entity_id(raw["authorization_id"]),
                invocation_id=parse_entity_id(raw["invocation_id"]),
                run_id=parse_entity_id(raw["run_id"]),
                claimed_at=UtcTimestamp.from_iso(raw["claimed_at"]),
                claim_nonce=raw["claim_nonce"],
                authorization_envelope_sha256=raw["authorization_envelope_sha256"],
                plan_sha256=raw["plan_sha256"],
                schema_version=raw["schema_version"],
            )
        except (TypeError, ValueError) as error:
            raise RealAuthorizationError("REAL claim receipt is invalid") from error


class RealAuthorizationClaimStore:
    """One-time, immutable R1 receipt store; C0B's claim store is untouched."""

    def __init__(self, *, claim_root: Path) -> None:
        if not isinstance(claim_root, Path) or not claim_root.is_absolute():
            raise ValueError("claim_root must be an absolute Path")
        self._root = claim_root

    def claim(
        self,
        *,
        loaded: LoadedRealRuntimeAuthorization,
        verifier: RealExecutionAuthorizationVerifier,
        at: UtcTimestamp,
    ) -> RealClaimReceipt:
        envelope = loaded.envelope
        if not verifier.verify(
            envelope.authorization,
            source="XTQUANT",
            dataset=DatasetKind.TRADING_STATUS,
            output_root=Path(envelope.output_scope),
            sample_sha256=envelope.authorization.sample_sha256,
            at=at,
        ):
            raise RealAuthorizationError("AUTHORIZATION_INVALID_OR_EXPIRED")
        receipt = RealClaimReceipt(
            authorization_id=envelope.authorization.authorization_id,
            invocation_id=envelope.identity.invocation_id,
            run_id=envelope.identity.run_id,
            claimed_at=at,
            claim_nonce=envelope.claim_nonce,
            authorization_envelope_sha256=loaded.envelope_sha256,
            plan_sha256=envelope.plan_sha256,
        )
        root = _prepare_claim_root(self._root)
        target = root / f"{envelope.authorization.authorization_id.value.hex}.json"
        if is_reparse_point(target):
            raise RealAuthorizationError("AUTHORIZATION_CLAIM_STORE_UNSAFE")
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RealAuthorizationError("AUTHORIZATION_ALREADY_CONSUMED") from error
        except OSError as error:
            raise RealAuthorizationError("AUTHORIZATION_CLAIM_PERSISTENCE_FAILED") from error
        try:
            payload = receipt.to_bytes()
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        except OSError as error:
            raise RealAuthorizationError("AUTHORIZATION_CLAIM_PERSISTENCE_FAILED") from error
        finally:
            os.close(descriptor)
        return receipt


def _prepare_claim_root(root: Path) -> Path:
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.exists() and is_reparse_point(current):
            raise RealAuthorizationError("AUTHORIZATION_CLAIM_STORE_UNSAFE")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RealAuthorizationError("AUTHORIZATION_CLAIM_PERSISTENCE_FAILED") from error
    if not root.is_dir() or is_reparse_point(root):
        raise RealAuthorizationError("AUTHORIZATION_CLAIM_STORE_UNSAFE")
    return root


def _path_has_no_reparse_points(path: Path) -> bool:
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if current.exists() and (current.is_symlink() or is_reparse_point(current)):
                return False
    except OSError:
        return False
    return True


__all__ = [
    "REAL_CLAIM_RECEIPT_SCHEMA",
    "REAL_EXECUTION_AUTHORIZATION_SCHEMA",
    "REAL_RUNTIME_AUTHORIZATION_PURPOSE",
    "REAL_RUNTIME_AUTHORIZATION_SCHEMA",
    "ExplicitLocalRealKey",
    "LoadedRealRuntimeAuthorization",
    "LocalRealV3Signer",
    "RealAuthorizationClaimStore",
    "RealAuthorizationError",
    "RealClaimReceipt",
    "RealExecutionAuthorization",
    "RealExecutionAuthorizationAuthority",
    "RealExecutionAuthorizationVerifier",
    "RealRuntimeAuthorizationAuthority",
    "RealRuntimeAuthorizationEnvelope",
    "RealRuntimeAuthorizationFileLoader",
    "RealRuntimeAuthorizationFileVerifier",
    "RealRuntimeAuthorizationVerifier",
    "RealV3RunnerTrustPolicy",
    "RealVerifierTrustAnchor",
]
