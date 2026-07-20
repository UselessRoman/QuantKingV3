"""Provider-neutral fallback-source candidate assessment package (S1-04B).

This package records per-source x dataset evidence without allowing synthetic
data to mark any source as production-ready. The production matrix must
remain ``BLOCKED_EXTERNAL_VALIDATION`` or ``UNVERIFIED`` for every fallback
source; no auto-switch configuration may exist. The package is deliberately
independent of SQLAlchemy, DuckDB, vendor SDKs and FastAPI.
"""
