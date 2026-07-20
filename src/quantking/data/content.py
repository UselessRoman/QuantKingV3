"""Public Canonical content fingerprint contract without framework imports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from quantking.data.models import DatasetKind, EmptyResultInfo


def canonical_content_sha256(
    dataset: DatasetKind,
    tables: Sequence[Any],
    *,
    empty_result: EmptyResultInfo | None = None,
) -> str:
    """Return the shared Stage 1 logical-content SHA-256."""
    from quantking.data.adapters.canonical_content import canonical_content_sha256 as compute

    return compute(dataset, tables, empty_result=empty_result)


__all__ = ("canonical_content_sha256",)
