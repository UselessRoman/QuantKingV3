"""Provider-neutral source-override application service package (S1-04C).

This package orchestrates the explicit fallback-source override workflow:
creating a PENDING override from a verified DifferenceReport, approving or
rejecting it through an audited transaction, and applying an APPROVED
override to a new direct-child DatasetVersion with the quality gate
re-run, atomic publish and APPLIED marking in the same audited transaction.

The package is deliberately independent of SQLAlchemy, DuckDB, vendor SDKs
and FastAPI. It depends only on the framework-free domain models, the
SQLite unit-of-work factory, and the standard library.
"""
