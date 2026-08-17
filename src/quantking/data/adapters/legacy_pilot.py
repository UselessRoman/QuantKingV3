"""Authorized S1-07K4D pilot executor: copy six files once, never publish.

The executor enforces the frozen pilot contract end to end under a one-shot
attempt guard: read exactly the six inventoried legacy files read-only, call
each A/B/C source once per security through an injected fetcher, confirm the
K4D semantics (unit scales + unadjusted boundary) before any staging write,
transform with derived ``pre_close``/suspension rules, recheck every canonical
row under the K4 0/50 thresholds, and atomically publish one immutable pilot
batch. Failures preserve evidence and never become successes; Warehouse is
never touched.

Every fetcher call is journaled to ``invocations.jsonl`` in the attempt
directory (append + flush + fsync per record); the fetcher itself journals one
record per endpoint call. ``SourceAdapterError`` failures are unpacked into
stable, redacted evidence fields (code / network_reason / failure_kind /
retryable) in both the journal and ``failure.json``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.data.adapters.canonical_schema import (
    canonical_schema,
    ensure_sorted,
    validate_table,
)
from quantking.data.adapters.legacy_import import (
    _safe_root,
    _safe_source_file,
)
from quantking.data.adapters.legacy_recheck import LegacyRecheckEngine, LegacyRecheckOutcome
from quantking.data.adapters.sources.contracts import SourceAdapterError
from quantking.data.comparison.thresholds import (
    K4_THRESHOLD_RULE_VERSION,
    ThresholdSet,
    thresholds_for,
)
from quantking.data.content import canonical_content_sha256
from quantking.data.legacy_import.contracts import (
    LegacyImportAuthorization,
    LegacyInventoryEntry,
    canonical_json_bytes,
    inventory_from_payload,
    marker_is_real,
    sha256_bytes,
    validate_idempotency_key,
)
from quantking.data.legacy_import.k4d_pilot import (
    K4D_EXPECTED_CANONICAL_ROW_COUNT,
    K4D_PILOT_SYMBOLS,
    K4DPilotPlan,
    K4DSemanticsConfirmation,
    K4DSemanticsError,
    K4DSemanticsStatus,
    classify_k4d_row,
    confirm_k4d_units,
    k4d_semantics_from_payload,
    parse_k4d_date,
    prove_k4d_unadjusted,
    select_k4d_pilot_entries,
)
from quantking.data.models import DatasetKind, DataUpstream

LEGACY_PILOT_ADAPTER_VERSION = "xtquant-legacy-pilot.k4d.v1"
LEGACY_PILOT_MANIFEST_SCHEMA_VERSION = "legacy-pilot-manifest.k4d.v1"
LEGACY_PILOT_ATTEMPT_SCHEMA_VERSION = "legacy-pilot-attempt.k4d.v1"
LEGACY_PILOT_IDEMPOTENCY_SCHEMA_VERSION = "legacy-pilot-idempotency.k4d.v1"
LEGACY_PILOT_INVOCATION_SCHEMA_VERSION = "legacy-pilot-invocation.k4d.v1"
_JOURNAL = "invocations.jsonl"
_MANIFEST = "manifest.json"
_COMPLETE = "COMPLETE"
_FIELDS = ("open", "high", "low", "close", "volume", "amount")
Key = tuple[str, str]
_read_parquet = cast("Callable[[Path], pa.Table]", pq.read_table)
_write_parquet = cast("Callable[[pa.Table, Path], None]", pq.write_table)


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LegacyPilotError("PILOT_FIELD_TYPE_INVALID")
    return float(value)


def unpack_source_error(error: BaseException) -> dict[str, object] | None:
    """Unpack a ``SourceAdapterError`` into stable, redacted evidence fields.

    The adapter boundary already guarantees that no third-party text leaks,
    so only its stable code / reason / kind / retryability are surfaced here.
    Any other exception returns ``None`` instead of unstable repr content.
    """
    if not isinstance(error, SourceAdapterError):
        return None
    return {
        "code": error.code.value,
        "network_reason": (
            error.network_reason.value if error.network_reason is not None else None
        ),
        "failure_kind": error.failure_kind.value,
        "retryable": error.retryable,
        "message": error.message,
    }


class LegacyPilotError(RuntimeError):
    """Stable-code pilot boundary failure (no absolute paths, no payloads)."""

    def __init__(
        self,
        code: str,
        *,
        symbol: str | None = None,
        source_error: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.symbol = symbol
        self.source_error = dict(source_error) if source_error is not None else None


class PilotInvocationJournal:
    """Append-only, fsync-durable per-call journal (one canonical JSON line)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._sequence = 0

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, payload: Mapping[str, object]) -> None:
        self._sequence += 1
        record: dict[str, object] = {
            "schema_version": LEGACY_PILOT_INVOCATION_SCHEMA_VERSION,
            "sequence": self._sequence,
            **dict(payload),
        }
        line = canonical_json_bytes(record) + b"\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("ab") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise LegacyPilotError("PILOT_JOURNAL_WRITE_FAILED") from error

    def record_symbol_success(self, *, symbol: str, row_counts: Mapping[str, int]) -> None:
        self._append(
            {
                "scope": "SYMBOL",
                "status": "SUCCESS",
                "symbol": symbol,
                "provider": None,
                "upstream": None,
                "source_error": None,
                "row_counts": dict(row_counts),
            }
        )

    def record_symbol_failure(self, *, symbol: str, error: BaseException) -> None:
        self._append(
            {
                "scope": "SYMBOL",
                "status": "FAILED",
                "symbol": symbol,
                "provider": None,
                "upstream": None,
                "source_error": unpack_source_error(error),
            }
        )

    def record_endpoint_success(
        self,
        *,
        symbol: str,
        provider: str,
        upstream: str,
        row_count: int,
        raw_payload_sha256: str,
        library_version: str,
    ) -> None:
        self._append(
            {
                "scope": "ENDPOINT",
                "status": "SUCCESS",
                "symbol": symbol,
                "provider": provider,
                "upstream": upstream,
                "source_error": None,
                "row_count": row_count,
                "raw_payload_sha256": raw_payload_sha256,
                "library_version": library_version,
            }
        )

    def record_endpoint_failure(
        self, *, symbol: str, provider: str, upstream: str, error: BaseException
    ) -> None:
        self._append(
            {
                "scope": "ENDPOINT",
                "status": "FAILED",
                "symbol": symbol,
                "provider": provider,
                "upstream": upstream,
                "source_error": unpack_source_error(error),
            }
        )


