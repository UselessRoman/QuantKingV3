"""S2-01 executable research contracts; no fetching, building or publication.

These immutable values are consumed by qualification tests now and by the
explicit-version research builder in S2-02. They do not introduce a lifecycle,
repository or a second publication service.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.models import ManifestFileEntry


class ResearchContractError(ValueError):
    """Invalid or insufficient research facts; caller must not publish."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ResearchContractError(reason)


def _text(value: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), "EMPTY_TEXT")


def _sha(value: str) -> None:
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", value)), "INVALID_SHA256")


def _code(value: str) -> None:
    _require(bool(re.fullmatch(r"[0-9]{6}\.(SH|SZ)", value)), "INVALID_SECURITY")


def _date(value: date) -> None:
    _require(type(value) is date, "INVALID_TRADING_DATE")


def _decimal(value: Decimal, *, positive: bool = False) -> None:
    _require(isinstance(value, Decimal) and value.is_finite(), "INVALID_DECIMAL")
    _require(value > 0 if positive else value >= 0, "INVALID_DECIMAL_RANGE")


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class KnowledgeTimePrecision(StrEnum):
    EXACT = "EXACT"
    DATE = "DATE"


@dataclass(frozen=True, slots=True)
class PublicationDateEvidence:
    """Binding to an independently qualified official publication record.

    These references validate structure, not the record's authority or bytes.
    A signature date, URL date or search snippet alone cannot qualify a record.
    """

    document_id: str
    document_revision: str
    document_sha256: str
    published_on: date
    publication_record_sha256: str

    def __post_init__(self) -> None:
        _text(self.document_id)
        _text(self.document_revision)
        _sha(self.document_sha256)
        _sha(self.publication_record_sha256)
        _date(self.published_on)
        _require(self.published_on < date.max, "PUBLICATION_DATE_OUT_OF_RANGE")

    def safe_available_at(self) -> UtcTimestamp:
        """Next Shanghai natural-day midnight, not the actual disclosure time."""
        return UtcTimestamp(
            datetime.combine(
                self.published_on + timedelta(days=1), time.min, ZoneInfo("Asia/Shanghai")
            )
        )


@dataclass(frozen=True, slots=True)
class SourceFact:
    """Source revision and provable knowledge time, distinct from fetch time."""

    source: str
    revision: str
    evidence_sha256: str
    available_at: UtcTimestamp
    observed_at: UtcTimestamp
    precision: KnowledgeTimePrecision = KnowledgeTimePrecision.EXACT
    publication: PublicationDateEvidence | None = None

    def __post_init__(self) -> None:
        _text(self.source)
        _text(self.revision)
        _sha(self.evidence_sha256)
        _require(
            isinstance(self.available_at, UtcTimestamp)
            and isinstance(self.observed_at, UtcTimestamp),
            "INVALID_TIMESTAMP",
        )
        _require(isinstance(self.precision, KnowledgeTimePrecision), "INVALID_TIME_PRECISION")
        if self.precision is KnowledgeTimePrecision.DATE:
            publication = self.publication
            if not isinstance(publication, PublicationDateEvidence):
                raise ResearchContractError("PUBLICATION_REQUIRED")
            _require(
                publication.document_revision == self.revision, "PUBLICATION_REVISION_MISMATCH"
            )
            _require(
                publication.document_sha256 == self.evidence_sha256, "PUBLICATION_CONTENT_MISMATCH"
            )
            _require(
                self.available_at == publication.safe_available_at(), "DATE_AVAILABILITY_MISMATCH"
            )
        else:
            _require(self.publication is None, "UNEXPECTED_PUBLICATION_DATE")
        _require(self.available_at.value <= self.observed_at.value, "INVALID_OBSERVATION_TIME")

    @classmethod
    def from_publication_date(
        cls,
        *,
        source: str,
        revision: str,
        evidence_sha256: str,
        observed_at: UtcTimestamp,
        publication: PublicationDateEvidence,
    ) -> SourceFact:
        """Compute a conservative cutoff from a previously qualified date record."""
        if not isinstance(publication, PublicationDateEvidence):
            raise ResearchContractError("PUBLICATION_REQUIRED")
        return cls(
            source,
            revision,
            evidence_sha256,
            publication.safe_available_at(),
            observed_at,
            KnowledgeTimePrecision.DATE,
            publication,
        )


