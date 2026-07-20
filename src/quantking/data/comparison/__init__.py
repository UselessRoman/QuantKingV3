"""Provider-neutral cross-source comparison package (S1-04B).

This package is deliberately independent of SQLAlchemy, DuckDB, vendor
SDKs and FastAPI. It depends only on PyArrow (for canonical schema
introspection), the framework-free domain models, and the standard
library. The comparator aligns two Canonical tables by primary key
and produces a deterministic, immutable :class:`DifferenceReport`
with field-level thresholds.
"""