@dataclass(frozen=True, slots=True)
class K4DServiceFetchResult:
    """One security's A/B/C canonical tables plus vendor reference pre_close."""

    symbol: str
    canonical: Mapping[DataUpstream, pa.Table]
    vendor_pre_close: Mapping[Key, float]
    evidence: Mapping[str, object]


ServiceFetcher = Callable[[str, "PilotInvocationJournal"], K4DServiceFetchResult]


@dataclass(frozen=True, slots=True)
class K4DPilotTransform:
    """Pure transform outcome for one pilot file set."""

    canonical: pa.Table
    x_rows: dict[Key, dict[str, object]]
    raw_row_count: int
    first_row_excluded_count: int
    isolated_row_count: int
    isolated_reasons: tuple[str, ...]
    suspended_placeholder_count: int


@dataclass(frozen=True, slots=True)
class LegacyPilotOutcome:
    batch_id: str
    manifest_sha256: str
    directory: Path
    identity_sha256: str
    confirmation: K4DSemanticsConfirmation
    recheck: LegacyRecheckOutcome
    raw_row_count: int
    canonical_row_count: int


def _canonical_write(path: Path, payload: Mapping[str, object]) -> bytes:
    raw = canonical_json_bytes(dict(payload))
    _write_exclusive(path, raw)
    return raw


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _stable_batch_id(identity_sha256: str) -> str:
    raw = bytearray(bytes.fromhex(identity_sha256)[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return f"legacy-pilot-batch:{uuid.UUID(bytes=bytes(raw))}"


def _path_safe_batch_id(batch_id: str) -> str:
    return batch_id.replace(":", "--")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_source_tables(
    source_root: Path, entries: tuple[LegacyInventoryEntry, ...]
) -> list[pa.Table]:
    tables: list[pa.Table] = []
    for entry in entries:
        path = _safe_source_file(source_root, entry.relative_path)
        before = path.stat()
        if before.st_size != entry.size or before.st_mtime_ns != entry.mtime_ns:
            raise LegacyPilotError("PILOT_SOURCE_METADATA_MISMATCH")
        digest = _sha256_file(path)
        if digest != entry.sha256:
            raise LegacyPilotError("PILOT_SOURCE_SHA_MISMATCH")
        try:
            table = _read_parquet(path)
        except (OSError, pa.ArrowException) as error:
            raise LegacyPilotError("PILOT_SOURCE_PARQUET_READ_ERROR") from error
        after = path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or _sha256_file(path) != digest
        ):
            raise LegacyPilotError("PILOT_SOURCE_MUTATED_DURING_READ")
        tables.append(table)
    return tables


def transform_k4d_tables(tables: list[pa.Table]) -> K4DPilotTransform:
    """Apply the K4D semantics to real-schema source tables (pure)."""
    parsed: list[tuple[str, date, dict[str, object]]] = []
    isolated_reasons: list[str] = []
    for table in tables:
        for source_row in table.to_pylist():
            code = source_row.get("code")
            raw_date = source_row.get("date")
            if not isinstance(code, str) or not code:
                isolated_reasons.append("ISOLATED:REQUIRED_FIELD_NULL:code")
                continue
            try:
                trade_date = parse_k4d_date(raw_date)
            except K4DSemanticsError:
                isolated_reasons.append("ISOLATED:INVALID_DATE_FORMAT")
                continue
            row = {name: source_row.get(name) for name in _FIELDS}
            if any(value is None for value in row.values()):
                isolated_reasons.append("ISOLATED:REQUIRED_FIELD_NULL")
                continue
            parsed.append((code, trade_date, row))
    parsed.sort(key=lambda item: (item[0], item[1]))
    canonical_rows: list[dict[str, object]] = []
    x_rows: dict[Key, dict[str, object]] = {}
    first_rows = 0
    suspended = 0
    previous_close: dict[str, float] = {}
    for code, trade_date, row in parsed:
        key: Key = (code, trade_date.isoformat())
        if key in x_rows:
            isolated_reasons.append("ISOLATED:DUPLICATE_KEY")
            continue
        if code in previous_close:
            derived = previous_close[code]
            classification = classify_k4d_row(row, derived)
            if classification.startswith("ISOLATED:"):
                isolated_reasons.append(classification)
                continue
            if classification == "SUSPENDED_PLACEHOLDER":
                suspended += 1
            volume = _as_float(row["volume"])
            volume_shares = round(volume)
            if abs(volume - volume_shares) > 1e-6:
                isolated_reasons.append("ISOLATED:VOLUME_NOT_INTEGRAL")
                continue
            canonical_row: dict[str, object] = {
                "code": code,
                "trade_date": trade_date,
                "open": _as_float(row["open"]),
                "high": _as_float(row["high"]),
                "low": _as_float(row["low"]),
                "close": _as_float(row["close"]),
                "pre_close": derived,
                "volume": volume_shares,
                "amount": _as_float(row["amount"]),
            }
            canonical_rows.append(canonical_row)
            x_rows[key] = dict(canonical_row)
        else:
            first_rows += 1
        previous_close[code] = _as_float(row["close"])
    schema = canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value)
    canonical = ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(canonical_rows, schema=schema),
    )
    raw_rows = sum(table.num_rows for table in tables)
    if raw_rows != len(canonical_rows) + first_rows + len(isolated_reasons):
        raise LegacyPilotError("PILOT_TRANSFORM_CONSERVATION_BROKEN")
    return K4DPilotTransform(
        canonical=canonical,
        x_rows=x_rows,
        raw_row_count=raw_rows,
        first_row_excluded_count=first_rows,
        isolated_row_count=len(isolated_reasons),
        isolated_reasons=tuple(isolated_reasons),
        suspended_placeholder_count=suspended,
    )


