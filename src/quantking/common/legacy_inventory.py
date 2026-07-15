# ruff: noqa: RUF001
"""Read-only, content-safe inventory support for the Stage 0 legacy audit.

The scanner walks the native filesystem instead of consulting Git, never follows
links or Windows reparse points, and only reads small, non-sensitive source files
to calculate an optional checksum.  It does not import, execute, copy, or format
anything from the source tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_MAX_CODE_HASH_BYTES = 1_048_576
_FINGERPRINT_ALGORITHM = (
    "sha256(relative_path\\0kind\\0size\\0mtime_ns\\n; directory size/mtime normalized to 0)"
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class EntryKind(StrEnum):
    """A filesystem entry kind observed without following links."""

    FILE = "file"
    DIRECTORY = "directory"
    LINK = "link_or_reparse_point"
    SPECIAL = "special"
    UNREADABLE = "unreadable"


class AssetType(StrEnum):
    """Stable coverage categories required by the S0-06 report."""

    CODE = "code"
    CONFIG = "config"
    DOCUMENT = "document"
    DATABASE = "database"
    DATA = "data"
    MODEL = "model"
    LOG = "log"
    FAILURE_BACKUP = "failure_or_backup"
    BINARY = "binary"
    OTHER = "other"


class Classification(StrEnum):
    """Legacy candidate dispositions defined by ADR-017."""

    REUSE = "REUSE"
    ADAPT = "ADAPT"
    REWRITE = "REWRITE"
    DISCARD = "DISCARD"
    DATA_ONLY = "DATA_ONLY"


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """Metadata for one immediate child returned by a filesystem adapter."""

    path: Path
    name: str
    kind: EntryKind
    size: int
    mtime_ns: int
    error_reason: str | None = None


class FileSystemPort(Protocol):
    """Minimal replaceable boundary used to test unsafe filesystem cases."""

    def list_directory(self, directory: Path) -> tuple[FileMetadata, ...]:
        """List immediate children without following links."""

    def read_bytes(self, path: Path, maximum_bytes: int) -> bytes:
        """Read at most ``maximum_bytes + 1`` bytes for an optional code hash."""


class LocalFileSystem:
    """Native read-only filesystem adapter."""

    def list_directory(self, directory: Path) -> tuple[FileMetadata, ...]:
        entries: list[FileMetadata] = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                path = Path(entry.path)
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except PermissionError:
                    entries.append(
                        FileMetadata(
                            path=path,
                            name=entry.name,
                            kind=EntryKind.UNREADABLE,
                            size=0,
                            mtime_ns=0,
                            error_reason="permission_denied",
                        )
                    )
                    continue
                except OSError:
                    entries.append(
                        FileMetadata(
                            path=path,
                            name=entry.name,
                            kind=EntryKind.UNREADABLE,
                            size=0,
                            mtime_ns=0,
                            error_reason="stat_failed",
                        )
                    )
                    continue

                attributes = getattr(metadata, "st_file_attributes", 0)
                if entry.is_symlink() or attributes & _REPARSE_POINT:
                    kind = EntryKind.LINK
                elif stat.S_ISDIR(metadata.st_mode):
                    kind = EntryKind.DIRECTORY
                elif stat.S_ISREG(metadata.st_mode):
                    kind = EntryKind.FILE
                else:
                    kind = EntryKind.SPECIAL
                entries.append(
                    FileMetadata(
                        path=path,
                        name=entry.name,
                        kind=kind,
                        size=metadata.st_size,
                        mtime_ns=metadata.st_mtime_ns,
                    )
                )
        return tuple(sorted(entries, key=lambda item: (item.name.casefold(), item.name)))

    def read_bytes(self, path: Path, maximum_bytes: int) -> bytes:
        with path.open("rb") as stream:
            return stream.read(maximum_bytes + 1)


@dataclass(frozen=True, slots=True)
class SkippedItem:
    """A source entry that could not or must not be traversed."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    """Stable summary of path, kind, size, and modification-time metadata."""

    algorithm: str
    digest_sha256: str
    entry_count: int
    file_count: int
    total_bytes: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class InventoryItem:
    """One candidate file or homogeneous collection in the review report."""

    path_pattern: str
    asset_type: AssetType
    count: int
    total_bytes: int
    grouped: bool
    sensitive: bool
    checksum_sha256: str | None
    classification: Classification
    rationale: str
    dependencies: str
    global_state: str
    external_side_effects: str
    semantic_risks: tuple[str, ...]
    existing_tests: str
    missing_tests: str
    target_stage: str
    target_module: str
    disposition: str
    migrated: bool = False


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Conserved counts for all files represented by inventory items."""

    file_count: int
    total_bytes: int
    item_count: int
    skipped_count: int
    by_asset_type: dict[str, int]
    bytes_by_asset_type: dict[str, int]
    by_classification: dict[str, int]


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Deterministic, source-relative S0-06 audit evidence."""

    schema_version: int
    stage: str
    source_label: str
    declaration: str
    grouping_policy: str
    checksum_policy: str
    code_hashing_enabled: bool
    hashed_file_count: int
    before_fingerprint: SourceFingerprint
    after_fingerprint: SourceFingerprint
    source_unchanged: bool
    summary: CoverageSummary
    items: tuple[InventoryItem, ...]
    skipped: tuple[SkippedItem, ...]


@dataclass(frozen=True, slots=True)
class _ObservedFile:
    relative_path: Path
    absolute_path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _ScanResult:
    files: tuple[_ObservedFile, ...]
    skipped: tuple[SkippedItem, ...]
    fingerprint: SourceFingerprint


@dataclass(frozen=True, slots=True)
class _Policy:
    asset_type: AssetType
    classification: Classification
    sensitive: bool
    rationale: str
    dependencies: str
    global_state: str
    external_side_effects: str
    semantic_risks: tuple[str, ...]
    existing_tests: str
    missing_tests: str
    target_stage: str
    target_module: str
    disposition: str


_CODE_SUFFIXES = frozenset(
    {
        ".bat",
        ".c",
        ".cmd",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".ps1",
        ".py",
        ".pyi",
        ".r",
        ".rs",
        ".sh",
        ".sql",
        ".ts",
        ".tsx",
        ".vue",
    }
)
_CONFIG_SUFFIXES = frozenset(
    {".cfg", ".conf", ".env", ".ini", ".properties", ".toml", ".yaml", ".yml"}
)
_DOCUMENT_SUFFIXES = frozenset({".adoc", ".md", ".pdf", ".rst"})
_DATABASE_SUFFIXES = frozenset({".db", ".duckdb", ".mdb", ".sqlite", ".sqlite3"})
_DATA_SUFFIXES = frozenset(
    {
        ".arrow",
        ".csv",
        ".feather",
        ".h5",
        ".hdf5",
        ".json",
        ".npy",
        ".npz",
        ".orc",
        ".parquet",
        ".tsv",
        ".xls",
        ".xlsb",
        ".xlsx",
        ".xml",
    }
)
_MODEL_SUFFIXES = frozenset(
    {".cbm", ".ckpt", ".joblib", ".model", ".onnx", ".pickle", ".pkl", ".pt", ".pth"}
)
_BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".bin",
        ".dll",
        ".dylib",
        ".exe",
        ".gz",
        ".jpg",
        ".jpeg",
        ".pyd",
        ".png",
        ".so",
        ".tar",
        ".whl",
        ".zip",
    }
)
_SENSITIVE_TOKENS = frozenset(
    {"account", "auth", "credential", "key", "password", "private", "secret", "token"}
)
_DATA_DIRECTORY_TOKENS = frozenset(
    {"bars", "data", "dataset", "datasets", "kline", "market", "raw", "warehouse"}
)
_MODEL_DIRECTORY_TOKENS = frozenset(
    {"artifact", "artifacts", "checkpoint", "checkpoints", "model", "models", "trained"}
)
_FAILURE_BACKUP_TOKENS = frozenset(
    {"backup", "backups", "failed", "failure", "failures", "snapshot", "snapshots"}
)
_LOG_TOKENS = frozenset({"log", "logs"})
_GENERATED_TOKENS = frozenset(
    {
        ".coverage",
        ".git",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "site-packages",
    }
)


def _normalized_error(error: OSError) -> str:
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, FileNotFoundError):
        return "entry_disappeared"
    return "directory_read_failed"


def _redact_component(component: str) -> str:
    redacted = re.sub(r"(?<!\d)\d{6,}(?!\d)", "[redacted-number]", component)
    return re.sub(r"(?i)(?<![0-9a-f])[0-9a-f]{24,}(?![0-9a-f])", "[redacted-id]", redacted)


def _safe_path(relative_path: Path) -> str:
    return "/".join(_redact_component(part) for part in relative_path.parts)


def _safe_relative(child: Path, root: Path) -> Path | None:
    try:
        return child.relative_to(root)
    except ValueError:
        return None


def _fingerprint(
    rows: list[tuple[str, EntryKind, int, int]],
    skipped: list[SkippedItem],
) -> SourceFingerprint:
    digest = hashlib.sha256()
    for path, kind, size, mtime_ns in sorted(rows, key=lambda row: (row[0].casefold(), row[0])):
        digest.update(f"{path}\0{kind.value}\0{size}\0{mtime_ns}\n".encode())
    for item in sorted(
        skipped, key=lambda value: (value.path.casefold(), value.path, value.reason)
    ):
        digest.update(f"SKIPPED\0{item.path}\0{item.reason}\n".encode())
    files = [row for row in rows if row[1] is EntryKind.FILE]
    return SourceFingerprint(
        algorithm=_FINGERPRINT_ALGORITHM,
        digest_sha256=digest.hexdigest(),
        entry_count=len(rows),
        file_count=len(files),
        total_bytes=sum(row[2] for row in files),
        skipped_count=len(skipped),
    )


def _scan_tree(root: Path, filesystem: FileSystemPort) -> _ScanResult:
    pending = [root]
    observed_files: list[_ObservedFile] = []
    skipped: list[SkippedItem] = []
    fingerprint_rows: list[tuple[str, EntryKind, int, int]] = []

    while pending:
        directory = pending.pop()
        relative_directory = _safe_relative(directory, root)
        if relative_directory is None:
            skipped.append(SkippedItem("[outside-root]", "path_outside_source_root"))
            continue
        try:
            entries = filesystem.list_directory(directory)
        except OSError as error:
            display = _safe_path(relative_directory) or "."
            skipped.append(SkippedItem(display, _normalized_error(error)))
            continue

        child_directories: list[Path] = []
        for entry in entries:
            relative = _safe_relative(entry.path, root)
            if relative is None or entry.path.parent != directory:
                skipped.append(SkippedItem("[outside-root]", "path_outside_source_root"))
                continue
            raw_display = relative.as_posix()
            safe_display = _safe_path(relative)
            fingerprint_size = 0 if entry.kind is EntryKind.DIRECTORY else max(entry.size, 0)
            fingerprint_mtime = 0 if entry.kind is EntryKind.DIRECTORY else max(entry.mtime_ns, 0)
            fingerprint_rows.append((raw_display, entry.kind, fingerprint_size, fingerprint_mtime))
            if entry.kind is EntryKind.DIRECTORY:
                child_directories.append(entry.path)
            elif entry.kind is EntryKind.FILE:
                observed_files.append(
                    _ObservedFile(relative, entry.path, max(entry.size, 0), max(entry.mtime_ns, 0))
                )
            elif entry.kind is EntryKind.LINK:
                skipped.append(SkippedItem(safe_display, "link_not_followed"))
            elif entry.kind is EntryKind.UNREADABLE:
                skipped.append(SkippedItem(safe_display, entry.error_reason or "stat_failed"))
            else:
                skipped.append(SkippedItem(safe_display, "special_file_not_read"))

        pending.extend(
            reversed(sorted(child_directories, key=lambda path: (path.name.casefold(), path.name)))
        )

    ordered_files = tuple(
        sorted(
            observed_files,
            key=lambda item: (
                item.relative_path.as_posix().casefold(),
                item.relative_path.as_posix(),
            ),
        )
    )
    ordered_skipped = tuple(
        sorted(skipped, key=lambda item: (item.path.casefold(), item.path, item.reason))
    )
    return _ScanResult(
        files=ordered_files,
        skipped=ordered_skipped,
        fingerprint=_fingerprint(fingerprint_rows, skipped),
    )


def _tokens(path: Path) -> frozenset[str]:
    return frozenset(part.casefold() for part in path.parts)


def _contains_token(tokens: frozenset[str], candidates: frozenset[str]) -> bool:
    return bool(tokens & candidates)


def _is_sensitive(path: Path) -> bool:
    name = path.name.casefold()
    if name == ".env" or name.startswith(".env."):
        return True
    return any(token in name for token in _SENSITIVE_TOKENS)


def _asset_type(path: Path) -> AssetType:
    tokens = _tokens(path)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if _contains_token(tokens, _GENERATED_TOKENS) or suffix in {".pyc", ".pyo"}:
        return AssetType.OTHER
    if suffix in _DATABASE_SUFFIXES:
        return AssetType.DATABASE
    if (
        name == ".env"
        or name.startswith(".env.")
        or suffix in _CONFIG_SUFFIXES
        or name
        in {
            ".editorconfig",
            ".gitattributes",
            ".gitignore",
            "dockerfile",
            "makefile",
            "package-lock.json",
            "package.json",
            "pyproject.toml",
            "requirements.txt",
        }
    ):
        return AssetType.CONFIG
    if suffix in _MODEL_SUFFIXES:
        return AssetType.MODEL
    if suffix in {".log", ".jsonl"}:
        return AssetType.LOG
    if suffix in _CODE_SUFFIXES:
        return AssetType.CODE
    if (
        _contains_token(tokens, frozenset({"doc", "docs", "documentation"}))
        or suffix in _DOCUMENT_SUFFIXES
        or name.startswith("readme")
    ):
        return AssetType.DOCUMENT
    if suffix in _BINARY_SUFFIXES:
        return AssetType.BINARY
    if suffix in _DATA_SUFFIXES:
        return AssetType.DATA
    if _contains_token(tokens, _FAILURE_BACKUP_TOKENS) or any(
        token in name for token in ("failed", "failure", "snapshot", "backup")
    ):
        return AssetType.FAILURE_BACKUP
    if _contains_token(tokens, _MODEL_DIRECTORY_TOKENS):
        return AssetType.MODEL
    if _contains_token(tokens, _LOG_TOKENS):
        return AssetType.LOG
    if _contains_token(tokens, _DATA_DIRECTORY_TOKENS):
        return AssetType.DATA
    return AssetType.OTHER


def _target_for(path: Path, asset_type: AssetType) -> tuple[str, str]:
    tokens = _tokens(path)
    if asset_type in {AssetType.DATA, AssetType.DATABASE}:
        return "Stage 1", "quantking.data"
    if asset_type is AssetType.MODEL:
        return "Stage 3", "quantking.research"
    if asset_type in {AssetType.LOG, AssetType.FAILURE_BACKUP, AssetType.BINARY, AssetType.OTHER}:
        return "No migration", "legacy evidence only"
    if tokens & {"factor", "factors", "label", "labels", "qlib", "research", "universe"}:
        return "Stage 2", "quantking.research"
    if tokens & {"experiment", "experiments", "model", "models", "train", "training"}:
        return "Stage 3", "quantking.research"
    if tokens & {"backtest", "backtests"}:
        return "Stage 4", "quantking.backtest"
    if tokens & {"portfolio"}:
        return "Stage 4", "quantking.portfolio"
    if tokens & {"risk"}:
        return "Stage 4", "quantking.risk"
    if tokens & {"account", "accounts", "qmt", "reconcile", "reconciliation", "trade", "trading"}:
        return "Stage 5", "quantking.trading"
    if tokens & {"signal", "signals"}:
        return "Stage 5", "quantking.signals"
    if tokens & {"data", "dataset", "datasets", "kline", "market", "xtquant"}:
        return "Stage 1", "quantking.data"
    if asset_type is AssetType.CONFIG:
        return "Stage 1+", "versioned configuration review"
    if asset_type is AssetType.DOCUMENT:
        return "Stage 1+", "docs for the owning domain"
    return "Stage 1+", "manual domain triage"


def _policy(path: Path, asset_type: AssetType) -> _Policy:
    sensitive = _is_sensitive(path) or asset_type is AssetType.CONFIG
    target_stage, target_module = _target_for(path, asset_type)
    tokens = _tokens(path)
    test_candidate = (
        "test candidate exists but was not executed or trusted"
        if ("test" in tokens or "tests" in tokens or path.name.casefold().startswith("test_"))
        else "not established by this metadata-only audit"
    )

    risks: tuple[str, ...]
    if asset_type in {AssetType.DATA, AssetType.DATABASE}:
        classification = Classification.DATA_ONLY
        rationale = "Data is only a validation candidate; it is neither verified nor migrated."
        dependencies = "Producer, schema, source version, and lineage are not established."
        global_state = (
            "Sibling indexes, manifests, sidecars, or collection completeness may be required."
        )
        external_effects = "None performed; only filesystem metadata was observed."
        risks = (
            "field, unit, adjustment, and trading-time semantics are unverified",
            "point-in-time, survivorship, duplication, and provenance risks remain",
        )
        missing_tests = "Stage 1 schema, coverage, lineage, checksum, and point-in-time validation."
        disposition = (
            "Validate in Stage 1 from an isolated copy before any DATA_ONLY migration decision."
        )
    elif asset_type is AssetType.MODEL:
        classification = Classification.DISCARD
        rationale = (
            "A legacy model binary cannot establish reproducible V3 training or approval lineage."
        )
        dependencies = (
            "Serializer, feature order, library versions, data version, and code commit "
            "are unknown."
        )
        global_state = "Training state and preprocessing artifacts may be implicit or incomplete."
        external_effects = "The artifact was not deserialized or executed."
        risks = (
            "unsafe deserialization and dependency incompatibility",
            "unverifiable feature, label, split, and training-data lineage",
        )
        missing_tests = (
            "Stage 3 reproducible retraining, leakage, metric, manifest, and approval tests."
        )
        disposition = "Do not load; retrain a new Stage 3 model from validated V3 inputs."
    elif asset_type is AssetType.CODE:
        adaptable = bool(tokens & {"script", "scripts", "test", "tests", "tool", "tools"})
        classification = Classification.ADAPT if adaptable else Classification.REWRITE
        rationale = (
            "Tool or test intent may inform a V3 implementation, but its contract is not trusted."
            if adaptable
            else "Legacy business implementation cannot cross V3 boundaries without a "
            "contract-first rewrite."
        )
        dependencies = (
            "Imports, native libraries, and version assumptions were not loaded or executed."
        )
        global_state = (
            "Module globals, caches, mutable singletons, and implicit latest-state access "
            "are unknown."
        )
        external_effects = (
            "Filesystem, network, QMT, process, and mutation behavior was not executed."
        )
        risks = (
            "V3 adapter and dependency-direction violations may exist",
            "time, data lineage, look-ahead, and survivorship semantics are unverified",
        )
        missing_tests = (
            "Target-stage contract, architecture-boundary, regression, and offline "
            "side-effect tests."
        )
        disposition = (
            "Use only as review input; implement and test anew in the declared target stage."
        )
    elif asset_type is AssetType.CONFIG:
        classification = Classification.REWRITE
        rationale = "Configuration values and legacy path semantics must not be copied into V3."
        dependencies = (
            "Keys, consumers, defaults, and environment precedence were intentionally not read."
        )
        global_state = (
            "Machine-specific paths, accounts, or implicit current-state settings may be present."
        )
        external_effects = "The configuration was not parsed, sourced, imported, or applied."
        risks = (
            "secret, account, endpoint, and machine-path exposure",
            "unversioned parameters may change research or execution semantics",
        )
        missing_tests = (
            "Typed schema, secret exclusion, precedence, path-boundary, and versioning tests."
        )
        disposition = (
            "Define a clean V3 schema; manually re-enter only approved non-secret settings."
        )
    elif asset_type is AssetType.DOCUMENT:
        classification = Classification.ADAPT
        rationale = (
            "Documentation is historical audit input and cannot override accepted V3 decisions."
        )
        dependencies = (
            "Referenced code, data, versions, and external services may be stale or absent."
        )
        global_state = "Statements may describe a past machine or runtime state."
        external_effects = "No links, commands, notebooks, or embedded code were executed."
        risks = ("stale requirements or conflict with accepted V3 decisions",)
        missing_tests = "Decision mapping, link review, and target-stage acceptance evidence."
        disposition = "Reconcile useful facts against V3 authorities in the owning target stage."
    else:
        classification = Classification.DISCARD
        rationale = (
            "Generated, operational, binary, failed, backup, or unknown material is not "
            "reusable code."
        )
        dependencies = "Producer, format, version, and completeness are not established."
        global_state = (
            "The artifact may be partial, duplicated, stale, or tied to an old environment."
        )
        external_effects = "The artifact was not opened, loaded, restored, imported, or executed."
        risks = (
            "secret leakage, unsafe loading, staleness, duplication, or incomplete failure state",
        )
        missing_tests = (
            "No migration test; retain legacy evidence in place or regenerate under V3 contracts."
        )
        disposition = (
            "Do not migrate; retain in the untouched legacy source only when evidence is needed."
        )

    return _Policy(
        asset_type=asset_type,
        classification=classification,
        sensitive=sensitive,
        rationale=rationale,
        dependencies=dependencies,
        global_state=global_state,
        external_side_effects=external_effects,
        semantic_risks=risks,
        existing_tests=test_candidate,
        missing_tests=missing_tests,
        target_stage=target_stage,
        target_module=target_module,
        disposition=disposition,
    )


def _group_pattern(path: Path, asset_type: AssetType) -> str:
    directories = path.parts[:-1]
    depth = 3 if asset_type is AssetType.DATA or path.parts[:1] == (".worktrees",) else 2
    anchor_parts = directories[:depth]
    anchor = "/".join(_redact_component(part) for part in anchor_parts)
    suffix = path.suffix.casefold()
    ending = f"*{suffix}" if suffix else "*[no-extension]"
    if not anchor:
        return ending
    has_deeper_directories = len(directories) > depth
    separator = "/**/" if has_deeper_directories else "/"
    return f"{anchor}{separator}{ending}"


def _checksum(
    file: _ObservedFile,
    policy: _Policy,
    filesystem: FileSystemPort,
) -> tuple[str | None, SkippedItem | None]:
    if (
        policy.asset_type is not AssetType.CODE
        or policy.sensitive
        or file.size > _MAX_CODE_HASH_BYTES
    ):
        return None, None
    try:
        payload = filesystem.read_bytes(file.absolute_path, _MAX_CODE_HASH_BYTES)
    except OSError:
        return None, SkippedItem(_safe_path(file.relative_path), "checksum_read_failed")
    if len(payload) > _MAX_CODE_HASH_BYTES:
        return None, None
    return hashlib.sha256(payload).hexdigest(), None


def _items(
    files: tuple[_ObservedFile, ...],
    filesystem: FileSystemPort,
    *,
    hash_small_code: bool,
) -> tuple[tuple[InventoryItem, ...], tuple[SkippedItem, ...]]:
    grouped: dict[tuple[Any, ...], InventoryItem] = {}
    checksum_skips: list[SkippedItem] = []
    grouped_types = {
        AssetType.DATA,
        AssetType.DATABASE,
        AssetType.MODEL,
        AssetType.LOG,
        AssetType.FAILURE_BACKUP,
        AssetType.BINARY,
        AssetType.OTHER,
    }

    for file in files:
        asset_type = _asset_type(file.relative_path)
        policy = _policy(file.relative_path, asset_type)
        is_grouped = asset_type in grouped_types
        display = (
            _group_pattern(file.relative_path, asset_type)
            if is_grouped
            else _safe_path(file.relative_path)
        )
        checksum, checksum_skip = (
            _checksum(file, policy, filesystem) if hash_small_code else (None, None)
        )
        if checksum_skip is not None:
            checksum_skips.append(checksum_skip)
        item = InventoryItem(
            path_pattern=display,
            asset_type=asset_type,
            count=1,
            total_bytes=file.size,
            grouped=is_grouped,
            sensitive=policy.sensitive,
            checksum_sha256=checksum,
            classification=policy.classification,
            rationale=policy.rationale,
            dependencies=policy.dependencies,
            global_state=policy.global_state,
            external_side_effects=policy.external_side_effects,
            semantic_risks=policy.semantic_risks,
            existing_tests=policy.existing_tests,
            missing_tests=policy.missing_tests,
            target_stage=policy.target_stage,
            target_module=policy.target_module,
            disposition=policy.disposition,
        )
        key = (
            display,
            asset_type,
            policy.classification,
            policy.sensitive,
            policy.target_stage,
            policy.target_module,
            policy.existing_tests,
        )
        previous = grouped.get(key)
        if previous is None:
            grouped[key] = item
        else:
            grouped[key] = replace(
                previous,
                count=previous.count + 1,
                total_bytes=previous.total_bytes + file.size,
                checksum_sha256=None,
            )

    ordered = tuple(
        sorted(
            grouped.values(),
            key=lambda item: (
                item.path_pattern.casefold(),
                item.path_pattern,
                item.asset_type.value,
            ),
        )
    )
    return ordered, tuple(
        sorted(
            checksum_skips,
            key=lambda item: (item.path.casefold(), item.path, item.reason),
        )
    )


def _summary(items: tuple[InventoryItem, ...], skipped: tuple[SkippedItem, ...]) -> CoverageSummary:
    by_type = {asset_type.value: 0 for asset_type in AssetType}
    bytes_by_type = {asset_type.value: 0 for asset_type in AssetType}
    by_classification = {classification.value: 0 for classification in Classification}
    for item in items:
        by_type[item.asset_type.value] += item.count
        bytes_by_type[item.asset_type.value] += item.total_bytes
        by_classification[item.classification.value] += item.count
    return CoverageSummary(
        file_count=sum(item.count for item in items),
        total_bytes=sum(item.total_bytes for item in items),
        item_count=len(items),
        skipped_count=len(skipped),
        by_asset_type=by_type,
        bytes_by_asset_type=bytes_by_type,
        by_classification=by_classification,
    )


def _validate_root(root: Path) -> Path:
    absolute = root.absolute()
    try:
        metadata = os.lstat(absolute)
    except OSError as error:
        raise ValueError("source root must be an existing readable directory") from error
    attributes = getattr(metadata, "st_file_attributes", 0)
    if not stat.S_ISDIR(metadata.st_mode) or attributes & _REPARSE_POINT:
        raise ValueError("source root must be a real directory, not a link or reparse point")
    return absolute


def audit_tree(
    source_root: Path,
    *,
    filesystem: FileSystemPort | None = None,
    source_label: str = "legacy-project",
    hash_small_code: bool = False,
) -> AuditReport:
    """Create a deterministic in-memory candidate audit without writing the source.

    The source is traversed before, during, and after inventory construction.  A
    changed fingerprint is reported instead of being silently accepted.
    """

    root = _validate_root(source_root)
    adapter = filesystem or LocalFileSystem()
    before = _scan_tree(root, adapter)
    inventory_scan = _scan_tree(root, adapter)
    items, checksum_skips = _items(
        inventory_scan.files,
        adapter,
        hash_small_code=hash_small_code,
    )
    after = _scan_tree(root, adapter)
    skipped = tuple(
        sorted(
            (*inventory_scan.skipped, *checksum_skips),
            key=lambda item: (item.path.casefold(), item.path, item.reason),
        )
    )
    summary = _summary(items, skipped)
    if summary.file_count != inventory_scan.fingerprint.file_count:
        raise RuntimeError("inventory count does not match filesystem scan")
    if summary.total_bytes != inventory_scan.fingerprint.total_bytes:
        raise RuntimeError("inventory bytes do not match filesystem scan")
    return AuditReport(
        schema_version=1,
        stage="Stage 0 / S0-06",
        source_label=source_label,
        declaration=(
            "Candidate audit only. No file or directory has been migrated. Actual migration is "
            "allowed only in the declared target stage after its validation gates pass."
        ),
        grouping_policy=(
            "Code, configuration, and documents are listed individually. Data, databases, models, "
            "logs, failure/backup artifacts, binaries, and generated/unknown files are grouped by "
            "stable leading directory and suffix."
        ),
        checksum_policy=(
            (
                "SHA-256 opt-in was enabled only for non-sensitive code files no larger than "
                f"{_MAX_CODE_HASH_BYTES} bytes. Configurations, data, databases, models, logs, "
                "and binaries were not content-read."
            )
            if hash_small_code
            else (
                "Content hashing was disabled. No source file content was read; the audit used "
                "filesystem metadata only."
            )
        ),
        code_hashing_enabled=hash_small_code,
        hashed_file_count=sum(item.count for item in items if item.checksum_sha256 is not None),
        before_fingerprint=before.fingerprint,
        after_fingerprint=after.fingerprint,
        source_unchanged=before.fingerprint == after.fingerprint,
        summary=summary,
        items=items,
        skipped=skipped,
    )


def _item_dict(item: InventoryItem) -> dict[str, Any]:
    value = asdict(item)
    value["asset_type"] = item.asset_type.value
    value["classification"] = item.classification.value
    value["semantic_risks"] = list(item.semantic_risks)
    return value


def report_dict(report: AuditReport) -> dict[str, Any]:
    """Return the stable machine-readable representation of a report."""

    return {
        "after_fingerprint": asdict(report.after_fingerprint),
        "before_fingerprint": asdict(report.before_fingerprint),
        "checksum_policy": report.checksum_policy,
        "code_hashing_enabled": report.code_hashing_enabled,
        "declaration": report.declaration,
        "grouping_policy": report.grouping_policy,
        "hashed_file_count": report.hashed_file_count,
        "items": [_item_dict(item) for item in report.items],
        "schema_version": report.schema_version,
        "skipped": [asdict(item) for item in report.skipped],
        "source_label": report.source_label,
        "source_unchanged": report.source_unchanged,
        "stage": report.stage,
        "summary": asdict(report.summary),
    }


def render_json(report: AuditReport) -> str:
    """Render deterministic UTF-8 JSON without absolute source paths or content."""

    return json.dumps(report_dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def render_markdown(report: AuditReport) -> str:
    """Render a reviewer-friendly report with the same conserved totals as JSON."""

    lines = [
        "# Stage 0 S0-06 旧项目只读迁移候选清单",
        "",
        "> 状态：候选审计，未迁移。分类不得覆盖 V3 权威文档；实际迁移只能在目标 Stage 验证后进行。",
        "",
        f"- 来源标签：`{_markdown_cell(report.source_label)}`（报告仅保存相对路径/模式）",
        f"- 前后指纹一致：`{str(report.source_unchanged).lower()}`",
        f"- 文件数：`{report.summary.file_count}`",
        f"- 总字节数：`{report.summary.total_bytes}`",
        f"- 清单项/分组数：`{report.summary.item_count}`",
        f"- 跳过/不可读项：`{report.summary.skipped_count}`",
        f"- 内容哈希启用：`{str(report.code_hashing_enabled).lower()}`",
        f"- 已哈希文件：`{report.hashed_file_count}`",
        "",
        "## 只读指纹",
        "",
        "| 时点 | 算法 | 条目 | 文件 | 字节 | 跳过 | SHA-256 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for label, fingerprint in (
        ("before", report.before_fingerprint),
        ("after", report.after_fingerprint),
    ):
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    label,
                    fingerprint.algorithm,
                    fingerprint.entry_count,
                    fingerprint.file_count,
                    fingerprint.total_bytes,
                    fingerprint.skipped_count,
                    fingerprint.digest_sha256,
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 覆盖汇总",
            "",
            "| 类型 | 文件数 | 字节数 |",
            "|---|---:|---:|",
        ]
    )
    for asset_type in AssetType:
        lines.append(
            f"| {asset_type.value} | {report.summary.by_asset_type[asset_type.value]} | "
            f"{report.summary.bytes_by_asset_type[asset_type.value]} |"
        )
    lines.extend(
        [
            "",
            "| 分类 | 文件数 |",
            "|---|---:|",
        ]
    )
    for classification in Classification:
        lines.append(
            f"| {classification.value} | {report.summary.by_classification[classification.value]} |"
        )

    lines.extend(
        [
            "",
            "## 策略与边界",
            "",
            f"- 分组：{report.grouping_policy}",
            f"- 校验和：{report.checksum_policy}",
            f"- 声明：{report.declaration}",
            "- 遍历：原生文件系统递归；不使用 Git 可见性；不跟随符号链接或 Windows reparse point。",
            "- 内容：无预览字段；配置、数据库、数据、模型、日志、失败/备份和二进制不读取内容。",
            "",
            "## 候选项",
            "",
            "| 路径/模式 | 类型 | 数量 | 字节 | 分类 | 敏感 | 目标 | 依据/风险/测试/处置 |",
            "|---|---|---:|---:|---|---|---|---|",
        ]
    )
    for item in report.items:
        detail = "<br>".join(
            _markdown_cell(value)
            for value in (
                f"依据：{item.rationale}",
                f"依赖：{item.dependencies}",
                f"全局状态：{item.global_state}",
                f"外部副作用：{item.external_side_effects}",
                f"语义风险：{'; '.join(item.semantic_risks)}",
                f"现有测试：{item.existing_tests}",
                f"缺失测试：{item.missing_tests}",
                f"后续：{item.disposition}",
                "迁移：false",
            )
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(f"`{item.path_pattern}`"),
                    item.asset_type.value,
                    str(item.count),
                    str(item.total_bytes),
                    item.classification.value,
                    str(item.sensitive).lower(),
                    _markdown_cell(f"{item.target_stage} / {item.target_module}"),
                    detail,
                )
            )
            + " |"
        )

    lines.extend(["", "## 跳过与不可读项", ""])
    if not report.skipped:
        lines.append("无。")
    else:
        lines.extend(["| 相对路径 | 原因 |", "|---|---|"])
        for skipped_item in report.skipped:
            lines.append(f"| `{_markdown_cell(skipped_item.path)}` | {skipped_item.reason} |")
    lines.append("")
    return "\n".join(lines)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_name = stream.name
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                Path(temporary_name).unlink()


def write_reports(
    report: AuditReport,
    *,
    json_path: Path,
    markdown_path: Path,
    allowed_output_root: Path,
    source_root: Path,
) -> None:
    """Write both reports atomically inside the approved V3 output root only."""

    if not report.source_unchanged:
        raise ValueError("source fingerprint changed during the audit; reports are not publishable")
    allowed = allowed_output_root.resolve(strict=False)
    source = source_root.resolve(strict=False)
    destinations = (json_path.resolve(strict=False), markdown_path.resolve(strict=False))
    if destinations[0] == destinations[1]:
        raise ValueError("JSON and Markdown destinations must be different")
    for destination in destinations:
        if _is_within(destination, source):
            raise ValueError("report destination cannot be inside the source tree")
        if not _is_within(destination, allowed):
            raise ValueError("report destination must be inside the allowed output root")
    _atomic_write(destinations[0], render_json(report))
    _atomic_write(destinations[1], render_markdown(report))


__all__ = [
    "AssetType",
    "AuditReport",
    "Classification",
    "CoverageSummary",
    "EntryKind",
    "FileMetadata",
    "FileSystemPort",
    "InventoryItem",
    "LocalFileSystem",
    "SkippedItem",
    "SourceFingerprint",
    "audit_tree",
    "render_json",
    "render_markdown",
    "report_dict",
    "write_reports",
]
