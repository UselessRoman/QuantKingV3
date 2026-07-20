"""Deterministic single-source quality rule executor (S1-04A).

The engine is framework-free apart from the optional :mod:`pyarrow`
dependency that flows through the rule callables. It never imports
SQLAlchemy, DuckDB, vendor SDKs or FastAPI. The executor is the only
component that calls :meth:`QualityRule.evaluate`; rules themselves are
pure functions of their inputs.

Run identity is derived deterministically from the request credentials,
the rule set version, and the candidate content SHA. Re-running the
same request against the same content must produce the same ``run_id``
and the same outcome statuses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace

import pyarrow as pa

from quantking.common.ids import EntityId
from quantking.common.time import utc_now
from quantking.data.adapters.canonical_content import canonical_content_sha256
from quantking.data.models import (
    BatchFailureInfo,
    BatchFailureKind,
    QualityRunStatus,
)
from quantking.data.quality.models import (
    QualityOutcomeResult,
    QualityRequest,
    QualityRunArtifact,
    RuleOutcomeStatus,
)
from quantking.data.quality.rules import QualityRuleSet


def _deterministic_run_id(request: QualityRequest, rule_version: str) -> EntityId:
    """Derive a stable ``data_quality`` EntityId from the request credentials."""
    payload = {
        "rule_version": rule_version,
        "request": request.to_deterministic_dict(),
    }
    serialised = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(serialised.encode("utf-8")).hexdigest()
    # Render the digest as a canonical UUID4 string. The digest is 64 hex
    # chars; a UUID4 is 32 hex chars plus 4 version/variant bits. We take
    # the first 32 chars, force the version nibble to ``4`` and the
    # variant bits to RFC 4122 so the resulting UUID parses as a valid v4.
    hex32 = digest[:32]
    hex_list = list(hex32)
    hex_list[12] = "4"  # version 4
    hex_list[16] = "8"  # variant 10xx
    canonical = "".join(hex_list)
    formatted = (
        f"{canonical[0:8]}-{canonical[8:12]}-{canonical[12:16]}-"
        f"{canonical[16:20]}-{canonical[20:32]}"
    )
    return EntityId.parse(f"data_quality:{formatted}")


class QualityEngine:
    """Execute a :class:`QualityRuleSet` against one candidate table."""

    def __init__(self, *, rule_set: QualityRuleSet) -> None:
        if not isinstance(rule_set, QualityRuleSet):
            raise TypeError("rule_set must be a QualityRuleSet")
        self._rule_set = rule_set

    @property
    def rule_set(self) -> QualityRuleSet:
        return self._rule_set

    def execute(
        self,
        *,
        request: QualityRequest,
        table: pa.Table,
        reference_tables: Mapping[str, pa.Table],
    ) -> QualityRunArtifact:
        """Run every applicable rule and return the resulting artifact.

        Internal executor errors (rule callables that raise, type errors
        on the candidate table, ...) propagate to the caller. The engine
        never produces a ``RUNNING`` artifact and never turns an internal
        exception into a ``PASSED`` outcome.
        """
        if not isinstance(request, QualityRequest):
            raise TypeError("request must be a QualityRequest")
        if not isinstance(table, pa.Table):
            raise TypeError("table must be a pyarrow.Table")
        if not isinstance(reference_tables, Mapping):
            raise TypeError("reference_tables must be a mapping")
        started_at = utc_now()
        actual_content_sha = canonical_content_sha256(
            request.dataset,
            (table,),
            empty_result=request.empty_result,
            validate=False,
        )
        request_for_identity = replace(request, content_sha256=actual_content_sha)
        run_id = _deterministic_run_id(request_for_identity, self._rule_set.rule_version)
        applicable = self._rule_set.for_dataset(request.dataset)
        results: list[QualityOutcomeResult] = []
        for rule in applicable:
            # Reference tables are required for some rules; pass them
            # through unfiltered so the rule itself can decide whether
            # the binding is missing.
            outcome = rule.evaluate(
                request=request,
                table=table,
                reference_tables=reference_tables,
            )
            if not isinstance(outcome, QualityOutcomeResult):
                raise TypeError(f"rule {rule.code!r} must return a QualityOutcomeResult")
            results.append(outcome)
        ended_at = utc_now()
        status = self._aggregate_status(results)
        failure: BatchFailureInfo | None
        if status is QualityRunStatus.FAILED:
            failed_rules = [r for r in results if r.status is RuleOutcomeStatus.FAILED]
            summary = f"{len(failed_rules)} blocking rule(s) failed: " + ",".join(
                rule.rule_code for rule in failed_rules
            )
            failure = BatchFailureInfo(
                error_type=BatchFailureKind.QUALITY,
                message=summary,
                retryable=False,
            )
        else:
            failure = None
        return QualityRunArtifact(
            run_id=run_id,
            version_id=request.version_id,
            rule_version=self._rule_set.rule_version,
            schema_version=request.schema_version,
            content_sha256=actual_content_sha,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            results=tuple(results),
            failure=failure,
        )

    @staticmethod
    def _aggregate_status(results: list[QualityOutcomeResult]) -> QualityRunStatus:
        if any(result.status is RuleOutcomeStatus.FAILED for result in results):
            return QualityRunStatus.FAILED
        # PASSED requires at least one applicable blocking rule with PASSED
        # status. A dataset whose rule set is empty (no applicable rules)
        # cannot be PASSED; the executor refuses to gate-publish such a
        # version. SKIPPED-only outcomes are not PASSED either.
        if results and all(result.status is RuleOutcomeStatus.PASSED for result in results):
            return QualityRunStatus.PASSED
        return QualityRunStatus.FAILED


__all__: tuple[str, ...] = ("QualityEngine",)
