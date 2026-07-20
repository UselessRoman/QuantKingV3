"""Framework-free single-source data quality rule contracts (S1-04A).

This package declares the framework-free quality rule contracts,
the versioned rule catalogue, the deterministic executor, and the
managed artifact writer. None of these modules import SQLAlchemy,
DuckDB, vendor SDKs or FastAPI; only :mod:`pyarrow` is consumed by
the rule callables and the artifact writer.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