def _index_table(table: pa.Table) -> dict[Key, dict[str, object]]:
    rows: dict[Key, dict[str, object]] = {}
    for row in table.to_pylist():
        key: Key = (str(row["code"]), str(row["trade_date"]))
        rows[key] = dict(row)
    return rows


def _service_tables(
    fetches: list[K4DServiceFetchResult],
) -> dict[DataUpstream, pa.Table]:
    combined: dict[DataUpstream, list[pa.Table]] = {
        upstream: []
        for upstream in (DataUpstream.BAOSTOCK, DataUpstream.TENCENT, DataUpstream.SINA)
    }
    for fetch in fetches:
        for upstream, table in fetch.canonical.items():
            if upstream not in combined:
                raise LegacyPilotError("PILOT_SERVICE_UPSTREAM_UNSUPPORTED")
            combined[upstream].append(table)
    return {
        upstream: ensure_sorted(
            DatasetKind.DAILY_BAR_UNADJUSTED.value,
            pa.concat_tables(tables, promote_options="default"),
        )
        for upstream, tables in combined.items()
        if tables
    }


class LegacyPilotExecutor:
    """One-shot executor bound to a frozen plan, inventory, and fetcher."""

    def __init__(
        self,
        *,
        authorization: LegacyImportAuthorization,
        plan: K4DPilotPlan,
        inventory_payload: Mapping[str, object],
        fetcher: ServiceFetcher,
        thresholds: ThresholdSet | None = None,
    ) -> None:
        if authorization.mode.value != "PILOT":
            raise LegacyPilotError("PILOT_AUTHORIZATION_MODE_INVALID")
        if authorization.pilot_symbols != K4D_PILOT_SYMBOLS:
            raise LegacyPilotError("PILOT_SYMBOL_SET_INVALID")
        marker = str(plan.payload["authorization_marker"])
        if marker != authorization.marker:
            raise LegacyPilotError("PILOT_PLAN_MARKER_MISMATCH")
        unsigned = {k: v for k, v in plan.payload.items() if k != "plan_sha256"}
        if plan.payload["plan_sha256"] != sha256_bytes(canonical_json_bytes(unsigned)):
            raise LegacyPilotError("PILOT_PLAN_SHA_INVALID")
        inventory = inventory_from_payload(dict(inventory_payload))
        if inventory.source_fingerprint != plan.payload["source_fingerprint"]:
            raise LegacyPilotError("PILOT_INVENTORY_FINGERPRINT_MISMATCH")
        select_k4d_pilot_entries(inventory.entries)
        resolved = thresholds or thresholds_for(K4_THRESHOLD_RULE_VERSION)
        if resolved.rule_version != K4_THRESHOLD_RULE_VERSION:
            raise LegacyPilotError("PILOT_REQUIRES_K4_THRESHOLDS")
        self._authorization = authorization
        self._plan = plan
        self._inventory = inventory
        self._thresholds = resolved
        self._fetcher = fetcher

    def run(
        self,
        *,
        authorization_id: str,
        attempt_dir: Path,
        idempotency_key: str,
    ) -> LegacyPilotOutcome:
        validate_idempotency_key(idempotency_key)
        attempt_dir = Path(attempt_dir)
        if attempt_dir.exists():
            raise LegacyPilotError("PILOT_ATTEMPT_ALREADY_STARTED")
        staging_root = _safe_root(self._authorization.staging_root, must_exist=False)
        idempotency_path = staging_root / "legacy-pilot-idempotency" / f"{idempotency_key}.json"
        if idempotency_path.exists():
            raise LegacyPilotError("PILOT_IDEMPOTENCY_KEY_USED")
        attempt_dir.mkdir(parents=True)
        self._record_attempt_header(attempt_dir, authorization_id, idempotency_key)
        journal = PilotInvocationJournal(attempt_dir / _JOURNAL)
        try:
            outcome = self._execute(
                authorization_id=authorization_id,
                attempt_dir=attempt_dir,
                staging_root=staging_root,
                idempotency_path=idempotency_path,
                idempotency_key=idempotency_key,
                journal=journal,
            )
        except LegacyPilotError as error:
            self._record_failure(attempt_dir, authorization_id, error)
            raise
        except K4DSemanticsError as error:
            self._record_failure(attempt_dir, authorization_id, LegacyPilotError(error.code))
            raise LegacyPilotError(error.code) from error
        except Exception as error:
            failure = LegacyPilotError("PILOT_UNEXPECTED_FAILURE")
            self._record_failure(attempt_dir, authorization_id, failure)
            raise LegacyPilotError("PILOT_UNEXPECTED_FAILURE") from error
        return outcome

    def _record_attempt_header(
        self, attempt_dir: Path, authorization_id: str, idempotency_key: str
    ) -> None:
        _canonical_write(
            attempt_dir / "attempt-header.json",
            {
                "schema_version": LEGACY_PILOT_ATTEMPT_SCHEMA_VERSION,
                "authorization_id": authorization_id,
                "authorization_marker": self._authorization.marker,
                "plan_sha256": self._plan.plan_sha256,
                "idempotency_key": idempotency_key,
                "status": "STARTED",
            },
        )

    def _record_failure(
        self, attempt_dir: Path, authorization_id: str, error: LegacyPilotError
    ) -> None:
        target = attempt_dir / "failure.json"
        if target.exists():
            return
        payload: dict[str, object] = {
            "schema_version": LEGACY_PILOT_ATTEMPT_SCHEMA_VERSION,
            "authorization_id": authorization_id,
            "code": error.code,
            "status": "FAILED",
        }
        if error.symbol is not None:
            payload["symbol"] = error.symbol
        if error.source_error is not None:
            payload["source_error"] = dict(error.source_error)
        # A failed evidence write must never mask the original pilot failure.
        with contextlib.suppress(OSError):
            _canonical_write(target, payload)

    def _execute(
        self,
        *,
        authorization_id: str,
        attempt_dir: Path,
        staging_root: Path,
        idempotency_path: Path,
        idempotency_key: str,
        journal: PilotInvocationJournal,
    ) -> LegacyPilotOutcome:
        source_root = _safe_root(self._authorization.source_root, must_exist=True)
        entries = select_k4d_pilot_entries(self._inventory.entries)
        # 1. Read exactly the six inventoried files read-only.
        tables = _read_source_tables(source_root, entries)
        # 2. One fetcher call per security (A/B/C once each -> 18 calls); each
        # call is journaled and a fetch failure carries the unpacked stable
        # source error code into the failure evidence.
        fetches: list[K4DServiceFetchResult] = []
        for symbol in K4D_PILOT_SYMBOLS:
            try:
                fetch = self._fetcher(symbol, journal)
            except Exception as error:
                # The journal is supplementary evidence: a broken journal
                # write must never mask the original fetch failure.
                with contextlib.suppress(LegacyPilotError):
                    journal.record_symbol_failure(symbol=symbol, error=error)
                raise LegacyPilotError(
                    f"PILOT_SERVICE_FETCH_FAILED:{symbol}",
                    symbol=symbol,
                    source_error=unpack_source_error(error),
                ) from error
            if fetch.symbol != symbol:
                raise LegacyPilotError("PILOT_SERVICE_FETCH_SYMBOL_MISMATCH")
            journal.record_symbol_success(
                symbol=symbol,
                row_counts={
                    upstream.value: table.num_rows for upstream, table in fetch.canonical.items()
                },
            )
            fetches.append(fetch)
        # 3. Transform with K4D semantics (pure, in memory).
        transform = transform_k4d_tables(tables)
        if (
            marker_is_real(self._authorization.marker)
            and transform.canonical.num_rows != K4D_EXPECTED_CANONICAL_ROW_COUNT
        ):
            raise LegacyPilotError("PILOT_CANONICAL_COUNT_UNEXPECTED")
        service_by_source = _service_tables(fetches)
        for table in service_by_source.values():
            validation = validate_table(DatasetKind.DAILY_BAR_UNADJUSTED.value, table)
            if not validation.ok:
                raise LegacyPilotError("PILOT_SERVICE_TABLE_INVALID")
        service_rows = {
            upstream: _index_table(table) for upstream, table in service_by_source.items()
        }
        vendor_pre_close: dict[Key, float] = {}
        for fetch in fetches:
            for key, value in fetch.vendor_pre_close.items():
                vendor_pre_close[(fetch.symbol, key[1])] = float(value)
        # 4. Semantics confirmation gates every staging write.
        unit_ok, unit_reasons = confirm_k4d_units(
            x_rows=transform.x_rows,
            service_rows=service_rows,
            thresholds=self._thresholds,
        )
        unadjusted_ok, boundary, unadjusted_reasons = prove_k4d_unadjusted(
            vendor_pre_close=vendor_pre_close,
            service_canonical=service_rows.get(DataUpstream.BAOSTOCK, {}),
            x_rows=transform.x_rows,
            thresholds=self._thresholds,
        )
        confirmation = K4DSemanticsConfirmation(
            status=(
                K4DSemanticsStatus.CONFIRMED
                if unit_ok and unadjusted_ok
                else K4DSemanticsStatus.HYPOTHESIS
            ),
            unit_confirmed=unit_ok,
            unit_reasons=unit_reasons,
            unadjusted_proven=unadjusted_ok,
            unadjusted_boundary=boundary,
            unadjusted_reasons=unadjusted_reasons,
            x_row_count=len(transform.x_rows),
            first_row_excluded_count=transform.first_row_excluded_count,
            isolated_row_count=transform.isolated_row_count,
            isolated_reasons=transform.isolated_reasons,
            suspended_placeholder_count=transform.suspended_placeholder_count,
        )
        if confirmation.status is not K4DSemanticsStatus.CONFIRMED:
            raise LegacyPilotError("PILOT_SEMANTICS_NOT_CONFIRMED")
        # 5. Recheck every canonical row under the K4 thresholds.
        recheck = LegacyRecheckEngine(thresholds=self._thresholds).reevaluate(
            x_table=transform.canonical, service_tables=service_by_source
        )
        if recheck.evidence.input_rows != transform.canonical.num_rows:
            raise LegacyPilotError("PILOT_RECHECK_CONSERVATION_BROKEN")
        # 6. Build the immutable pilot batch (only after CONFIRMED).
        service_evidence: dict[str, object] = {
            fetch.symbol: dict(fetch.evidence) for fetch in fetches
        }
        confirmation_payload = confirmation.to_payload()
        service_evidence_payload = {
            "schema_version": "legacy-pilot-service-evidence.k4d.v1",
            "per_symbol": service_evidence,
        }
        identity = {
            "plan_sha256": self._plan.plan_sha256,
            "inventory_sha256": self._plan.payload["inventory_sha256"],
            "source_fingerprint": self._inventory.source_fingerprint,
            "pilot_files": [
                {
                    "relative_path": entry.relative_path,
                    "sha256": entry.sha256,
                    "size": entry.size,
                }
                for entry in entries
            ],
            "semantics_confirmation_sha256": sha256_bytes(
                canonical_json_bytes(confirmation_payload)
            ),
            "service_evidence_sha256": sha256_bytes(canonical_json_bytes(service_evidence_payload)),
            "k4_threshold_rule_version": K4_THRESHOLD_RULE_VERSION,
            "adapter_version": LEGACY_PILOT_ADAPTER_VERSION,
        }
        identity_sha = sha256_bytes(canonical_json_bytes(identity))
        batch_id = _stable_batch_id(identity_sha)
        imports_root = staging_root / "legacy-pilot-imports"
        final = imports_root / _path_safe_batch_id(batch_id)
        if final.exists():
            raise LegacyPilotError("PILOT_BATCH_ALREADY_EXISTS")
        incomplete = imports_root / (_path_safe_batch_id(batch_id) + ".incomplete")
        incomplete.mkdir(parents=True)
        try:
            self._write_batch(
                source_root=source_root,
                entries=entries,
                incomplete=incomplete,
                transform=transform,
                confirmation_payload=confirmation_payload,
                service_evidence_payload=service_evidence_payload,
                service_by_source=service_by_source,
                vendor_pre_close=vendor_pre_close,
                recheck_payload=recheck.to_payload(),
                identity=dict(identity),
                identity_sha=identity_sha,
                batch_id=batch_id,
                authorization_id=authorization_id,
            )
            verify_legacy_pilot_batch(incomplete, staging_root=staging_root)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incomplete, final)
        except Exception:
            failed = imports_root / (_path_safe_batch_id(batch_id) + ".failed")
            if not failed.exists():
                os.replace(incomplete, failed)
            raise
        manifest, manifest_sha = verify_legacy_pilot_batch(final, staging_root=staging_root)
        _canonical_write(
            idempotency_path,
            {
                "schema_version": LEGACY_PILOT_IDEMPOTENCY_SCHEMA_VERSION,
                "idempotency_key": idempotency_key,
                "authorization_id": authorization_id,
                "identity_sha256": identity_sha,
                "batch_id": batch_id,
                "status": "COMPLETE",
            },
        )
        _canonical_write(
            attempt_dir / "attempt-header.json.status",
            {
                "schema_version": LEGACY_PILOT_ATTEMPT_SCHEMA_VERSION,
                "authorization_id": authorization_id,
                "status": "COMPLETE",
                "batch_id": batch_id,
                "manifest_sha256": manifest_sha,
            },
        )
        counts = cast(dict[str, object], manifest["counts"])
        return LegacyPilotOutcome(
            batch_id=batch_id,
            manifest_sha256=manifest_sha,
            directory=final,
            identity_sha256=identity_sha,
            confirmation=confirmation,
            recheck=recheck,
            raw_row_count=int(str(counts["raw_row_count"])),
            canonical_row_count=int(str(counts["canonical_row_count"])),
        )

    def _write_batch(
        self,
        *,
        source_root: Path,
        entries: tuple[LegacyInventoryEntry, ...],
        incomplete: Path,
        transform: K4DPilotTransform,
        confirmation_payload: Mapping[str, object],
        service_evidence_payload: Mapping[str, object],
        service_by_source: Mapping[DataUpstream, pa.Table],
        vendor_pre_close: Mapping[Key, float],
        recheck_payload: Mapping[str, object],
        identity: Mapping[str, object],
        identity_sha: str,
        batch_id: str,
        authorization_id: str,
    ) -> None:
        file_entries: list[dict[str, object]] = []
        for entry in entries:
            source = _safe_source_file(source_root, entry.relative_path)
            before = source.stat()
            if before.st_size != entry.size or before.st_mtime_ns != entry.mtime_ns:
                raise LegacyPilotError("PILOT_SOURCE_METADATA_MISMATCH")
            raw_target = incomplete / "raw" / entry.relative_path
            raw_target.parent.mkdir(parents=True, exist_ok=True)
            payload = source.read_bytes()
            if sha256_bytes(payload) != entry.sha256:
                raise LegacyPilotError("PILOT_SOURCE_SHA_MISMATCH")
            _write_exclusive(raw_target, payload)
            after = source.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or _sha256_file(source) != entry.sha256
            ):
                raise LegacyPilotError("PILOT_SOURCE_MUTATED_DURING_COPY")
            file_entries.append(
                {
                    "relative_path": entry.relative_path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "row_count": entry.row_count,
                }
            )
        candidate_target = incomplete / "candidate" / "canonical.parquet"
        candidate_target.parent.mkdir(parents=True, exist_ok=True)
        _write_parquet(transform.canonical, candidate_target)
        for upstream, table in sorted(service_by_source.items(), key=lambda item: item[0].value):
            service_target = incomplete / "service" / f"{upstream.value}.parquet"
            service_target.parent.mkdir(parents=True, exist_ok=True)
            _write_parquet(table, service_target)
        vendor_payload = {
            "schema_version": "legacy-pilot-vendor-pre-close.k4d.v1",
            "rows": [
                {"code": code, "trade_date": trade_date, "vendor_reference_pre_close": value}
                for (code, trade_date), value in sorted(vendor_pre_close.items())
            ],
        }
        _canonical_write(incomplete / "vendor-pre-close.json", vendor_payload)
        _canonical_write(incomplete / "semantics-confirmation.json", dict(confirmation_payload))
        _canonical_write(incomplete / "service-evidence.json", dict(service_evidence_payload))
        _canonical_write(incomplete / "recheck.json", dict(recheck_payload))
        unsigned = {
            "manifest_schema_version": LEGACY_PILOT_MANIFEST_SCHEMA_VERSION,
            "identity_sha256": identity_sha,
            "batch_id": batch_id,
            "authorization_id": authorization_id,
            "authorization_marker": self._authorization.marker,
            "plan_sha256": self._plan.plan_sha256,
            "plan": self._plan.payload,
            "inventory_sha256": self._plan.payload["inventory_sha256"],
            "source_fingerprint": self._inventory.source_fingerprint,
            "pilot_files": sorted(file_entries, key=lambda value: str(value["relative_path"])),
            "semantics": self._plan.payload["semantics"],
            "semantics_confirmation": dict(confirmation_payload),
            "semantics_confirmation_sha256": identity["semantics_confirmation_sha256"],
            "service_evidence": dict(service_evidence_payload),
            "service_evidence_sha256": identity["service_evidence_sha256"],
            "k4_threshold_rule_version": K4_THRESHOLD_RULE_VERSION,
            "library_versions": {
                "pyarrow": pa.__version__,
                "adapter": LEGACY_PILOT_ADAPTER_VERSION,
            },
            "counts": {
                "raw_file_count": len(file_entries),
                "raw_byte_count": sum(entry.size for entry in entries),
                "raw_row_count": transform.raw_row_count,
                "first_row_excluded_count": transform.first_row_excluded_count,
                "isolated_row_count": transform.isolated_row_count,
                "suspended_placeholder_count": transform.suspended_placeholder_count,
                "canonical_row_count": transform.canonical.num_rows,
            },
            "canonical_content_sha256": canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED, [transform.canonical]
            ),
            "recheck": dict(recheck_payload),
            "warehouse_published": False,
            "status": "PILOT_STAGED_UNPUBLISHED",
        }
        manifest_sha = sha256_bytes(canonical_json_bytes(unsigned))
        _write_exclusive(
            incomplete / _MANIFEST,
            canonical_json_bytes({**unsigned, "manifest_sha256": manifest_sha}),
        )
        _write_exclusive(
            incomplete / _COMPLETE,
            canonical_json_bytes({"batch_id": batch_id, "manifest_sha256": manifest_sha}),
        )


