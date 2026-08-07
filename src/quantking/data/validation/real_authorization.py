"""C1A REAL-only authorization and signer primitives.

This module intentionally has a different canonical file schema and purpose
from C0B's ``runtime-authorization-envelope.v2``.  It never accepts the C0B
Fake authority, does not generate key material, and reads secret material only
from an explicitly supplied absolute local file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from quantking.common.ids import EntityId
from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.assessment.models import EvidenceKind
from quantking.data.models import DatasetKind
from quantking.data.validation.authorization import (
    AuthorizationVerifier,
    ExecutionAuthorization,
)
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

REAL_RUNTIME_AUTHORIZATION_SCHEMA = "runtime-authorization-envelope.real.v1"
REAL_RUNTIME_AUTHORIZATION_PURPOSE = "S1-07C1A_XTQUANT_TRADING_STATUS_REAL"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[a-z][a-z0-9.-]{2,127}$")
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


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class RealAuthorizationError(ValueError):
    """A REAL authorization key, signature, or canonical file is invalid."""


class ExplicitLocalRealKey:
    """HMAC key material loaded from one explicit private local file.

    The signer and verifier APIs use a private key rather than a default or
    repository-resident secret.  HMAC is sufficient here because C1A's local
    verifier is the same explicitly configured local authority; the key is
    never serialized into evidence, an artifact, an environment variable, or
    a log payload.
    """

    def __init__(self, *, key_id: str, private_key_file: Path) -> None:
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            raise RealAuthorizationError("REAL key ID is invalid")
        if key_id == FAKE_V3_RUNNER_KEY_ID or key_id.startswith("quantking.fake-"):
            raise RealAuthorizationError("Fake key IDs cannot be REAL keys")
        if not isinstance(private_key_file, Path) or not private_key_file.is_absolute():
            raise RealAuthorizationError("REAL private key file must be an explicit absolute Path")
        if not _path_has_no_reparse_points(private_key_file):
            raise RealAuthorizationError("REAL private key file is unsafe")
        try:
            payload = private_key_file.read_bytes()
        except OSError as error:
            raise RealAuthorizationError("REAL private key file is unavailable") from error
        if len(payload) < 32:
            raise RealAuthorizationError("REAL private key file is invalid")
        self._key_id = key_id
        self._payload = payload

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, *, domain: str, payload: Mapping[str, object]) -> str:
        return hmac.new(
            self._payload,
            _canonical({"domain": domain, "payload": dict(payload)}),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, *, domain: str, payload: Mapping[str, object], signature: str) -> bool:
        return isinstance(signature, str) and hmac.compare_digest(
            self.sign(domain=domain, payload=payload), signature
        )


class LocalRealV3Signer(V3ResultAttestationSigner):
    """Production REAL v3 signer/verifier backed by explicit local key material."""

    _DOMAIN = "quantking.s1-07c1a.result-attestation.v1"

    def __init__(self, *, key_id: str, private_key_file: Path) -> None:
        self._key = ExplicitLocalRealKey(key_id=key_id, private_key_file=private_key_file)

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
        return attestation.signer_key_id == self.key_id and self._key.verify(
            domain=self._DOMAIN,
            payload=attestation.unsigned_payload(),
            signature=attestation.signature,
        )


class RealV3RunnerTrustPolicy(V3RunnerTrustPolicy):
    """Verifier-owned REAL key allowlist; no caller-provided profile is trusted."""

    def __init__(self, *, verifier_key_ids: frozenset[str]) -> None:
        if not verifier_key_ids or any(
            not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
            or key_id == FAKE_V3_RUNNER_KEY_ID
            or key_id.startswith("quantking.fake-")
            for key_id in verifier_key_ids
        ):
            raise RealAuthorizationError("REAL verifier key allowlist is invalid")
        self._key_ids = frozenset(verifier_key_ids)

    def evidence_kind_for(self, *, attestation_key_id: str, verifier_key_id: str) -> EvidenceKind:
        if (
            attestation_key_id != verifier_key_id
            or verifier_key_id not in self._key_ids
            or attestation_key_id == FAKE_V3_RUNNER_KEY_ID
        ):
            raise RealAuthorizationError("REAL runner key is not verifier-allowlisted")
        return EvidenceKind.REAL


class RealExecutionAuthorizationAuthority(AuthorizationVerifier):
    """Issues and verifies one-time execution authorizations with a REAL key."""

    _DOMAIN = "quantking.s1-07c1a.execution-authorization.v1"

    def __init__(self, *, key_id: str, private_key_file: Path) -> None:
        self._key = ExplicitLocalRealKey(key_id=key_id, private_key_file=private_key_file)

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
    ) -> ExecutionAuthorization:
        start = valid_from or utc_now()
        unsigned = ExecutionAuthorization(
            authorization_id=authorization_id,
            source=source,
            dataset=dataset,
            output_root=str(output_root.resolve(strict=False)),
            sample_sha256=sample_sha256,
            valid_from=start,
            expires_at=expires_at,
            signature="0" * 64,
        )
        return replace(
            unsigned,
            signature=self._key.sign(domain=self._DOMAIN, payload=unsigned.unsigned_payload()),
        )

    def verify(
        self,
        authorization: ExecutionAuthorization,
        *,
        source: str,
        dataset: DatasetKind,
        output_root: Path,
        sample_sha256: str,
        at: UtcTimestamp,
    ) -> bool:
        return (
            self._key.verify(
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
    """Strict C1A REAL-only canonical authorization-file contract."""

    authorization: ExecutionAuthorization
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
        if self.schema_version != REAL_RUNTIME_AUTHORIZATION_SCHEMA:
            raise RealAuthorizationError("REAL authorization schema is invalid")
        if self.purpose != REAL_RUNTIME_AUTHORIZATION_PURPOSE:
            raise RealAuthorizationError("REAL authorization purpose is invalid")
        if self.identity.authorization_id != self.authorization.authorization_id:
            raise RealAuthorizationError("REAL authorization identity is unbound")
        if (
            self.authorization.source != "XTQUANT"
            or self.authorization.dataset is not DatasetKind.TRADING_STATUS
            or self.source_dataset_contract.source != self.authorization.source
            or self.source_dataset_contract.dataset is not self.authorization.dataset
            or self.promotion_profile.source != self.authorization.source
            or self.promotion_profile.dataset is not self.authorization.dataset
        ):
            raise RealAuthorizationError("REAL authorization source/dataset is invalid")
        if (
            self.selected_case_ids != ("st_warning_603377_sh",)
            or self.reference_bundle_id != self.promotion_profile.reference_bundle_id
            or self.reference_bundle_sha256
            != self.promotion_profile.reference_bundle_manifest_sha256
            or self.output_scope != self.authorization.output_root
        ):
            raise RealAuthorizationError("REAL authorization controlled bindings are invalid")
        if set(self.frozen_versions) != _VERSION_FIELDS or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.frozen_versions.items()
        ):
            raise RealAuthorizationError("REAL authorization versions are incomplete")
        object.__setattr__(
            self,
            "frozen_versions",
            MappingProxyType(dict(sorted(self.frozen_versions.items()))),
        )
        for value in (
            self.reference_bundle_sha256,
            self.plan_sha256,
            self.claim_nonce,
            self.signature,
        ):
            if not isinstance(value, str) or not _HEX64.fullmatch(value):
                raise RealAuthorizationError("REAL authorization digest is invalid")
        for key_id in (self.authorization_key_id, self.runner_key_id):
            if (
                not isinstance(key_id, str)
                or not _KEY_ID.fullmatch(key_id)
                or key_id == FAKE_V3_RUNNER_KEY_ID
                or key_id.startswith("quantking.fake-")
            ):
                raise RealAuthorizationError("REAL authorization key ID is invalid")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "authorization": json.loads(self.authorization.to_bytes()),
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
        if (
            not isinstance(raw, dict)
            or payload != _canonical(raw)
            or set(raw) != expected
            or not isinstance(raw["authorization"], dict)
            or not isinstance(raw["identity"], dict)
            or not isinstance(raw["source_dataset_contract"], dict)
            or not isinstance(raw["promotion_profile"], dict)
            or not isinstance(raw["frozen_versions"], dict)
            or not isinstance(raw["case_ids"], list)
            or set(raw["source_dataset_contract"])
            != {
                "contract_version",
                "coverage_rule_version",
                "dataset",
                "expected_schema_version",
                "semantic_rule_version",
                "source",
                "unit_rule_version",
            }
            or set(raw["promotion_profile"])
            != {
                "dataset",
                "maximum_total_differences",
                "minimum_stability_runs",
                "promotion_config_version",
                "promotion_profile_version",
                "reference_bundle_id",
                "reference_bundle_manifest_sha256",
                "required_case_ids",
                "source",
            }
        ):
            raise RealAuthorizationError("REAL authorization fields are invalid")
        _require_scalar_types(raw)
        contract = raw["source_dataset_contract"]
        profile = raw["promotion_profile"]
        assert isinstance(contract, dict)
        assert isinstance(profile, dict)
        try:
            candidate = cls(
                authorization=ExecutionAuthorization.from_bytes(_canonical(raw["authorization"])),
                identity=ValidationAttemptIdentityV3.from_dict(raw["identity"]),
                selected_case_ids=tuple(raw["case_ids"]),
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
                frozen_versions=MappingProxyType(dict(raw["frozen_versions"])),
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


def _require_scalar_types(raw: Mapping[str, object]) -> None:
    text = {
        "authorization_key_id",
        "claim_nonce",
        "output_scope",
        "plan_sha256",
        "purpose",
        "reference_bundle_id",
        "reference_bundle_sha256",
        "runner_key_id",
        "schema_version",
        "signature",
    }
    if any(type(raw[name]) is not str for name in text):
        raise RealAuthorizationError("REAL authorization text field type is invalid")
    mappings = ("authorization", "identity", "source_dataset_contract")
    for name in mappings:
        value = raw[name]
        if not isinstance(value, dict) or any(
            type(key) is not str or type(item) is not str for key, item in value.items()
        ):
            raise RealAuthorizationError("REAL authorization nested text type is invalid")
    frozen = raw["frozen_versions"]
    cases = raw["case_ids"]
    assert isinstance(frozen, dict)
    assert isinstance(cases, list)
    if any(type(key) is not str or type(value) is not str for key, value in frozen.items()) or any(
        type(value) is not str for value in cases
    ):
        raise RealAuthorizationError("REAL authorization version or case type is invalid")
    profile = raw["promotion_profile"]
    assert isinstance(profile, dict)
    for name in (
        "dataset",
        "promotion_config_version",
        "promotion_profile_version",
        "reference_bundle_id",
        "reference_bundle_manifest_sha256",
        "source",
    ):
        if type(profile.get(name)) is not str:
            raise RealAuthorizationError("REAL authorization profile text type is invalid")
    if (
        type(profile.get("minimum_stability_runs")) is not int
        or type(profile.get("maximum_total_differences")) is not int
        or not isinstance(profile.get("required_case_ids"), list)
        or any(type(value) is not str for value in profile["required_case_ids"])
    ):
        raise RealAuthorizationError("REAL authorization profile type is invalid")


class RealRuntimeAuthorizationFileVerifier(Protocol):
    def verify_real_file_authorization(
        self, envelope: RealRuntimeAuthorizationEnvelope
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class LoadedRealRuntimeAuthorization:
    envelope: RealRuntimeAuthorizationEnvelope
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _LOADED_REAL_AUTHORIZATION_SEAL:
            raise RealAuthorizationError("REAL authorization must come from its file loader")


_LOADED_REAL_AUTHORIZATION_SEAL = object()


class RealRuntimeAuthorizationFileLoader:
    """The only C1A execution entrance for a canonical REAL file."""

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
            envelope = RealRuntimeAuthorizationEnvelope.from_bytes(path.read_bytes())
        except (OSError, ValueError) as error:
            raise RealAuthorizationError("REAL authorization file is invalid") from error
        if not self._verifier.verify_real_file_authorization(envelope):
            raise RealAuthorizationError("REAL authorization file is untrusted")
        return LoadedRealRuntimeAuthorization(
            envelope=envelope,
            _seal=_LOADED_REAL_AUTHORIZATION_SEAL,
        )


class RealRuntimeAuthorizationAuthority(RealRuntimeAuthorizationFileVerifier):
    """Explicitly configured REAL envelope signer/verifier."""

    _DOMAIN = "quantking.s1-07c1a.runtime-authorization.v1"

    def __init__(self, *, key_id: str, private_key_file: Path) -> None:
        self._key = ExplicitLocalRealKey(key_id=key_id, private_key_file=private_key_file)

    @property
    def key_id(self) -> str:
        return self._key.key_id

    def issue(
        self,
        *,
        authorization: ExecutionAuthorization,
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
            signature="0" * 64,
        )
        return replace(
            unsigned,
            signature=self._key.sign(domain=self._DOMAIN, payload=unsigned.unsigned_payload()),
        )

    def verify_real_file_authorization(self, envelope: RealRuntimeAuthorizationEnvelope) -> bool:
        return envelope.authorization_key_id == self.key_id and self._key.verify(
            domain=self._DOMAIN,
            payload=envelope.unsigned_payload(),
            signature=envelope.signature,
        )


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
    "REAL_RUNTIME_AUTHORIZATION_PURPOSE",
    "REAL_RUNTIME_AUTHORIZATION_SCHEMA",
    "ExplicitLocalRealKey",
    "LoadedRealRuntimeAuthorization",
    "LocalRealV3Signer",
    "RealAuthorizationError",
    "RealExecutionAuthorizationAuthority",
    "RealRuntimeAuthorizationAuthority",
    "RealRuntimeAuthorizationEnvelope",
    "RealRuntimeAuthorizationFileLoader",
    "RealRuntimeAuthorizationFileVerifier",
    "RealV3RunnerTrustPolicy",
]
