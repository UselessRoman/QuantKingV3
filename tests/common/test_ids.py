from uuid import UUID, uuid1

import pytest

from quantking.common.errors import InvalidIdError
from quantking.common.ids import EntityId

UUID4 = UUID("123e4567-e89b-42d3-a456-426614174000")


def test_entity_id_new_supports_deterministic_uuid_factory() -> None:
    entity_id = EntityId.new("job_run", uuid_factory=lambda: UUID4)

    assert entity_id.namespace == "job_run"
    assert entity_id.value == UUID4
    assert str(entity_id) == "job_run:123e4567-e89b-42d3-a456-426614174000"
    assert EntityId.parse(str(entity_id)) == entity_id


def test_entity_id_is_immutable() -> None:
    entity_id = EntityId.new("job", uuid_factory=lambda: UUID4)

    with pytest.raises((AttributeError, TypeError)):
        entity_id.namespace = "other"  # type: ignore[misc]


@pytest.mark.parametrize("namespace", ["", "Job", "job-run", "job run", "_job", "job_"])
def test_entity_id_rejects_invalid_namespace(namespace: str) -> None:
    with pytest.raises(InvalidIdError):
        EntityId.new(namespace, uuid_factory=lambda: UUID4)


def test_entity_id_rejects_non_uuid4_factory_value() -> None:
    with pytest.raises(InvalidIdError, match="UUID4"):
        EntityId.new("job", uuid_factory=uuid1)


@pytest.mark.parametrize(
    "raw",
    [
        "job",
        "job:not-a-uuid",
        "Job:123e4567-e89b-42d3-a456-426614174000",
        "job:123E4567-E89B-42D3-A456-426614174000",
        "job:123e4567-e89b-12d3-a456-426614174000",
    ],
)
def test_entity_id_parse_rejects_non_canonical_or_non_uuid4_values(raw: str) -> None:
    with pytest.raises(InvalidIdError):
        EntityId.parse(raw)
