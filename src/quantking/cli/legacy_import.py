"""Offline-safe CLI transport for the S1-07K4D legacy DATA_ONLY import phases.

Only local modules are imported; the retired XTQuant runtime is rejected up
front, and the real marker is required before any authorized source-root
read or staging-root write. Inventory, pilot and full are three separate
authorizations bound to their own markers; ``inventory --dry-run`` and the
import dry-run never touch either root, while the authorized inventory uses
the fail-closed closed scan.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, TextIO

from quantking.common.errors import IdempotencyConflictError
from quantking.data.adapters.legacy_import import (
    LegacyImportError,
    LegacyInventoryPlanner,
)
from quantking.data.adapters.legacy_managed_import import (
    LegacyImportQuarantined,
    LegacyManagedImporter,
)
from quantking.data.legacy_import.contracts import (
    K4D_FULL_REAL_MARKER,
    K4D_FULL_SYNTHETIC_MARKER,
    K4D_INVENTORY_REAL_MARKER,
    K4D_INVENTORY_SYNTHETIC_MARKER,
    K4D_PILOT_REAL_MARKER,
    K4D_PILOT_SYNTHETIC_MARKER,
    LegacyImportAuthorization,
    LegacyImportDryRun,
    LegacyImportMode,
    LegacyInventoryDryRun,
    canonical_json_bytes,
    inventory_from_payload,
    semantics_from_payload,
    sha256_bytes,
)
from quantking.history.stage1.k4d_pilot import build_k4d_pilot_plan

_MARKERS = {
    (LegacyImportMode.INVENTORY, "synthetic"): K4D_INVENTORY_SYNTHETIC_MARKER,
    (LegacyImportMode.INVENTORY, "real"): K4D_INVENTORY_REAL_MARKER,
    (LegacyImportMode.PILOT, "synthetic"): K4D_PILOT_SYNTHETIC_MARKER,
    (LegacyImportMode.PILOT, "real"): K4D_PILOT_REAL_MARKER,
    (LegacyImportMode.FULL, "synthetic"): K4D_FULL_SYNTHETIC_MARKER,
    (LegacyImportMode.FULL, "real"): K4D_FULL_REAL_MARKER,
}


class _ArgumentError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _ArgumentError


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="quantking-legacy-import", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--source-root", required=True)
    inventory.add_argument("--staging-root", required=True)
    inventory.add_argument("--marker", required=True, choices=("synthetic", "real"))
    inventory.add_argument("--dry-run", action="store_true")
    imp = commands.add_parser("import")
    imp.add_argument("--source-root", required=True)
    imp.add_argument("--staging-root", required=True)
    imp.add_argument("--marker", required=True, choices=("synthetic", "real"))
    imp.add_argument("--inventory-file", required=True)
    imp.add_argument("--semantics-file", required=True)
    imp.add_argument("--idempotency-key", required=True)
    imp.add_argument("--mode", default="PILOT", choices=("PILOT", "FULL"))
    imp.add_argument("--symbols", default=None)
    imp.add_argument("--dry-run", action="store_true")
    pilot = commands.add_parser("pilot")
    pilot.add_argument("--dry-run", action="store_true")
    pilot.add_argument("--inventory-file", required=True)
    pilot.add_argument("--plan-file", required=True)
    pilot.add_argument("--marker", required=True, choices=("synthetic", "real"))
    return parser


def _write(stream: TextIO, payload: object) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise _ArgumentError
    return path


def _symbols(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    symbols = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not symbols or any("." not in item for item in symbols):
        raise _ArgumentError
    return symbols


def _authorization(
    arguments: argparse.Namespace, *, mode: LegacyImportMode
) -> LegacyImportAuthorization:
    marker = _MARKERS[(mode, str(arguments.marker))]
    return LegacyImportAuthorization(
        source_root=_root(str(arguments.source_root)),
        staging_root=_root(str(arguments.staging_root)),
        mode=mode,
        marker=marker,
        pilot_symbols=_symbols(getattr(arguments, "symbols", None)),
    )


def _load_json_file(path: str) -> dict[str, object]:
    raw = Path(path).read_bytes()
    try:
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("legacy input file is invalid") from error
    canonical = canonical_json_bytes(payload)
    # The CLI writes exactly one canonical JSON line (trailing LF included);
    # accept both that form and a bare canonical document so frozen evidence
    # files remain directly consumable by later phases.
    if not isinstance(payload, dict) or raw not in (canonical, canonical + b"\n"):
        raise ValueError("legacy input file is not canonical JSON")
    return payload


def _run_inventory(arguments: argparse.Namespace) -> dict[str, object]:
    if arguments.dry_run:
        dry_run = LegacyInventoryDryRun(
            source_root=_root(str(arguments.source_root)),
            staging_root=_root(str(arguments.staging_root)),
            marker=_MARKERS[(LegacyImportMode.INVENTORY, str(arguments.marker))],
        )
        return dry_run.to_payload()
    authorization = _authorization(arguments, mode=LegacyImportMode.INVENTORY)
    planner = LegacyInventoryPlanner(authorization)
    return planner.closed_scan().to_payload()


def _run_dry_run(arguments: argparse.Namespace) -> dict[str, object]:
    inventory = inventory_from_payload(_load_json_file(str(arguments.inventory_file)))
    semantics = semantics_from_payload(_load_json_file(str(arguments.semantics_file)))
    if not semantics.confirmed:
        raise ValueError("legacy import semantics are not confirmed")
    dry_run = LegacyImportDryRun(
        mode=LegacyImportMode(str(arguments.mode)),
        source_root_label=str(arguments.source_root),
        staging_root_label=str(arguments.staging_root),
        relative_paths=tuple(entry.relative_path for entry in inventory.entries),
        declared_file_count=inventory.file_count,
        declared_bytes=inventory.total_bytes,
        declared_rows=inventory.readable_rows,
    )
    return dict(dry_run.to_payload())


def _run_pilot(arguments: argparse.Namespace) -> dict[str, object]:
    if not arguments.dry_run:
        # The real pilot runs only through the env-guarded authorized
        # executor; the CLI deliberately builds plans and nothing else.
        raise _ArgumentError
    inventory_path = Path(str(arguments.inventory_file))
    raw = inventory_path.read_bytes()
    plan = build_k4d_pilot_plan(
        inventory_payload=_load_json_file(str(arguments.inventory_file)),
        inventory_sha256=sha256_bytes(raw),
        marker=_MARKERS[(LegacyImportMode.PILOT, str(arguments.marker))],
        inventory_relative_path=str(arguments.inventory_file).replace("\\", "/"),
        enforce_expected_totals=True,
    )
    plan_path = Path(str(arguments.plan_file))
    payload_bytes = canonical_json_bytes(plan.payload) + b"\n"
    if plan_path.exists():
        if plan_path.read_bytes() != payload_bytes:
            raise ValueError("pilot plan file exists with different bytes")
    else:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        with plan_path.open("xb") as stream:
            stream.write(payload_bytes)
    return dict(plan.payload)


def _run_import(arguments: argparse.Namespace) -> dict[str, object]:
    inventory = inventory_from_payload(_load_json_file(str(arguments.inventory_file)))
    semantics = semantics_from_payload(_load_json_file(str(arguments.semantics_file)))
    if not semantics.confirmed:
        raise ValueError("legacy import semantics are not confirmed")
    mode = LegacyImportMode(str(arguments.mode))
    authorization = _authorization(arguments, mode=mode)
    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=inventory,
        semantics=semantics,
        idempotency_key=str(arguments.idempotency_key),
        selected_symbols=_symbols(getattr(arguments, "symbols", None)),
    )
    return {
        "schema_version": "legacy-import-command.k4d.v1",
        "batch_id": outcome.batch_id,
        "manifest_sha256": outcome.manifest_sha256,
        "created": outcome.created,
        "reused": outcome.reused,
        "raw_file_count": outcome.raw_file_count,
        "canonical_row_count": outcome.canonical_row_count,
        "warehouse_published": outcome.warehouse_published,
        "deferred_file_count": outcome.deferred_file_count,
        "deferred_byte_count": outcome.deferred_byte_count,
        "resolution_evidence": outcome.evidence.to_payload(),
    }


def run_cli(argv: Sequence[str], *, stdout: TextIO) -> int:
    """Parse one explicit command and emit exactly one stable JSON line."""
    try:
        arguments = _parser().parse_args(list(argv))
    except _ArgumentError:
        _write(
            stdout,
            {
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "CLI arguments are invalid.",
                }
            },
        )
        return 2
    try:
        if arguments.command == "inventory":
            payload = _run_inventory(arguments)
        elif arguments.command == "pilot":
            payload = _run_pilot(arguments)
        elif arguments.command == "import" and arguments.dry_run:
            payload = _run_dry_run(arguments)
        elif arguments.command == "import":
            payload = _run_import(arguments)
        else:
            raise _ArgumentError
    except _ArgumentError:
        _write(
            stdout,
            {
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "CLI arguments are invalid.",
                }
            },
        )
        return 2
    except (ValueError, OSError) as error:
        _write(
            stdout,
            {
                "error": {
                    "code": "LEGACY_INPUT_INVALID",
                    "message": "Legacy import inputs are invalid.",
                },
                "detail": str(error),
            },
        )
        return 1
    except IdempotencyConflictError as error:
        _write(
            stdout,
            {
                "error": {
                    "code": "LEGACY_IDEMPOTENCY_CONFLICT",
                    "message": "Legacy import idempotency was violated.",
                },
                "detail": str(error),
            },
        )
        return 1
    except (LegacyImportError, LegacyImportQuarantined) as error:
        _write(
            stdout,
            {
                "error": {
                    "code": "LEGACY_SOURCE_ERROR",
                    "message": "Legacy source boundary was violated.",
                },
                "detail": str(error),
            },
        )
        return 1
    except Exception:
        _write(
            stdout,
            {
                "error": {
                    "code": "COMMAND_FAILED",
                    "message": "The legacy import command failed.",
                }
            },
        )
        return 1
    _write(stdout, payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installed local CLI without importing any vendor package."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if any(item in {"-h", "--help"} for item in arguments):
        _parser().parse_args(list(arguments))
        return 0
    return run_cli(arguments, stdout=sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_cli"]