def _load_canonical_json(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or raw not in (
        canonical_json_bytes(payload),
        canonical_json_bytes(payload) + b"\n",
    ):
        raise LegacyPilotError("PILOT_BATCH_JSON_NOT_CANONICAL")
    return payload


def verify_legacy_pilot_batch(
    directory: Path, *, staging_root: Path
) -> tuple[dict[str, object], str]:
    """Fully recompute one pilot batch offline from its stored artifacts."""
    root = _safe_root(staging_root, must_exist=True)
    try:
        resolved = directory.resolve(strict=True)
        resolved.relative_to(root / "legacy-pilot-imports")
    except (OSError, ValueError) as error:
        raise LegacyPilotError("PILOT_BATCH_DIRECTORY_INVALID") from error
    if resolved.parent.name != "legacy-pilot-imports" or not resolved.is_dir():
        raise LegacyPilotError("PILOT_BATCH_DIRECTORY_INVALID")
    manifest = _load_canonical_json(resolved / _MANIFEST)
    complete = _load_canonical_json(resolved / _COMPLETE)
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    manifest_sha = sha256_bytes(canonical_json_bytes(unsigned))
    if manifest.get("manifest_sha256") != manifest_sha:
        raise LegacyPilotError("PILOT_MANIFEST_SHA_MISMATCH")
    if complete != {"batch_id": manifest.get("batch_id"), "manifest_sha256": manifest_sha}:
        raise LegacyPilotError("PILOT_COMPLETE_BINDING_MISMATCH")
    if (
        manifest.get("manifest_schema_version") != LEGACY_PILOT_MANIFEST_SCHEMA_VERSION
        or manifest.get("warehouse_published") is not False
        or manifest.get("status") != "PILOT_STAGED_UNPUBLISHED"
    ):
        raise LegacyPilotError("PILOT_MANIFEST_CONTRACT_INVALID")
    plan_payload = manifest.get("plan")
    if not isinstance(plan_payload, dict):
        raise LegacyPilotError("PILOT_MANIFEST_PLAN_INVALID")
    plan_unsigned = {k: v for k, v in plan_payload.items() if k != "plan_sha256"}
    if plan_payload.get("plan_sha256") != sha256_bytes(canonical_json_bytes(plan_unsigned)):
        raise LegacyPilotError("PILOT_PLAN_SHA_MISMATCH")
    k4d_semantics_from_payload(plan_payload.get("semantics"))
    pilot_files = manifest.get("pilot_files")
    if not isinstance(pilot_files, list) or len(pilot_files) != 6:
        raise LegacyPilotError("PILOT_MANIFEST_PILOT_FILES_INVALID")
    raw_tables: list[pa.Table] = []
    raw_byte_count = 0
    raw_row_count = 0
    for item in pilot_files:
        if not isinstance(item, dict) or set(item) != {
            "relative_path",
            "size",
            "sha256",
            "row_count",
        }:
            raise LegacyPilotError("PILOT_MANIFEST_PILOT_FILES_INVALID")
        raw_path = resolved / "raw" / str(item["relative_path"])
        payload = raw_path.read_bytes()
        if len(payload) != item["size"] or sha256_bytes(payload) != item["sha256"]:
            raise LegacyPilotError("PILOT_BATCH_RAW_MISMATCH")
        raw_tables.append(_read_parquet(raw_path))
        raw_byte_count += int(str(item["size"]))
        raw_row_count += int(str(item["row_count"]))
    transform = transform_k4d_tables(raw_tables)
    if transform.raw_row_count != raw_row_count:
        raise LegacyPilotError("PILOT_BATCH_RAW_ROW_COUNT_MISMATCH")
    candidate = _read_parquet(resolved / "candidate" / "canonical.parquet")
    if not candidate.equals(transform.canonical):
        raise LegacyPilotError("PILOT_BATCH_CANDIDATE_NOT_REBUILDABLE")
    thresholds = thresholds_for(K4_THRESHOLD_RULE_VERSION)
    service_tables: dict[DataUpstream, pa.Table] = {}
    for upstream in (DataUpstream.BAOSTOCK, DataUpstream.TENCENT, DataUpstream.SINA):
        path = resolved / "service" / f"{upstream.value}.parquet"
        if path.is_file():
            service_tables[upstream] = _read_parquet(path)
    vendor_payload = _load_canonical_json(resolved / "vendor-pre-close.json")
    vendor_rows = vendor_payload.get("rows")
    if not isinstance(vendor_rows, list):
        raise LegacyPilotError("PILOT_VENDOR_PRE_CLOSE_INVALID")
    vendor_pre_close = {
        (str(row["code"]), str(row["trade_date"])): float(row["vendor_reference_pre_close"])
        for row in vendor_rows
    }
    service_rows = {upstream: _index_table(table) for upstream, table in service_tables.items()}
    unit_ok, unit_reasons = confirm_k4d_units(
        x_rows=transform.x_rows, service_rows=service_rows, thresholds=thresholds
    )
    unadjusted_ok, boundary, unadjusted_reasons = prove_k4d_unadjusted(
        vendor_pre_close=vendor_pre_close,
        service_canonical=service_rows.get(DataUpstream.BAOSTOCK, {}),
        x_rows=transform.x_rows,
        thresholds=thresholds,
    )
    if not (unit_ok and unadjusted_ok):
        raise LegacyPilotError("PILOT_BATCH_SEMANTICS_NOT_RECONFIRMABLE")
    confirmation = K4DSemanticsConfirmation(
        status=K4DSemanticsStatus.CONFIRMED,
        unit_confirmed=unit_ok,
        unit_reasons=unit_reasons,
        unadjusted_proven=unadjusted_ok,
        unadjusted_boundary=boundary,
        unadjusted_reasons=unadjusted_reasons,
        x_row_count=len(transform.x_rows),
        first_row_excluded_count=transform.first_row_excluded_count,
        isolated_row_count=transform.isolated_row_count,
        isolated_reasons=transform.isolated_reasons,
        suspended_placeholder_count=transform.suspended_placeholder_count,
    )
    stored_confirmation = manifest.get("semantics_confirmation")
    if stored_confirmation != confirmation.to_payload():
        raise LegacyPilotError("PILOT_BATCH_CONFIRMATION_MISMATCH")
    recheck = LegacyRecheckEngine(thresholds=thresholds).reevaluate(
        x_table=transform.canonical, service_tables=service_tables
    )
    if manifest.get("recheck") != recheck.to_payload():
        raise LegacyPilotError("PILOT_BATCH_RECHECK_MISMATCH")
    canonical_content = canonical_content_sha256(
        DatasetKind.DAILY_BAR_UNADJUSTED, [transform.canonical]
    )
    if manifest.get("canonical_content_sha256") != canonical_content:
        raise LegacyPilotError("PILOT_BATCH_CANONICAL_CONTENT_MISMATCH")
    expected_counts = {
        "raw_file_count": len(pilot_files),
        "raw_byte_count": raw_byte_count,
        "raw_row_count": transform.raw_row_count,
        "first_row_excluded_count": transform.first_row_excluded_count,
        "isolated_row_count": transform.isolated_row_count,
        "suspended_placeholder_count": transform.suspended_placeholder_count,
        "canonical_row_count": transform.canonical.num_rows,
    }
    if manifest.get("counts") != expected_counts:
        raise LegacyPilotError("PILOT_BATCH_COUNTS_MISMATCH")
    actual_files = {
        path.relative_to(resolved).as_posix() for path in resolved.rglob("*") if path.is_file()
    }
    expected_files = {
        _MANIFEST,
        _COMPLETE,
        "semantics-confirmation.json",
        "service-evidence.json",
        "recheck.json",
        "vendor-pre-close.json",
        "candidate/canonical.parquet",
    }
    expected_files |= {f"raw/{item['relative_path']}" for item in pilot_files}
    expected_files |= {f"service/{upstream.value}.parquet" for upstream in service_tables}
    if actual_files != expected_files:
        raise LegacyPilotError("PILOT_BATCH_FILE_SET_MISMATCH")
    return manifest, manifest_sha


__all__ = [
    "LEGACY_PILOT_ADAPTER_VERSION",
    "LEGACY_PILOT_INVOCATION_SCHEMA_VERSION",
    "LEGACY_PILOT_MANIFEST_SCHEMA_VERSION",
    "K4DPilotTransform",
    "K4DServiceFetchResult",
    "LegacyPilotError",
    "LegacyPilotExecutor",
    "LegacyPilotOutcome",
    "PilotInvocationJournal",
    "ServiceFetcher",
    "transform_k4d_tables",
    "unpack_source_error",
    "verify_legacy_pilot_batch",
]
