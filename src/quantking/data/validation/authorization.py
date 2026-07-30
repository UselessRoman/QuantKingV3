"""Scoped authorization, controlled signer trust and result attestations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from quantking.common.ids import EntityId, new_entity_id, parse_entity_id
from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.models import DatasetKind

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
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


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _signature(key: bytes, payload: object) -> str:
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    authorization_id: EntityId
    source: str
    dataset: DatasetKind
    output_root: str
    sample_sha256: str
    valid_from: UtcTimestamp
    expires_at: UtcTimestamp
    signature: str

    def __post_init__(self) -> None:
        if self.authorization_id.namespace != "data_authorization":
            raise ValueError("authorization_id must use data_authorization namespace")
        if not _SOURCE.fullmatch(self.source):
            raise ValueError("authorization source must be a stable uppercase identifier")
        if not Path(self.output_root).is_absolute():
            raise ValueError("authorization output_root must be absolute")
        for digest in (self.sample_sha256, self.signature):
            if not _HEX64.fullmatch(digest):
                raise ValueError("authorization digest is invalid")
        if self.expires_at.value <= self.valid_from.value:
            raise ValueError("authorization expiry must follow its start")

    def unsigned_payload(self) -> dict[str, str]:
        return {
            "authorization_id": str(self.authorization_id),
            "dataset": self.dataset.value,
            "expires_at": self.expires_at.to_iso(),
            "output_root": self.output_root,
            "sample_sha256": self.sample_sha256,
            "source": self.source,
            "valid_from": self.valid_from.to_iso(),
        }

    def to_bytes(self) -> bytes:
        return _canonical({**self.unsigned_payload(), "signature": self.signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> ExecutionAuthorization:
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("execution authorization is invalid JSON") from error
        if not isinstance(parsed, dict) or payload != _canonical(parsed):
            raise ValueError("execution authorization must be canonical JSON")
        if set(parsed) != {
            "authorization_id",
            "dataset",
            "expires_at",
            "output_root",
            "sample_sha256",
            "signature",
            "source",
            "valid_from",
        }:
            raise ValueError("execution authorization fields do not match the schema")
        return cls(
            authorization_id=parse_entity_id(str(parsed["authorization_id"])),
            source=str(parsed["source"]),
            dataset=DatasetKind(str(parsed["dataset"])),
            output_root=str(parsed["output_root"]),
            sample_sha256=str(parsed["sample_sha256"]),
            valid_from=UtcTimestamp.from_iso(str(parsed["valid_from"])),
            expires_at=UtcTimestamp.from_iso(str(parsed["expires_at"])),
            signature=str(parsed["signature"]),
        )


class AuthorizationVerifier(Protocol):
    def verify(
        self,
        authorization: ExecutionAuthorization,
        *,
        source: str,
        dataset: DatasetKind,
        output_root: Path,
        sample_sha256: str,
        at: UtcTimestamp,
    ) -> bool: ...


class HmacAuthorizationAuthority:
    def __init__(self, *, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("authorization signing key must contain at least 32 bytes")
        self._key = key

    def issue(
        self,
        *,
        source: str,
        dataset: DatasetKind,
        output_root: Path,
        sample_sha256: str,
        valid_from: UtcTimestamp | None = None,
        expires_at: UtcTimestamp | None = None,
    ) -> ExecutionAuthorization:
        start = valid_from or utc_now()
        end = expires_at or UtcTimestamp(start.value + timedelta(minutes=5))
        unsigned = ExecutionAuthorization(
            authorization_id=new_entity_id("data_authorization"),
            source=source,
            dataset=dataset,
            output_root=str(output_root.resolve(strict=False)),
            sample_sha256=sample_sha256,
            valid_from=start,
            expires_at=end,
            signature="0" * 64,
        )
        return replace(unsigned, signature=_signature(self._key, unsigned.unsigned_payload()))

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
        expected = _signature(self._key, authorization.unsigned_payload())
        return (
            hmac.compare_digest(expected, authorization.signature)
            and authorization.source == source
            and authorization.dataset is dataset
            and authorization.output_root == str(output_root.resolve(strict=False))
            and authorization.sample_sha256 == sample_sha256
            and authorization.valid_from.value <= at.value < authorization.expires_at.value
        )


class FakeAuthorizationAuthority(HmacAuthorizationAuthority):
    def __init__(self) -> None:
        super().__init__(key=b"quantking-s1-07a-r2-fake-authority-key")


class AuthorizationClaimError(Exception):
    """Stable one-time authorization claim failure."""


class AuthorizationClaimStore:
    """Cross-process immutable claim store using atomic exclusive file creation."""

    def __init__(self, *, claim_root: Path) -> None:
        if not isinstance(claim_root, Path) or not claim_root.is_absolute():
            raise ValueError("claim_root must be an absolute Path")
        self._root = claim_root

    def claim(
        self,
        authorization: ExecutionAuthorization,
        *,
        invocation_id: EntityId,
        verifier: AuthorizationVerifier,
        source: str,
        dataset: DatasetKind,
        output_root: Path,
        sample_sha256: str,
        at: UtcTimestamp,
    ) -> None:
        if invocation_id.namespace != "data_invocation":
            raise ValueError("invocation_id must use data_invocation namespace")
        if not verifier.verify(
            authorization,
            source=source,
            dataset=dataset,
            output_root=output_root,
            sample_sha256=sample_sha256,
            at=at,
        ):
            raise AuthorizationClaimError("AUTHORIZATION_INVALID_OR_EXPIRED")
        root = _prepare_claim_root(self._root)
        claim_path = root / f"{authorization.authorization_id.value.hex}.json"
        if is_reparse_point(claim_path):
            raise AuthorizationClaimError("AUTHORIZATION_CLAIM_STORE_UNSAFE")
        payload = _canonical(
            {
                "authorization_id": str(authorization.authorization_id),
                "claimed_at": at.to_iso(),
                "dataset": dataset.value,
                "expires_at": authorization.expires_at.to_iso(),
                "invocation_id": str(invocation_id),
                "output_root": str(output_root.resolve(strict=False)),
                "sample_sha256": sample_sha256,
                "source": source,
                "status": "CLAIMED",
            }
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(claim_path, flags, 0o600)
        except FileExistsError:
            raise AuthorizationClaimError("AUTHORIZATION_ALREADY_CONSUMED") from None
        except OSError:
            raise AuthorizationClaimError("AUTHORIZATION_CLAIM_PERSISTENCE_FAILED") from None
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        except OSError:
            raise AuthorizationClaimError("AUTHORIZATION_CLAIM_PERSISTENCE_FAILED") from None
        finally:
            os.close(descriptor)


def _prepare_claim_root(root: Path) -> Path:
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.exists() and is_reparse_point(current):
            raise AuthorizationClaimError("AUTHORIZATION_CLAIM_STORE_UNSAFE")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AuthorizationClaimError("AUTHORIZATION_CLAIM_PERSISTENCE_FAILED") from None
    if not root.is_dir() or is_reparse_point(root):
        raise AuthorizationClaimError("AUTHORIZATION_CLAIM_STORE_UNSAFE")
    return root


class SignerProfile(StrEnum):
    SYNTHETIC = "SYNTHETIC"
    REAL = "REAL"


@dataclass(frozen=True, slots=True)
class SignerTrustPolicy:
    _profiles: Mapping[str, SignerProfile]

    def __post_init__(self) -> None:
        profiles = dict(self._profiles)
        if profiles.get(FakeRunnerTrustStore.KEY_ID) not in {
            None,
            SignerProfile.SYNTHETIC,
        }:
            raise ValueError("Fake runner key is permanently SYNTHETIC")
        if SignerProfile.REAL in profiles.values():
            raise ValueError("current offline validation runtime has no allowlisted REAL signer")
        object.__setattr__(self, "_profiles", MappingProxyType(profiles))

    @classmethod
    def synthetic_only(cls) -> SignerTrustPolicy:
        return cls(MappingProxyType({FakeRunnerTrustStore.KEY_ID: SignerProfile.SYNTHETIC}))

    def profile_for(self, key_id: str) -> SignerProfile | None:
        return self._profiles.get(key_id)


@dataclass(frozen=True, slots=True)
class ResultAttestation:
    signer_key_id: str
    authorization_id: EntityId
    invocation_id: EntityId
    session_id: EntityId
    run_id: EntityId
    source: str
    dataset: DatasetKind
    request_sha256: str
    run_fingerprint: str
    raw_manifest_sha256: str
    canonical_content_sha256: str
    quality_manifest_sha256: str
    quality_run_id: EntityId
    difference_report_id: EntityId | None
    difference_report_sha256: str | None
    evidence_inventory_sha256: str
    versions: Mapping[str, str]
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    signature: str

    def __post_init__(self) -> None:
        for value, namespace, field in (
            (self.authorization_id, "data_authorization", "authorization_id"),
            (self.invocation_id, "data_invocation", "invocation_id"),
            (self.session_id, "data_session", "session_id"),
            (self.run_id, "data_validation", "run_id"),
            (self.quality_run_id, "data_quality", "quality_run_id"),
        ):
            if value.namespace != namespace:
                raise ValueError(f"{field} must use {namespace} namespace")
        if (
            self.difference_report_id is not None
            and self.difference_report_id.namespace != "data_diff"
        ):
            raise ValueError("difference_report_id must use data_diff namespace")
        if (self.difference_report_id is None) != (self.difference_report_sha256 is None):
            raise ValueError("DifferenceReport ID and SHA must be supplied together")
        for digest in (
            self.request_sha256,
            self.run_fingerprint,
            self.raw_manifest_sha256,
            self.canonical_content_sha256,
            self.quality_manifest_sha256,
            self.evidence_inventory_sha256,
            self.signature,
        ):
            if not _HEX64.fullmatch(digest):
                raise ValueError("result attestation digest is invalid")
        if self.difference_report_sha256 is not None and not _HEX64.fullmatch(
            self.difference_report_sha256
        ):
            raise ValueError("difference_report_sha256 is invalid")
        if set(self.versions) != _VERSION_FIELDS:
            raise ValueError("result attestation versions are incomplete")
        object.__setattr__(self, "versions", MappingProxyType(dict(sorted(self.versions.items()))))
        if self.ended_at.value <= self.started_at.value:
            raise ValueError("result attestation end must follow start")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "authorization_id": str(self.authorization_id),
            "canonical_content_sha256": self.canonical_content_sha256,
            "dataset": self.dataset.value,
            "difference_report_id": (
                str(self.difference_report_id) if self.difference_report_id else None
            ),
            "difference_report_sha256": self.difference_report_sha256,
            "ended_at": self.ended_at.to_iso(),
            "evidence_inventory_sha256": self.evidence_inventory_sha256,
            "invocation_id": str(self.invocation_id),
            "quality_manifest_sha256": self.quality_manifest_sha256,
            "quality_run_id": str(self.quality_run_id),
            "raw_manifest_sha256": self.raw_manifest_sha256,
            "request_sha256": self.request_sha256,
            "run_fingerprint": self.run_fingerprint,
            "run_id": str(self.run_id),
            "session_id": str(self.session_id),
            "signer_key_id": self.signer_key_id,
            "source": self.source,
            "started_at": self.started_at.to_iso(),
            "versions": dict(self.versions),
        }

    def to_bytes(self) -> bytes:
        return _canonical({**self.unsigned_payload(), "signature": self.signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> ResultAttestation:
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("result attestation is invalid JSON") from error
        if not isinstance(raw, dict) or payload != _canonical(raw):
            raise ValueError("result attestation must be canonical JSON")
        expected = {
            *cls.__dataclass_fields__,
        }
        if set(raw) != expected:
            raise ValueError("result attestation fields do not match the schema")
        signature = raw.pop("signature", None)
        difference_id = raw["difference_report_id"]
        return cls(
            signer_key_id=str(raw["signer_key_id"]),
            authorization_id=parse_entity_id(str(raw["authorization_id"])),
            invocation_id=parse_entity_id(str(raw["invocation_id"])),
            session_id=parse_entity_id(str(raw["session_id"])),
            run_id=parse_entity_id(str(raw["run_id"])),
            source=str(raw["source"]),
            dataset=DatasetKind(str(raw["dataset"])),
            request_sha256=str(raw["request_sha256"]),
            run_fingerprint=str(raw["run_fingerprint"]),
            raw_manifest_sha256=str(raw["raw_manifest_sha256"]),
            canonical_content_sha256=str(raw["canonical_content_sha256"]),
            quality_manifest_sha256=str(raw["quality_manifest_sha256"]),
            quality_run_id=parse_entity_id(str(raw["quality_run_id"])),
            difference_report_id=(
                parse_entity_id(str(difference_id)) if difference_id is not None else None
            ),
            difference_report_sha256=(
                str(raw["difference_report_sha256"])
                if raw["difference_report_sha256"] is not None
                else None
            ),
            evidence_inventory_sha256=str(raw["evidence_inventory_sha256"]),
            versions=MappingProxyType({str(k): str(v) for k, v in raw["versions"].items()}),
            started_at=UtcTimestamp.from_iso(str(raw["started_at"])),
            ended_at=UtcTimestamp.from_iso(str(raw["ended_at"])),
            signature=str(signature),
        )


class ResultAttestationSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def verify(self, attestation: ResultAttestation) -> bool: ...


class FakeRunnerTrustStore:
    KEY_ID = "quantking.fake-runner.v1"

    def __init__(self) -> None:
        self._key = b"quantking-s1-07a-r2-fake-runner-key"

    @property
    def key_id(self) -> str:
        return self.KEY_ID

    @property
    def profile(self) -> SignerProfile:
        return SignerProfile.SYNTHETIC

    def sign(self, attestation: ResultAttestation) -> ResultAttestation:
        if attestation.signer_key_id != self.key_id:
            raise ValueError("attestation signer_key_id does not match Fake signer")
        return replace(
            attestation,
            signature=_signature(self._key, attestation.unsigned_payload()),
        )

    def verify(self, attestation: ResultAttestation) -> bool:
        return attestation.signer_key_id == self.key_id and hmac.compare_digest(
            _signature(self._key, attestation.unsigned_payload()),
            attestation.signature,
        )


__all__ = [
    "AuthorizationClaimError",
    "AuthorizationClaimStore",
    "AuthorizationVerifier",
    "ExecutionAuthorization",
    "FakeAuthorizationAuthority",
    "FakeRunnerTrustStore",
    "HmacAuthorizationAuthority",
    "ResultAttestation",
    "ResultAttestationSigner",
    "SignerProfile",
    "SignerTrustPolicy",
]
