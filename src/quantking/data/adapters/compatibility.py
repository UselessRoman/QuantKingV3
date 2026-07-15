"""DuckDB Stage 0 compatibility adapter."""

from __future__ import annotations

from pathlib import Path

from quantking.common.compatibility import (
    CompatibilityProbeError,
    ProbeErrorCode,
    ProbePayload,
    ProbeResult,
    capture_probe,
)


def probe_duckdb(root: Path) -> ProbeResult:
    """Execute a real query after reopening a temporary database read-only."""
    return capture_probe("duckdb", lambda: _run_duckdb(root))


def _run_duckdb(root: Path) -> ProbePayload:
    import duckdb

    database = root / "duckdb-compatibility.duckdb"
    with duckdb.connect(str(database)) as writer:
        writer.execute("CREATE TABLE probe(value INTEGER)")
        writer.execute("INSERT INTO probe VALUES (2), (3), (5)")
    with duckdb.connect(str(database), read_only=True) as reader:
        observed = reader.execute("SELECT sum(value) FROM probe").fetchone()
    if observed != (10,):
        raise CompatibilityProbeError(
            ProbeErrorCode.EXECUTION_FAILED,
            "DuckDB read-only query returned unexpected evidence",
        )
    return ProbePayload(
        str(duckdb.__version__),
        ("temporary_database_created", "read_only_connection_opened", "query_executed"),
    )
