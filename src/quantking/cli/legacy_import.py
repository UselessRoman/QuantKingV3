"""Offline-safe CLI transport for the S1-07K3C legacy DATA_ONLY import phases.

Only local modules are imported; the retired XTQuant runtime is rejected up
front, and the real marker is required before any authorized source-root
read or staging-root write. Dry-run never touches either root.
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
    K3C_REAL_AUTHORIZATION_MARKER,
    K3C_SYNTHETIC_AUTHORIZATION_MARKER,
    LegacyImportAuthorization,
    LegacyImportDryRun,
    LegacyImportMode,
    canonical_json_bytes,
    inventory_from_payload,
    semantics_from_payload,
)

_MARKERS = {
    "synthetic": K3C_SYNTHETIC_AUTHORIZATION_MARKER,
    "real": K3C_REAL_AUTHORIZATION_MARKER,
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
    imp = commands.add_parser("import")
    imp.add_argument("--source-root", required=True)
    imp.add_argument("--staging-root", required=True)
    imp.add_argument("--marker", required=True, choices=("synthetic", "real"))
    imp.add_argument("--inventory-file", required=True)
    imp.add_argument("--semantics-file", required=True)
    imp.add_argument("--idempotency-key", required=True)
    imp.add_argument("--mode", default="PILOT", choices=("PILOT", "FULL"))
    imp.add_argument("--dry-run", action="store_true")
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


def _authorization(arguments: argparse.Namespace) -> LegacyImportAuthorization:
    marker = _MARKERS[str(arguments.marker)]
    return LegacyImportAuthorization(
        source_root=_root(str(arguments.source_root)),
        staging_root=_root(str(arguments.staging_root)),
        marker=marker,
    )


def _load_json_file(path: str) -> dict[str, object]:
    raw = Path(path).read_bytes()
    try:
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("legacy input file is invalid") from error
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
        raise ValueError("legacy input file is not canonical JSON")
    return payload


def _run_inventory(arguments: argparse.Namespace) -> dict[str, object]:
    authorization = _authorization(arguments)
    planner = LegacyInventoryPlanner(authorization)
    paths = planner.discover()
    inventory = planner.inventory(paths)
    return inventory.to_payload()


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


def _run_import(arguments: argparse.Namespace) -> dict[str, object]:
    inventory = inventory_from_payload(_load_json_file(str(arguments.inventory_file)))
    semantics = semantics_from_payload(_load_json_file(str(arguments.semantics_file)))
    if not semantics.confirmed:
        raise ValueError("legacy import semantics are not confirmed")
    authorization = _authorization(arguments)
    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=inventory,
        semantics=semantics,
        idempotency_key=str(arguments.idempotency_key),
    )
    return {
        "schema_version": "legacy-import-command.k3c.v1",
        "batch_id": outcome.batch_id,
        "manifest_sha256": outcome.manifest_sha256,
        "created": outcome.created,
        "reused": outcome.reused,
        "raw_file_count": outcome.raw_file_count,
        "canonical_row_count": outcome.canonical_row_count,
        "warehouse_published": outcome.warehouse_published,
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
