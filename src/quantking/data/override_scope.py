"""Canonical exact-difference scope contract for SourceOverride."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from quantking.data.schemas import DatasetSchemaDescriptor, FieldType, descriptor_for


@dataclass(frozen=True, slots=True)
class ExactOverrideScope:
    """Normalized exact units plus their deterministic legacy summary."""

    field_replacements: tuple[Mapping[str, str], ...]
    fallback_only_keys: tuple[Mapping[str, str], ...]
    scope_codes: tuple[str, ...]
    scope_dates: tuple[str, ...]
    fields: tuple[str, ...]
    sha256: str

    def deterministic_dict(self) -> dict[str, list[dict[str, str]]]:
        return {
            "field_replacements": [dict(unit) for unit in self.field_replacements],
            "fallback_only_keys": [dict(unit) for unit in self.fallback_only_keys],
        }


def _text_mapping(unit: Mapping[str, str], *, label: str) -> dict[str, str]:
    if not isinstance(unit, Mapping) or not unit:
        raise TypeError(f"{label} must contain non-empty mappings")
    normalized: dict[str, str] = {}
    for key, value in unit.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label} contains an empty or non-text field name")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} contains an empty or non-text value")
        normalized[key] = value
    return normalized


def _date_primary_key(descriptor: DatasetSchemaDescriptor) -> str:
    date_keys = tuple(
        field.name
        for field in descriptor.fields
        if field.name in descriptor.primary_key and field.field_type is FieldType.DATE
    )
    if len(date_keys) != 1:
        raise ValueError(f"dataset {descriptor.name!r} must have exactly one date primary key")
    return date_keys[0]


def normalize_override_scope(
    *,
    dataset: str,
    field_replacements: tuple[Mapping[str, str], ...],
    fallback_only_keys: tuple[Mapping[str, str], ...],
) -> ExactOverrideScope:
    """Validate and normalize exact units using the canonical dataset descriptor."""
    if not isinstance(field_replacements, tuple):
        raise TypeError("field_replacements must be a tuple")
    if not isinstance(fallback_only_keys, tuple):
        raise TypeError("fallback_only_keys must be a tuple")
    if not field_replacements and not fallback_only_keys:
        raise ValueError("override must contain at least one exact difference unit")

    descriptor = descriptor_for(dataset)
    primary_key = descriptor.primary_key
    primary_key_set = set(primary_key)
    allowed_fields = set(descriptor.field_names) - primary_key_set
    date_key = _date_primary_key(descriptor)
    identity_keys = tuple(key for key in primary_key if key != date_key)
    if len(identity_keys) != 1:
        raise ValueError(f"dataset {dataset!r} must have one non-date summary primary key")
    identity_key = identity_keys[0]

    normalized_fields: list[dict[str, str]] = []
    normalized_rows: list[dict[str, str]] = []
    seen_fields: set[tuple[str, ...]] = set()
    seen_rows: set[tuple[str, ...]] = set()

    for raw in field_replacements:
        unit = _text_mapping(raw, label="field_replacements")
        if set(unit) != primary_key_set | {"field"}:
            raise ValueError(
                "FIELD_REPLACEMENT must contain the complete dataset primary key and one field"
            )
        if unit["field"] not in allowed_fields:
            raise ValueError("FIELD_REPLACEMENT field must be a legal non-primary-key field")
        _validate_date(unit[date_key], field=date_key)
        identity = (*tuple(unit[key] for key in primary_key), unit["field"])
        if identity in seen_fields:
            raise ValueError("field_replacements must not contain duplicates")
        seen_fields.add(identity)
        normalized_fields.append({key: unit[key] for key in (*primary_key, "field")})

    for raw in fallback_only_keys:
        unit = _text_mapping(raw, label="fallback_only_keys")
        if set(unit) != primary_key_set:
            raise ValueError("ROW_INSERT must contain the complete dataset primary key only")
        _validate_date(unit[date_key], field=date_key)
        identity = tuple(unit[key] for key in primary_key)
        if identity in seen_rows:
            raise ValueError("fallback_only_keys must not contain duplicates")
        seen_rows.add(identity)
        normalized_rows.append({key: unit[key] for key in primary_key})

    normalized_fields.sort(key=lambda unit: tuple(unit[key] for key in (*primary_key, "field")))
    normalized_rows.sort(key=lambda unit: tuple(unit[key] for key in primary_key))
    all_units = [*normalized_fields, *normalized_rows]
    scope_codes = tuple(sorted({unit[identity_key] for unit in all_units}))
    scope_dates = tuple(sorted({unit[date_key] for unit in all_units}))
    fields = tuple(sorted({unit["field"] for unit in normalized_fields}))
    payload = {
        "field_replacements": normalized_fields,
        "fallback_only_keys": normalized_rows,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ExactOverrideScope(
        field_replacements=tuple(MappingProxyType(unit) for unit in normalized_fields),
        fallback_only_keys=tuple(MappingProxyType(unit) for unit in normalized_rows),
        scope_codes=scope_codes,
        scope_dates=scope_dates,
        fields=fields,
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def contribution_scope_sha256(*, dataset: str, scope: ExactOverrideScope) -> str:
    """Return the manifest contribution SHA derived from the same exact units."""
    descriptor = descriptor_for(dataset)
    grouped: dict[tuple[tuple[str, str], ...], set[str]] = {}
    for unit in scope.field_replacements:
        key = tuple((name, unit[name]) for name in descriptor.primary_key)
        grouped.setdefault(key, set()).add(unit["field"])
    contributions: list[dict[str, object]] = [
        {
            "primary_key": dict(key),
            "kind": "FIELD_REPLACEMENT",
            "fields": sorted(fields),
        }
        for key, fields in sorted(grouped.items())
    ]
    inserted_fields = sorted(set(descriptor.field_names) - set(descriptor.primary_key))
    contributions.extend(
        {
            "primary_key": dict(unit),
            "kind": "ROW_INSERT",
            "fields": inserted_fields,
        }
        for unit in scope.fallback_only_keys
    )
    contributions.sort(
        key=lambda item: (
            json.dumps(item["primary_key"], separators=(",", ":"), sort_keys=True),
            str(item["kind"]),
        )
    )
    summary = {
        "field_replacement_cells": str(len(scope.field_replacements)),
        "row_insert_count": str(len(scope.fallback_only_keys)),
        "affected_primary_key_count": str(len(contributions)),
    }
    canonical = json.dumps(
        {"contributions": contributions, "summary": summary},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def infer_request_scope(
    *,
    field_replacements: tuple[Mapping[str, str], ...],
    fallback_only_keys: tuple[Mapping[str, str], ...],
) -> ExactOverrideScope:
    """Validate an API request before its DifferenceReport supplies the dataset."""
    candidates: list[ExactOverrideScope] = []
    for descriptor in DatasetSchemaDescriptor.all():
        try:
            candidate = normalize_override_scope(
                dataset=descriptor.name,
                field_replacements=field_replacements,
                fallback_only_keys=fallback_only_keys,
            )
        except (KeyError, TypeError, ValueError):
            continue
        candidates.append(candidate)
    if not candidates:
        raise ValueError("exact difference units do not match a canonical dataset descriptor")
    projection = (
        candidates[0].scope_codes,
        candidates[0].scope_dates,
        candidates[0].fields,
    )
    if any(
        (item.scope_codes, item.scope_dates, item.fields) != projection for item in candidates[1:]
    ):
        raise ValueError("exact difference units have an ambiguous summary projection")
    return candidates[0]


def _validate_date(value: str, *, field: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO calendar date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use canonical YYYY-MM-DD form")


__all__ = [
    "ExactOverrideScope",
    "contribution_scope_sha256",
    "infer_request_scope",
    "normalize_override_scope",
]
