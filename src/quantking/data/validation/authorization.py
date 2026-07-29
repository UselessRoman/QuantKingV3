"""Per-invocation authorization and runner-attestation contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from quantking.common.ids import EntityId, new_entity_id, parse_entity_id
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.models import DatasetKind

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")


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
        if not _HEX64.fullmatch(self.sample_sha256):
            raise ValueError("authorization sample_sha256 is invalid")
        if not _HEX64.fullmatch(self.signature):
            raise ValueError("authorization signature is invalid")
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
        required = {
            "authorization_id",
            "dataset",
            "expires_at",
            "output_root",
            "sample_sha256",
            "signature",
            "source",
            "valid_from",
        }
        if set(parsed) != required:
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
    """In-memory authority; the signing key is never serialized into evidence."""

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
        return replace(
            unsigned,
            signature=_signature(self._key, unsigned.unsigned_payload()),
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
        if not isinstance(authorization, ExecutionAuthorization):
            return False
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
    """Deterministic offline-only authority for tmp_path tests."""

    def __init__(self) -> None:
        super().__init__(key=b"quantking-s1-07a-r1-fake-authority-key")


@dataclass(frozen=True, slots=True)
class RunnerInvocationAttestation:
    invocation_id: EntityId
    session_id: EntityId
    authorization_id: EntityId
    run_id: EntityId
    source: str
    dataset: DatasetKind
    request_sha256: str
    run_fingerprint: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    signature: str

    def __post_init__(self) -> None:
        for value, namespace, field in (
            (self.invocation_id, "data_invocation", "invocation_id"),
            (self.session_id, "data_session", "session_id"),
            (self.authorization_id, "data_authorization", "authorization_id"),
            (self.run_id, "data_validation", "run_id"),
        ):
            if value.namespace != namespace:
                raise ValueError(f"{field} must use {namespace} namespace")
        if self.ended_at.value <= self.started_at.value:
            raise ValueError("runner invocation end must follow its start")
        if not _SOURCE.fullmatch(self.source):
            raise ValueError("runner source must be a stable uppercase identifier")
        for digest, field in (
            (self.request_sha256, "request_sha256"),
            (self.run_fingerprint, "run_fingerprint"),
            (self.signature, "signature"),
        ):
            if not _HEX64.fullmatch(digest):
                raise ValueError(f"runner {field} is invalid")

    def unsigned_payload(self) -> dict[str, str]:
        return {
            "authorization_id": str(self.authorization_id),
            "dataset": self.dataset.value,
            "ended_at": self.ended_at.to_iso(),
            "invocation_id": str(self.invocation_id),
            "request_sha256": self.request_sha256,
            "run_fingerprint": self.run_fingerprint,
            "run_id": str(self.run_id),
            "session_id": str(self.session_id),
            "source": self.source,
            "started_at": self.started_at.to_iso(),
        }

    def to_bytes(self) -> bytes:
        return _canonical({**self.unsigned_payload(), "signature": self.signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> RunnerInvocationAttestation:
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("runner attestation is invalid JSON") from error
        if not isinstance(parsed, dict) or payload != _canonical(parsed):
            raise ValueError("runner attestation must be canonical JSON")
        required = {
            "authorization_id",
            "dataset",
            "ended_at",
            "invocation_id",
            "request_sha256",
            "run_fingerprint",
            "run_id",
            "session_id",
            "signature",
            "source",
            "started_at",
        }
        if set(parsed) != required:
            raise ValueError("runner attestation fields do not match the schema")
        return cls(
            invocation_id=parse_entity_id(str(parsed["invocation_id"])),
            session_id=parse_entity_id(str(parsed["session_id"])),
            authorization_id=parse_entity_id(str(parsed["authorization_id"])),
            run_id=parse_entity_id(str(parsed["run_id"])),
            source=str(parsed["source"]),
            dataset=DatasetKind(str(parsed["dataset"])),
            request_sha256=str(parsed["request_sha256"]),
            run_fingerprint=str(parsed["run_fingerprint"]),
            started_at=UtcTimestamp.from_iso(str(parsed["started_at"])),
            ended_at=UtcTimestamp.from_iso(str(parsed["ended_at"])),
            signature=str(parsed["signature"]),
        )


class RunnerAttestationVerifier(Protocol):
    @property
    def establishes_real_identity(self) -> bool: ...

    def verify(self, attestation: RunnerInvocationAttestation) -> bool: ...


class FakeRunnerTrustStore:
    """Offline Fake runner signer/verifier; never used as a provider SDK."""

    def __init__(self) -> None:
        self._key = b"quantking-s1-07a-r1-fake-runner-key"

    @property
    def establishes_real_identity(self) -> bool:
        return False

    def sign(self, attestation: RunnerInvocationAttestation) -> RunnerInvocationAttestation:
        return replace(
            attestation,
            signature=_signature(self._key, attestation.unsigned_payload()),
        )

    def verify(self, attestation: RunnerInvocationAttestation) -> bool:
        expected = _signature(self._key, attestation.unsigned_payload())
        return hmac.compare_digest(expected, attestation.signature)


__all__ = [
    "AuthorizationVerifier",
    "ExecutionAuthorization",
    "FakeAuthorizationAuthority",
    "FakeRunnerTrustStore",
    "HmacAuthorizationAuthority",
    "RunnerAttestationVerifier",
    "RunnerInvocationAttestation",
]