@dataclass(frozen=True, slots=True)
class SecurityLifecycle:
    """One revision of [listed_on, delisted_on); None means known open-ended."""

    code: str
    listed_on: date
    delisted_on: date | None
    source: SourceFact

    def __post_init__(self) -> None:
        _code(self.code)
        _date(self.listed_on)
        _require(isinstance(self.source, SourceFact), "SOURCE_REQUIRED")
        if self.delisted_on is not None:
            _date(self.delisted_on)
            _require(self.listed_on < self.delisted_on, "INVALID_LIFECYCLE_INTERVAL")


class RiskStatus(StrEnum):
    NORMAL = "NORMAL"
    ST = "ST"
    STAR_ST = "STAR_ST"
    DELISTING = "DELISTING"


@dataclass(frozen=True, slots=True)
class SecurityStatusDaily:
    code: str
    trade_date: date
    risk_status: RiskStatus
    suspended: bool
    source: SourceFact

    def __post_init__(self) -> None:
        _code(self.code)
        _date(self.trade_date)
        _require(isinstance(self.risk_status, RiskStatus), "UNKNOWN_RISK_STATUS")
        _require(type(self.suspended) is bool, "UNKNOWN_SUSPENSION_STATUS")
        _require(isinstance(self.source, SourceFact), "SOURCE_REQUIRED")


@dataclass(frozen=True, slots=True)
class AdjustmentFactorDaily:
    """Positive forward cumulative multiplier, fixed anchor; never future-normalized."""

    code: str
    trade_date: date
    anchor_date: date
    cumulative_factor: Decimal
    source: SourceFact

    def __post_init__(self) -> None:
        _code(self.code)
        _date(self.trade_date)
        _date(self.anchor_date)
        _require(self.anchor_date <= self.trade_date, "FUTURE_FACTOR_ANCHOR")
        _decimal(self.cumulative_factor, positive=True)
        _require(
            self.anchor_date != self.trade_date or self.cumulative_factor == Decimal(1),
            "FACTOR_ANCHOR_NOT_NORMALIZED",
        )
        _require(isinstance(self.source, SourceFact), "SOURCE_REQUIRED")


ReferenceRow = SecurityLifecycle | SecurityStatusDaily | AdjustmentFactorDaily


def validate_reference_rows(rows: tuple[ReferenceRow, ...], *, as_of: UtcTimestamp) -> None:
    """Reject future knowledge, conflicting keys and per-security anchor changes.

    Select an explicit source revision before calling; missing coverage is checked
    against the declared scope by S2-02, never inferred from an empty tuple here.
    """
    _require(bool(rows), "REFERENCE_EMPTY")
    keys: set[tuple[str, str, date]] = set()
    anchors: dict[str, date] = {}
    for row in rows:
        _require(row.source.available_at.value <= as_of.value, "FUTURE_KNOWLEDGE")
        if isinstance(row, AdjustmentFactorDaily):
            _require(
                anchors.setdefault(row.code, row.anchor_date) == row.anchor_date,
                "CONFLICTING_FACTOR_ANCHOR",
            )
        effective = row.listed_on if isinstance(row, SecurityLifecycle) else row.trade_date
        # Lifecycle has one selected revision per security, not one per IPO date.
        key = (
            type(row).__name__,
            row.code,
            date.min if isinstance(row, SecurityLifecycle) else effective,
        )
        _require(key not in keys, "DUPLICATE_OR_CONFLICTING_REFERENCE")
        keys.add(key)


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    BONUS_SHARES = "BONUS_SHARES"


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """Facts for Stage 4/5; incomplete execution facts remain explicit gaps."""

    event_id: str
    code: str
    kind: CorporateActionType
    disclosed_at: UtcTimestamp | None
    record_date: date
    ex_date: date
    cash_per_share: Decimal | None
    shares_per_share: Decimal | None
    cash_pay_date: date | None
    shares_available_date: date | None
    reference_price: Decimal | None
    tax_policy: str | None
    source: SourceFact
    disclosed_on: date | None = None

    def __post_init__(self) -> None:
        _text(self.event_id)
        _code(self.code)
        _require(isinstance(self.kind, CorporateActionType), "UNKNOWN_ACTION_TYPE")
        _date(self.record_date)
        _date(self.ex_date)
        _require(self.record_date <= self.ex_date, "INVALID_ACTION_DATES")
        _require(isinstance(self.source, SourceFact), "SOURCE_REQUIRED")
        if self.source.precision is KnowledgeTimePrecision.DATE:
            _require(self.disclosed_at is None, "DISCLOSURE_PRECISION_CONFLICT")
            publication = self.source.publication
            _require(
                publication is not None and self.disclosed_on == publication.published_on,
                "DISCLOSURE_DATE_MISMATCH",
            )
            _date(self.disclosed_on)  # type: ignore[arg-type]
        else:
            _require(self.disclosed_on is None, "DISCLOSURE_PRECISION_CONFLICT")
            if not isinstance(self.disclosed_at, UtcTimestamp):
                raise ResearchContractError("INVALID_DISCLOSURE_TIME")
            _require(
                self.disclosed_at.value <= self.source.available_at.value,
                "AVAILABLE_BEFORE_DISCLOSURE",
            )
        for value in (self.cash_per_share, self.shares_per_share, self.reference_price):
            if value is not None:
                _decimal(value, positive=True)
        for event_date in (self.cash_pay_date, self.shares_available_date):
            if event_date is not None:
                _date(event_date)
                _require(self.ex_date <= event_date, "INVALID_SETTLEMENT_DATE")
        if self.kind is CorporateActionType.CASH_DIVIDEND:
            _require(self.shares_per_share is None, "ACTION_UNIT_CONFLICT")
        else:
            _require(self.cash_per_share is None, "ACTION_UNIT_CONFLICT")
        if self.tax_policy is not None:
            _text(self.tax_policy)

    def execution_gaps(self) -> tuple[str, ...]:
        """Missing facts block positions spanning the event, not only new orders."""
        required = (
            (
                ("cash_per_share", self.cash_per_share),
                ("cash_pay_date", self.cash_pay_date),
            )
            if self.kind is CorporateActionType.CASH_DIVIDEND
            else (
                ("shares_per_share", self.shares_per_share),
                ("shares_available_date", self.shares_available_date),
            )
        )
        return tuple(
            name
            for name, value in (
                *required,
                ("reference_price", self.reference_price),
                ("tax_policy", self.tax_policy),
            )
            if value is None
        )


