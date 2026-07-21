"""Freeze exact SourceOverride approval scope.

Revision ID: 20260722_0009
Revises: 20260721_0008
Create Date: 2026-07-22
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0009"
down_revision: str | None = "20260721_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DESCRIPTORS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...]]] = {
    "security_master": (
        ("code", "effective_from"),
        "effective_from",
        (
            "exchange",
            "name",
            "board",
            "listing_date",
            "delisting_date",
            "effective_to",
            "listing_status",
        ),
    ),
    "exchange_calendar": (("exchange", "trade_date"), "trade_date", ("is_open",)),
    "daily_bar_unadjusted": (
        ("code", "trade_date"),
        "trade_date",
        ("open", "high", "low", "close", "pre_close", "volume", "amount"),
    ),
    "adjustment_factor": (
        ("code", "trade_date"),
        "trade_date",
        ("price_multiplier", "volume_multiplier", "normalization_date", "algorithm_version"),
    ),
    "trading_status": (
        ("code", "trade_date"),
        "trade_date",
        (
            "is_trading",
            "is_suspended",
            "is_st",
            "is_star_st",
            "is_delisting_period",
            "limit_up_price",
            "limit_down_price",
            "status_reason",
        ),
    ),
    "benchmark_index_daily": (
        ("code", "trade_date"),
        "trade_date",
        ("open", "high", "low", "close", "pre_close", "volume", "amount"),
    ),
}

_SOURCE_TRIGGERS = (
    "source_overrides_require_published_application",
    "source_overrides_no_delete_decided",
    "source_overrides_no_update_terminal",
    "source_overrides_approved_transition",
    "source_overrides_pending_transition",
    "source_overrides_validate_insert",
)


def _canonical_existing_scope(row: sa.Row[object]) -> tuple[str, str]:
    dataset = str(row.dataset)
    descriptor = _DESCRIPTORS.get(dataset)
    if descriptor is None:
        raise RuntimeError(f"cannot upgrade SourceOverride with unknown dataset {dataset!r}")
    primary_key, date_key, allowed_fields = descriptor
    try:
        field_units = json.loads(str(row.field_replacements_json))
        row_units = json.loads(str(row.fallback_only_keys_json))
        scope_codes = json.loads(str(row.scope_codes_json))
        scope_dates = json.loads(str(row.scope_dates_json))
        fields = json.loads(str(row.fields_json))
    except (TypeError, ValueError) as error:
        raise RuntimeError("cannot upgrade SourceOverride with invalid exact-scope JSON") from error
    if not isinstance(field_units, list) or not isinstance(row_units, list):
        raise RuntimeError("cannot upgrade SourceOverride with non-array exact scope")
    if not field_units and not row_units:
        raise RuntimeError(
            "cannot upgrade legacy SourceOverride without exact DifferenceReport units; "
            "cancel or deterministically migrate the override before retrying"
        )
    normalized_fields: list[dict[str, str]] = []
    normalized_rows: list[dict[str, str]] = []
    seen_fields: set[tuple[str, ...]] = set()
    seen_rows: set[tuple[str, ...]] = set()
    for unit in field_units:
        if not isinstance(unit, dict) or set(unit) != set(primary_key) | {"field"}:
            raise RuntimeError("cannot upgrade invalid FIELD_REPLACEMENT primary key")
        if any(not isinstance(unit[key], str) or not unit[key] for key in (*primary_key, "field")):
            raise RuntimeError("cannot upgrade empty FIELD_REPLACEMENT value")
        if unit["field"] not in allowed_fields:
            raise RuntimeError("cannot upgrade illegal FIELD_REPLACEMENT field")
        try:
            parsed_date = date.fromisoformat(unit[date_key])
        except ValueError as error:
            raise RuntimeError("cannot upgrade invalid exact-scope date") from error
        if parsed_date.isoformat() != unit[date_key]:
            raise RuntimeError("cannot upgrade non-canonical exact-scope date")
        identity = (*tuple(unit[key] for key in primary_key), unit["field"])
        if identity in seen_fields:
            raise RuntimeError("cannot upgrade duplicate FIELD_REPLACEMENT")
        seen_fields.add(identity)
        normalized_fields.append({key: unit[key] for key in (*primary_key, "field")})
    for unit in row_units:
        if not isinstance(unit, dict) or set(unit) != set(primary_key):
            raise RuntimeError("cannot upgrade invalid ROW_INSERT primary key")
        if any(not isinstance(unit[key], str) or not unit[key] for key in primary_key):
            raise RuntimeError("cannot upgrade empty ROW_INSERT value")
        try:
            parsed_date = date.fromisoformat(unit[date_key])
        except ValueError as error:
            raise RuntimeError("cannot upgrade invalid exact-scope date") from error
        if parsed_date.isoformat() != unit[date_key]:
            raise RuntimeError("cannot upgrade non-canonical exact-scope date")
        identity = tuple(unit[key] for key in primary_key)
        if identity in seen_rows:
            raise RuntimeError("cannot upgrade duplicate ROW_INSERT")
        seen_rows.add(identity)
        normalized_rows.append({key: unit[key] for key in primary_key})
    normalized_fields.sort(key=lambda item: tuple(item[key] for key in (*primary_key, "field")))
    normalized_rows.sort(key=lambda item: tuple(item[key] for key in primary_key))
    identity_key = next(key for key in primary_key if key != date_key)
    all_units = [*normalized_fields, *normalized_rows]
    expected = (
        sorted({item[identity_key] for item in all_units}),
        sorted({item[date_key] for item in all_units}),
        sorted({item["field"] for item in normalized_fields}),
    )
    if (scope_codes, scope_dates, fields) != expected:
        raise RuntimeError("cannot upgrade SourceOverride whose summary disagrees with exact scope")
    payload = {
        "field_replacements": normalized_fields,
        "fallback_only_keys": normalized_rows,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    exact_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    grouped: dict[tuple[tuple[str, str], ...], set[str]] = {}
    for unit in normalized_fields:
        key = tuple((name, unit[name]) for name in primary_key)
        grouped.setdefault(key, set()).add(unit["field"])
    contributions: list[dict[str, object]] = [
        {
            "primary_key": dict(key),
            "kind": "FIELD_REPLACEMENT",
            "fields": sorted(unit_fields),
        }
        for key, unit_fields in sorted(grouped.items())
    ]
    contributions.extend(
        {
            "primary_key": dict(unit),
            "kind": "ROW_INSERT",
            "fields": sorted(allowed_fields),
        }
        for unit in normalized_rows
    )
    contributions.sort(
        key=lambda item: (
            json.dumps(item["primary_key"], separators=(",", ":"), sort_keys=True),
            str(item["kind"]),
        )
    )
    summary = {
        "field_replacement_cells": str(len(normalized_fields)),
        "row_insert_count": str(len(normalized_rows)),
        "affected_primary_key_count": str(len(contributions)),
    }
    contribution_payload = json.dumps(
        {"contributions": contributions, "summary": summary},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    contribution_sha = hashlib.sha256(contribution_payload.encode("utf-8")).hexdigest()
    return exact_sha, contribution_sha


def _drop_source_triggers() -> None:
    for name in _SOURCE_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _lineage_insert_guard() -> str:
    return """
        SELECT CASE WHEN NEW.status <> 'PENDING'
            OR NEW.primary_batch_id = NEW.fallback_batch_id OR NOT EXISTS (
                SELECT 1 FROM ingestion_batches p, ingestion_batches f,
                    difference_reports d, dataset_versions v
                WHERE p.batch_id = NEW.primary_batch_id
                  AND p.source = NEW.primary_source AND p.dataset = NEW.dataset
                  AND f.batch_id = NEW.fallback_batch_id
                  AND f.source = NEW.fallback_source AND f.dataset = NEW.dataset
                  AND d.report_id = NEW.difference_report_id
                  AND d.primary_source = NEW.primary_source
                  AND d.fallback_source = NEW.fallback_source
                  AND d.primary_batch_id = NEW.primary_batch_id
                  AND d.fallback_batch_id = NEW.fallback_batch_id
                  AND v.version_id = d.version_id AND v.dataset = NEW.dataset
            ) THEN RAISE(ABORT, 'source override lineage is inconsistent') END;
    """


def _shape_checks() -> str:
    statements: list[str] = []
    dataset_names = ",".join(f"'{name}'" for name in _DESCRIPTORS)
    statements.append(
        f"SELECT CASE WHEN NEW.dataset NOT IN ({dataset_names}) "
        "THEN RAISE(ABORT, 'unknown exact-scope dataset') END;"
    )
    for dataset, (primary_key, date_key, allowed_fields) in _DESCRIPTORS.items():
        field_keys = (*primary_key, "field")
        field_key_sql = ",".join(f"'{key}'" for key in field_keys)
        row_key_sql = ",".join(f"'{key}'" for key in primary_key)
        allowed_sql = ",".join(f"'{field}'" for field in allowed_fields)
        field_text = " OR ".join(
            f"json_type(unit.value, '$.{key}') <> 'text' "
            f"OR length(trim(json_extract(unit.value, '$.{key}'))) = 0"
            for key in field_keys
        )
        row_text = " OR ".join(
            f"json_type(unit.value, '$.{key}') <> 'text' "
            f"OR length(trim(json_extract(unit.value, '$.{key}'))) = 0"
            for key in primary_key
        )
        field_identity = ",".join(f"json_extract(value, '$.{key}')" for key in field_keys)
        row_identity = ",".join(f"json_extract(value, '$.{key}')" for key in primary_key)
        identity_key = next(key for key in primary_key if key != date_key)
        code_projection = (
            f"SELECT json_extract(value, '$.{identity_key}') AS value "
            "FROM json_each(NEW.field_replacements_json) UNION "
            f"SELECT json_extract(value, '$.{identity_key}') AS value "
            "FROM json_each(NEW.fallback_only_keys_json)"
        )
        date_projection = (
            f"SELECT json_extract(value, '$.{date_key}') AS value "
            "FROM json_each(NEW.field_replacements_json) UNION "
            f"SELECT json_extract(value, '$.{date_key}') AS value "
            "FROM json_each(NEW.fallback_only_keys_json)"
        )
        field_projection = (
            "SELECT DISTINCT json_extract(value, '$.field') AS value "
            "FROM json_each(NEW.field_replacements_json)"
        )
        statements.append(
            f"""
            SELECT CASE WHEN NEW.dataset = '{dataset}' AND EXISTS (
                SELECT 1 FROM json_each(NEW.field_replacements_json) unit
                WHERE json_type(unit.value) <> 'object'
                   OR (SELECT count(*) FROM json_each(unit.value)) <> {len(field_keys)}
                   OR EXISTS (SELECT 1 FROM json_each(unit.value) member
                              WHERE member.key NOT IN ({field_key_sql}))
                   OR {field_text}
                   OR json_extract(unit.value, '$.field') NOT IN ({allowed_sql})
                   OR date(json_extract(unit.value, '$.{date_key}'))
                        IS NOT json_extract(unit.value, '$.{date_key}')
            ) THEN RAISE(ABORT, 'invalid FIELD_REPLACEMENT exact unit') END;
            SELECT CASE WHEN NEW.dataset = '{dataset}' AND EXISTS (
                SELECT 1 FROM json_each(NEW.fallback_only_keys_json) unit
                WHERE json_type(unit.value) <> 'object'
                   OR (SELECT count(*) FROM json_each(unit.value)) <> {len(primary_key)}
                   OR EXISTS (SELECT 1 FROM json_each(unit.value) member
                              WHERE member.key NOT IN ({row_key_sql}))
                   OR {row_text}
                   OR date(json_extract(unit.value, '$.{date_key}'))
                        IS NOT json_extract(unit.value, '$.{date_key}')
            ) THEN RAISE(ABORT, 'invalid ROW_INSERT exact unit') END;
            SELECT CASE WHEN NEW.dataset = '{dataset}' AND
                (SELECT count(*) FROM json_each(NEW.field_replacements_json)) <>
                (SELECT count(*) FROM (SELECT DISTINCT json_array({field_identity})
                                      FROM json_each(NEW.field_replacements_json)))
                THEN RAISE(ABORT, 'duplicate FIELD_REPLACEMENT exact unit') END;
            SELECT CASE WHEN NEW.dataset = '{dataset}' AND
                (SELECT count(*) FROM json_each(NEW.fallback_only_keys_json)) <>
                (SELECT count(*) FROM (SELECT DISTINCT json_array({row_identity})
                                      FROM json_each(NEW.fallback_only_keys_json)))
                THEN RAISE(ABORT, 'duplicate ROW_INSERT exact unit') END;
            SELECT CASE WHEN NEW.dataset = '{dataset}' AND NEW.scope_codes_json IS NOT
                (SELECT json_group_array(value) FROM
                    (SELECT value FROM ({code_projection}) ORDER BY value))
                THEN RAISE(ABORT, 'scope_codes_json disagrees with exact scope') END;
            SELECT CASE WHEN NEW.dataset = '{dataset}' AND NEW.scope_dates_json IS NOT
                (SELECT json_group_array(value) FROM
                    (SELECT value FROM ({date_projection}) ORDER BY value))
                THEN RAISE(ABORT, 'scope_dates_json disagrees with exact scope') END;
            SELECT CASE WHEN NEW.dataset = '{dataset}' AND NEW.fields_json IS NOT
                (SELECT json_group_array(value) FROM
                    (SELECT value FROM ({field_projection}) ORDER BY value))
                THEN RAISE(ABORT, 'fields_json disagrees with exact scope') END;
            """
        )
    return "\n".join(statements)


def _create_precise_source_triggers() -> None:
    op.execute(
        f"""
        CREATE TRIGGER source_overrides_validate_insert
        BEFORE INSERT ON source_overrides
        BEGIN
            {_lineage_insert_guard()}
            SELECT CASE WHEN NOT json_valid(NEW.field_replacements_json)
                              OR NOT json_valid(NEW.fallback_only_keys_json)
                              OR NOT json_valid(NEW.scope_codes_json)
                              OR NOT json_valid(NEW.scope_dates_json)
                              OR NOT json_valid(NEW.fields_json)
                THEN RAISE(ABORT, 'source override scope JSON is invalid') END;
            SELECT CASE WHEN json_type(NEW.field_replacements_json) <> 'array'
                              OR json_type(NEW.fallback_only_keys_json) <> 'array'
                              OR json_type(NEW.scope_codes_json) <> 'array'
                              OR json_type(NEW.scope_dates_json) <> 'array'
                              OR json_type(NEW.fields_json) <> 'array'
                THEN RAISE(ABORT, 'source override scope JSON must use arrays') END;
            SELECT CASE WHEN json_array_length(NEW.field_replacements_json)
                              + json_array_length(NEW.fallback_only_keys_json) = 0
                THEN RAISE(ABORT, 'PENDING override requires exact difference units') END;
            SELECT CASE WHEN length(NEW.exact_scope_sha256) <> 64
                              OR NEW.exact_scope_sha256 GLOB '*[^0-9a-f]*'
                THEN RAISE(ABORT, 'exact scope SHA-256 is invalid') END;
            SELECT CASE WHEN length(NEW.contribution_scope_sha256) <> 64
                              OR NEW.contribution_scope_sha256 GLOB '*[^0-9a-f]*'
                THEN RAISE(ABORT, 'contribution scope SHA-256 is invalid') END;
            {_shape_checks()}
        END
        """
    )
    immutable = """
        NEW.override_id IS NOT OLD.override_id OR NEW.dataset IS NOT OLD.dataset
        OR NEW.difference_report_id IS NOT OLD.difference_report_id
        OR NEW.primary_batch_id IS NOT OLD.primary_batch_id
        OR NEW.fallback_batch_id IS NOT OLD.fallback_batch_id
        OR NEW.primary_source IS NOT OLD.primary_source
        OR NEW.fallback_source IS NOT OLD.fallback_source
        OR NEW.scope_codes_json IS NOT OLD.scope_codes_json
        OR NEW.scope_dates_json IS NOT OLD.scope_dates_json
        OR NEW.fields_json IS NOT OLD.fields_json
        OR NEW.field_replacements_json IS NOT OLD.field_replacements_json
        OR NEW.fallback_only_keys_json IS NOT OLD.fallback_only_keys_json
        OR NEW.exact_scope_sha256 IS NOT OLD.exact_scope_sha256
        OR NEW.contribution_scope_sha256 IS NOT OLD.contribution_scope_sha256
        OR NEW.reason IS NOT OLD.reason OR NEW.created_at IS NOT OLD.created_at
    """
    op.execute(
        f"""
        CREATE TRIGGER source_overrides_pending_transition
        BEFORE UPDATE ON source_overrides
        WHEN OLD.status = 'PENDING' AND (
            NEW.status NOT IN ('APPROVED','REJECTED') OR {immutable}
            OR NEW.approved_at IS NULL OR NEW.approved_by IS NULL
            OR NEW.applied_at IS NOT NULL OR NEW.applied_version_id IS NOT NULL
        ) BEGIN SELECT RAISE(ABORT, 'invalid source override decision transition'); END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER source_overrides_approved_transition
        BEFORE UPDATE ON source_overrides
        WHEN OLD.status = 'APPROVED' AND (
            NEW.status <> 'APPLIED' OR {immutable}
            OR NEW.approved_at IS NOT OLD.approved_at
            OR NEW.approved_by IS NOT OLD.approved_by
            OR NEW.applied_at IS NULL OR NEW.applied_at < OLD.approved_at
            OR NEW.applied_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM dataset_versions v
                JOIN difference_reports d ON d.report_id = OLD.difference_report_id
                JOIN source_override_applications a
                  ON a.override_id = OLD.override_id
                 AND a.child_version_id = NEW.applied_version_id
                WHERE v.version_id = NEW.applied_version_id
                  AND v.dataset = OLD.dataset AND v.status = 'PUBLISHED'
                  AND v.version_id <> d.primary_version_id
                  AND v.parent_version_id = d.primary_version_id
                  AND v.created_at >= OLD.approved_at
                  AND a.status = 'PUBLISHED'
                  AND a.report_sha256 = d.report_sha256
                  AND a.contribution_scope_sha256 = OLD.contribution_scope_sha256
            )
        ) BEGIN
            SELECT RAISE(ABORT, 'approved override requires its exact published application');
        END
        """
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_update_terminal BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status IN ('REJECTED','APPLIED') "
        "BEGIN SELECT RAISE(ABORT, 'terminal source overrides are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_delete_decided BEFORE DELETE ON source_overrides "
        "WHEN OLD.status IN ('APPROVED','REJECTED','APPLIED') "
        "BEGIN SELECT RAISE(ABORT, 'decided source overrides are immutable'); END"
    )
    op.execute(
        """
        CREATE TRIGGER source_overrides_require_published_application
        BEFORE UPDATE ON source_overrides
        WHEN NEW.status = 'APPLIED' AND OLD.status = 'APPROVED'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM source_override_applications a
                JOIN difference_reports d ON d.report_id = OLD.difference_report_id
                WHERE a.override_id = OLD.override_id
                  AND a.child_version_id = NEW.applied_version_id
                  AND a.status = 'PUBLISHED'
                  AND a.report_sha256 = d.report_sha256
                  AND a.contribution_scope_sha256 = OLD.contribution_scope_sha256
            ) THEN RAISE(ABORT, 'APPLIED override requires exact PUBLISHED application') END;
        END
        """
    )


def _replace_application_publish_trigger(*, exact: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS source_override_applications_validate_update")
    scope_check = (
        "AND NEW.contribution_scope_sha256 = o.contribution_scope_sha256"
        if exact
        else "AND length(NEW.contribution_scope_sha256) = 64"
    )
    op.execute(
        f"""
        CREATE TRIGGER source_override_applications_validate_update
        BEFORE UPDATE ON source_override_applications
        WHEN OLD.status = 'CLAIMED'
        BEGIN
            SELECT CASE WHEN NEW.override_id != OLD.override_id
                              OR NEW.child_version_id != OLD.child_version_id
                              OR NEW.input_fingerprint != OLD.input_fingerprint
                              OR NEW.claimed_by != OLD.claimed_by
                              OR NEW.claimed_at != OLD.claimed_at
                THEN RAISE(ABORT, 'override application identity is immutable') END;
            SELECT CASE WHEN NEW.status NOT IN ('FAILED','PUBLISHED')
                THEN RAISE(ABORT, 'invalid override application transition') END;
            SELECT CASE WHEN NEW.status = 'PUBLISHED' AND NOT EXISTS (
                SELECT 1 FROM dataset_versions v
                JOIN source_overrides o ON o.override_id = NEW.override_id
                JOIN difference_reports r ON r.report_id = o.difference_report_id
                WHERE v.version_id = NEW.child_version_id
                  AND v.status = 'PUBLISHED'
                  AND NEW.report_sha256 = r.report_sha256
                  {scope_check}
                  AND NEW.failure_message IS NULL
            ) THEN RAISE(ABORT, 'published override application credentials are invalid') END;
            SELECT CASE WHEN NEW.status = 'FAILED'
                              AND (NEW.report_sha256 IS NOT NULL
                                   OR NEW.contribution_scope_sha256 IS NOT NULL
                                   OR NEW.failure_message IS NULL
                                   OR NOT EXISTS (
                                       SELECT 1 FROM dataset_versions v
                                       WHERE v.version_id = NEW.child_version_id
                                         AND v.status = 'FAILED'
                                   ))
                THEN RAISE(ABORT, 'failed override application facts are invalid') END;
        END
        """
    )


def _create_0008_source_triggers() -> None:
    # The 0008 contract is restored on downgrade; exact JSON columns exist but
    # are not decision facts until 0009.
    op.execute(
        "CREATE TRIGGER source_overrides_validate_insert BEFORE INSERT ON source_overrides "
        "WHEN NEW.status <> 'PENDING' OR NEW.primary_batch_id=NEW.fallback_batch_id OR NOT EXISTS "
        "(SELECT 1 FROM ingestion_batches p, ingestion_batches f, difference_reports d, "
        "dataset_versions v WHERE p.batch_id=NEW.primary_batch_id "
        "AND p.source=NEW.primary_source AND p.dataset=NEW.dataset "
        "AND f.batch_id=NEW.fallback_batch_id AND f.source=NEW.fallback_source "
        "AND f.dataset=NEW.dataset AND d.report_id=NEW.difference_report_id "
        "AND d.primary_source=NEW.primary_source AND d.fallback_source=NEW.fallback_source "
        "AND d.primary_batch_id=NEW.primary_batch_id AND d.fallback_batch_id=NEW.fallback_batch_id "
        "AND v.version_id=d.version_id AND v.dataset=NEW.dataset) "
        "BEGIN SELECT RAISE(ABORT, 'source override lineage is inconsistent'); END"
    )
    immutable = (
        "NEW.override_id IS NOT OLD.override_id OR NEW.dataset IS NOT OLD.dataset "
        "OR NEW.difference_report_id IS NOT OLD.difference_report_id "
        "OR NEW.primary_batch_id IS NOT OLD.primary_batch_id "
        "OR NEW.fallback_batch_id IS NOT OLD.fallback_batch_id "
        "OR NEW.primary_source IS NOT OLD.primary_source "
        "OR NEW.fallback_source IS NOT OLD.fallback_source "
        "OR NEW.scope_codes_json IS NOT OLD.scope_codes_json "
        "OR NEW.scope_dates_json IS NOT OLD.scope_dates_json "
        "OR NEW.fields_json IS NOT OLD.fields_json OR NEW.reason IS NOT OLD.reason "
        "OR NEW.created_at IS NOT OLD.created_at"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_pending_transition BEFORE UPDATE ON source_overrides "
        f"WHEN OLD.status='PENDING' AND (NEW.status NOT IN ('APPROVED','REJECTED') OR {immutable} "
        "OR NEW.approved_at IS NULL OR NEW.approved_by IS NULL OR NEW.applied_at IS NOT NULL "
        "OR NEW.applied_version_id IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT, 'invalid source override decision transition'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_approved_transition BEFORE UPDATE ON source_overrides "
        f"WHEN OLD.status='APPROVED' AND (NEW.status <> 'APPLIED' OR {immutable} "
        "OR NEW.approved_at IS NOT OLD.approved_at OR NEW.approved_by IS NOT OLD.approved_by "
        "OR NEW.applied_at IS NULL OR NEW.applied_version_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM dataset_versions v JOIN difference_reports d "
        "ON d.report_id=OLD.difference_report_id WHERE v.version_id=NEW.applied_version_id "
        "AND v.dataset=OLD.dataset AND v.status='PUBLISHED' AND v.version_id<>d.version_id "
        "AND v.parent_version_id=d.primary_version_id AND v.created_at>=OLD.approved_at)) "
        "BEGIN SELECT RAISE(ABORT, 'approved override requires a published direct child'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_update_terminal BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status IN ('REJECTED','APPLIED') "
        "BEGIN SELECT RAISE(ABORT, 'terminal source overrides are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_delete_decided BEFORE DELETE ON source_overrides "
        "WHEN OLD.status IN ('APPROVED','REJECTED','APPLIED') "
        "BEGIN SELECT RAISE(ABORT, 'decided source overrides are immutable'); END"
    )
    op.execute(
        """
        CREATE TRIGGER source_overrides_require_published_application
        BEFORE UPDATE ON source_overrides
        WHEN NEW.status = 'APPLIED' AND OLD.status = 'APPROVED'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM source_override_applications a
                WHERE a.override_id = OLD.override_id
                  AND a.child_version_id = NEW.applied_version_id
                  AND a.status = 'PUBLISHED'
                  AND length(a.report_sha256) = 64
                  AND length(a.contribution_scope_sha256) = 64
            ) THEN RAISE(ABORT, 'APPLIED override requires PUBLISHED application') END;
        END
        """
    )


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT override_id,dataset,status,scope_codes_json,scope_dates_json,fields_json,"
            "field_replacements_json,fallback_only_keys_json FROM source_overrides"
        )
    ).fetchall()
    scope_hashes = [(str(row.override_id), *_canonical_existing_scope(row)) for row in rows]
    _drop_source_triggers()
    op.add_column(
        "source_overrides",
        sa.Column(
            "exact_scope_sha256",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "source_overrides",
        sa.Column(
            "contribution_scope_sha256",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    for override_id, scope_sha, contribution_sha in scope_hashes:
        connection.execute(
            sa.text(
                "UPDATE source_overrides SET exact_scope_sha256=:scope_sha, "
                "contribution_scope_sha256=:contribution_sha "
                "WHERE override_id=:override_id"
            ),
            {
                "scope_sha": scope_sha,
                "contribution_sha": contribution_sha,
                "override_id": override_id,
            },
        )
    _create_precise_source_triggers()
    _replace_application_publish_trigger(exact=True)


def downgrade() -> None:
    _drop_source_triggers()
    _replace_application_publish_trigger(exact=False)
    op.execute("ALTER TABLE source_overrides DROP COLUMN contribution_scope_sha256")
    op.execute("ALTER TABLE source_overrides DROP COLUMN exact_scope_sha256")
    _create_0008_source_triggers()
