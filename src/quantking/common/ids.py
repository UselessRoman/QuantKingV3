"""Namespaced UUID4 entity identifiers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from uuid import RFC_4122, UUID, uuid4

from quantking.common.errors import InvalidIdError

_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
UuidFactory = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class EntityId:
    """Immutable identifier serialized as ``namespace:canonical-uuid4``."""

    namespace: str
    value: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not _NAMESPACE_PATTERN.fullmatch(self.namespace):
            raise InvalidIdError(
                "namespace must be lowercase snake case",
                context={"namespace": str(self.namespace)},
            )
        if (
            not isinstance(self.value, UUID)
            or self.value.version != 4
            or self.value.variant != RFC_4122
        ):
            raise InvalidIdError(
                "entity identifier value must be an RFC 4122 UUID4",
                context={"namespace": self.namespace},
            )

    @classmethod
    def new(cls, namespace: str, *, uuid_factory: UuidFactory = uuid4) -> EntityId:
        """Create an identifier, allowing deterministic factory injection in tests."""
        try:
            value = uuid_factory()
        except Exception as error:
            raise InvalidIdError(
                "UUID4 generation failed", context={"namespace": namespace}
            ) from error
        return cls(namespace=namespace, value=value)

    @classmethod
    def parse(cls, raw: str) -> EntityId:
        """Parse only the canonical namespaced UUID4 representation."""
        if not isinstance(raw, str) or raw.count(":") != 1:
            raise InvalidIdError("entity identifier has an invalid format")
        namespace, raw_uuid = raw.split(":", maxsplit=1)
        try:
            value = UUID(raw_uuid)
        except (AttributeError, ValueError) as error:
            raise InvalidIdError(
                "entity identifier contains an invalid UUID",
                context={"namespace": namespace},
            ) from error
        if raw_uuid != str(value):
            raise InvalidIdError(
                "entity identifier UUID must use canonical lowercase form",
                context={"namespace": namespace},
            )
        return cls(namespace=namespace, value=value)

    def __str__(self) -> str:
        return f"{self.namespace}:{self.value}"


def new_entity_id(namespace: str, *, uuid_factory: UuidFactory = uuid4) -> EntityId:
    """Create a namespaced UUID4 identifier."""
    return EntityId.new(namespace, uuid_factory=uuid_factory)


def parse_entity_id(raw: str) -> EntityId:
    """Parse a namespaced UUID4 identifier."""
    return EntityId.parse(raw)