@dataclass(frozen=True, slots=True)
class ResearchRules:
    """Frozen first-use configuration; changes require a new rule version."""

    version: str
    minimum_listing_sessions: int
    liquidity_window: int
    liquidity_minimum_observations: int
    liquidity_median_cny: Decimal
    signal_rule: str
    label_rule: str
    price_rule: str

    def __post_init__(self) -> None:
        _require(self.version == "research-rules.v1", "UNKNOWN_RULE_VERSION")
        _require(
            type(self.minimum_listing_sessions) is int
            and self.minimum_listing_sessions == 120
            and type(self.liquidity_window) is int
            and self.liquidity_window == 20
            and type(self.liquidity_minimum_observations) is int
            and self.liquidity_minimum_observations == 20,
            "RULE_VERSION_MISMATCH",
        )
        _decimal(self.liquidity_median_cny, positive=True)
        _require(self.liquidity_median_cny == Decimal("10000000"), "RULE_VERSION_MISMATCH")
        _require(self.signal_rule == "ISO_WEEK_LAST_SESSION", "RULE_VERSION_MISMATCH")
        _require(self.label_rule == "NEXT_OPEN_TO_NEXT_WEEK_NEXT_OPEN", "RULE_VERSION_MISMATCH")
        _require(self.price_rule == "FIXED_ANCHOR_FORWARD_MULTIPLIER", "RULE_VERSION_MISMATCH")

    def fingerprint(self) -> str:
        payload = asdict(self)
        payload["liquidity_median_cny"] = str(self.liquidity_median_cny.normalize())
        return _digest(payload)


@dataclass(frozen=True, slots=True)
class ResearchScope:
    """Explicit sorted security/session sets, with separate warmup/signal/tail."""

    codes: tuple[str, ...]
    sessions: tuple[date, ...]
    signal_start: date
    signal_end: date
    calendar_version: EntityId
    membership_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "codes", tuple(self.codes))
        object.__setattr__(self, "sessions", tuple(self.sessions))
        _require(bool(self.codes) and self.codes == tuple(sorted(set(self.codes))), "INVALID_CODES")
        for code in self.codes:
            _code(code)
        _require(
            bool(self.sessions) and self.sessions == tuple(sorted(set(self.sessions))),
            "INVALID_SESSIONS",
        )
        for session in self.sessions:
            _date(session)
        _date(self.signal_start)
        _date(self.signal_end)
        _require(self.signal_start <= self.signal_end, "INVALID_SIGNAL_WINDOW")
        _require(
            self.signal_start in self.sessions and self.signal_end in self.sessions,
            "SIGNAL_OUTSIDE_ACQUISITION_WINDOW",
        )
        _require(self.calendar_version.namespace == "data_version", "INVALID_CALENDAR_VERSION")
        _sha(self.membership_sha256)

    def fingerprint(self) -> str:
        return _digest(
            {
                "codes": self.codes,
                "sessions": [item.isoformat() for item in self.sessions],
                "signal_start": self.signal_start.isoformat(),
                "signal_end": self.signal_end.isoformat(),
                "calendar_version": str(self.calendar_version),
                "membership_sha256": self.membership_sha256,
            }
        )


class ResearchArtifactType(StrEnum):
    RESEARCH_INPUT = "ResearchInputVersion"
    UNIVERSE = "UniverseVersion"
    LABEL = "LabelVersion"
    FACTOR_SET = "FactorSetVersion"
    QLIB_VIEW = "QlibViewVersion"


@dataclass(frozen=True, slots=True)
class ParentArtifact:
    version_id: EntityId
    manifest_sha256: str

    def __post_init__(self) -> None:
        _require(isinstance(self.version_id, EntityId), "EXPLICIT_PARENT_REQUIRED")
        _sha(self.manifest_sha256)


@dataclass(frozen=True, slots=True)
class ResearchArtifactManifest:
    """Shared payload contract, reusing existing file entries and publication machinery.

    Construction verifies structure only. It does not prove parent PUBLISHED status
    or file bytes; the existing publisher must verify those before publication.
    """

    artifact_type: ResearchArtifactType
    version_id: EntityId
    parents: tuple[ParentArtifact, ...]
    scope: ResearchScope
    rules_sha256: str
    files: tuple[ManifestFileEntry, ...]
    code_version: str
    dependency_lock_sha256: str
    gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        _require(isinstance(self.artifact_type, ResearchArtifactType), "INVALID_ARTIFACT_TYPE")
        _require(self.version_id.namespace == "research_version", "INVALID_RESEARCH_VERSION")
        _require(isinstance(self.scope, ResearchScope), "SCOPE_REQUIRED")
        for name in ("parents", "files", "gaps"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        _require(bool(self.parents), "EXPLICIT_PARENT_REQUIRED")
        _require(all(isinstance(p, ParentArtifact) for p in self.parents), "INVALID_PARENT")
        parent_ids = [str(p.version_id) for p in self.parents]
        _require(parent_ids == sorted(set(parent_ids)), "DUPLICATE_OR_UNSORTED_PARENTS")
        _require(str(self.version_id) not in parent_ids, "SELF_PARENT")
        if self.artifact_type is ResearchArtifactType.RESEARCH_INPUT:
            _require(
                any(p.version_id.namespace == "data_version" for p in self.parents),
                "DATASET_PARENT_REQUIRED",
            )
        _require(bool(self.files), "ARTIFACT_FILES_REQUIRED")
        _require(all(isinstance(f, ManifestFileEntry) for f in self.files), "INVALID_FILE_ENTRY")
        paths = [f.relative_path for f in self.files]
        _require(paths == sorted(set(paths)), "DUPLICATE_OR_UNSORTED_FILES")
        _sha(self.rules_sha256)
        _sha(self.dependency_lock_sha256)
        _text(self.code_version)
        for gap in self.gaps:
            _text(gap)
        _require(self.gaps == tuple(sorted(set(self.gaps))), "DUPLICATE_OR_UNSORTED_GAPS")

    def content_sha256(self) -> str:
        """Stable file-content identity, independent of attempt/version identity."""
        return _digest([asdict(entry) for entry in self.files])

    def fingerprint(self) -> str:
        """Full manifest identity binds scope, rules, ancestry and execution gaps."""
        return _digest(
            {
                "schema_version": "research-artifact-manifest.v1",
                "artifact_type": self.artifact_type.value,
                "version_id": str(self.version_id),
                "parents": [(str(p.version_id), p.manifest_sha256) for p in self.parents],
                "scope_sha256": self.scope.fingerprint(),
                "rules_sha256": self.rules_sha256,
                "content_sha256": self.content_sha256(),
                "code_version": self.code_version,
                "dependency_lock_sha256": self.dependency_lock_sha256,
                "gaps": self.gaps,
            }
        )
