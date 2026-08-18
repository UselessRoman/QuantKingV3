"""S1-08R3 A: the single formal entry refuses everything it must, pre-call.

The formal entry binds one verified R2 release manifest plus one R3
authorization envelope (marker, authorization id, idempotency key, git HEAD,
code tree SHA, concrete published calendar version). Every guard here fails
closed with a stable code and, critically, with zero provider invocations:
the fake gateways count every call. One happy-path chain proves the entry
itself publishes through the shared EodCommandService/Worker stack on a
temporary data root.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.data.adapters.test_akshare_adapter import FakeFrame, FakeRunner
from tests.integration.data.test_eod_worker_k4 import (
    FakeBaostockGateway,
    _calendar_table,
    _constant,
    _publish_exchange_calendar,
)

from quantking.cli.eod import FormalEntryError, run_formal_entry
from quantking.common.config import AppSettings
from quantking.common.ids import new_entity_id
from quantking.common.paths import RuntimePaths
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_content import canonical_content_sha256
from quantking.data.adapters.release_verifier import verify_manifest_release
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.eod.formal_authorization import (
    build_formal_authorization_envelope,
    compute_code_tree_sha256,
)
from quantking.data.eod.release_manifest import build_release_manifest
from quantking.data.models import DatasetKind, DatasetVersionStatus, DataUpstream
from quantking.jobs.models import JobStatus
from quantking.runtime import EodRuntime, build_eod_runtime

_NOW = UtcTimestamp(datetime(2026, 8, 17, 10, 0, tzinfo=UTC))
_SYMBOLS = ("000001.SZ", "600000.SH")
_START = date(2026, 6, 11)
_END = date(2026, 6, 30)


class _FormalAkshareModule:
    """AKShare-shaped fake serving the two formal symbols."""

    def __init__(self, upstream: DataUpstream) -> None:
        self.__version__ = "1.18.83"
        self.upstream = upstream
        self.calls: list[dict[str, object]] = []

    def _call(self, endpoint: str, kwargs: dict[str, object]) -> FakeFrame:
        self.calls.append({"endpoint": endpoint, "kwargs": kwargs})
        current = date.fromisoformat(str(kwargs["start_date"]))
        end = date.fromisoformat(str(kwargs["end_date"]))
        # Unit policy (akshare-1.18.83-returned-units.v1): only sz000* comes
        # back from TENCENT in lots (x100 shares); every other symbol and all
        # SINA responses are already shares. BaoStock uses 10_000 shares, so
        # both roles agree with A inside the K4 thresholds and C stays at
        # zero actual calls.
        symbol = str(kwargs.get("symbol", ""))
        in_lots = self.upstream is DataUpstream.TENCENT and symbol.startswith("sz000")
        volume = 100 if in_lots else 10_000
        rows: list[dict[str, object]] = []
        while current <= end:
            rows.append(
                {
                    "date": current.isoformat(),
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "volume": volume,
                    "amount": 100_000.0,
                }
            )
            current = date.fromordinal(current.toordinal() + 1)
        return FakeFrame(rows)

    def stock_zh_a_hist_tx(self, **kwargs: object) -> FakeFrame:
        return self._call("stock_zh_a_hist_tx", kwargs)  # type: ignore[arg-type]

    def stock_zh_a_daily(self, **kwargs: object) -> FakeFrame:
        return self._call("stock_zh_a_daily", kwargs)  # type: ignore[arg-type]


class _ExplodingBaostockGateway(FakeBaostockGateway):
    """BaoStock fake that fails every fetch after the call is counted."""

    def fetch(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls.append({"args": args, "kwargs": kwargs})
        from quantking.data.adapters.sources.contracts import (
            SourceAdapterError,
            SourceErrorCode,
        )

        raise SourceAdapterError(
            SourceErrorCode.NETWORK,
            "formal test injection",
            retryable=False,
        )


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        paths=RuntimePaths(
            code_root=Path.cwd(),
            data_root=(tmp_path / "runtime-data").resolve(),
            offsite_backup_root=(tmp_path / "offsite").resolve(),
        ),
        web_host="127.0.0.1",
        web_port=8765,
        sqlite_busy_timeout_ms=250,
        sqlite_max_attempts=7,
        sqlite_retry_base_delay_ms=10,
        sqlite_retry_cap_delay_ms=160,
        sqlite_retry_jitter_ratio=0.0,
        job_heartbeat_min_interval_ms=1000,
        job_progress_min_interval_ms=250,
        eod_symbols=_SYMBOLS,
    )


def _gateways(baostock: FakeBaostockGateway | None = None) -> tuple[dict, list]:
    bao = baostock or FakeBaostockGateway(_constant(10.0))
    modules = {
        upstream: _FormalAkshareModule(upstream)
        for upstream in (DataUpstream.TENCENT, DataUpstream.SINA)
    }
    gateways = {
        DataUpstream.BAOSTOCK: bao,
        **{
            upstream: AkshareMarketDataGateway(
                AkshareDailyBarAdapter(
                    upstream,
                    runner=FakeRunner(module),
                    module_loader=lambda module=module: module,
                    clock=lambda: _NOW,
                )
            )
            for upstream, module in modules.items()
        },
    }
    return gateways, list(modules.values())


class _FormalEnv:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.settings = _settings(tmp_path)
        # Fake repository: code tree + git HEAD read by the formal entry.
        self.code_root = tmp_path / "repo"
        source = self.code_root / "src" / "quantking"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("", encoding="utf-8")
        (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        git = self.code_root / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.head = "9f5b9ea0deadbeef00c0ffee"
        (git / "refs" / "heads" / "main").write_text(self.head + "\n", encoding="utf-8")
        self.code_tree_sha = compute_code_tree_sha256(source)
        # Publish a calendar version in a runtime on the same data root so
        # the envelope can bind a real PUBLISHED calendar id and content SHA.
        calendar_runtime = build_eod_runtime(self.settings, gateways=_gateways()[0])
        self.calendar_table = _calendar_table()
        self.calendar_sha = canonical_content_sha256(
            DatasetKind.EXCHANGE_CALENDAR, [self.calendar_table]
        )
        self.calendar_version_id = new_entity_id("data_version")
        assert _publish_exchange_calendar(
            calendar_runtime,
            version_id=self.calendar_version_id,
            table=self.calendar_table,
            content_sha=self.calendar_sha,
        ) == str(self.calendar_version_id)
        self.manifest = build_release_manifest(
            symbols=_SYMBOLS,
            target_start=_START,
            target_end=_END,
            lookback_days=15,
            calendar_version_id="exchange-calendar-2026h2.v1",
            calendar_sha256=self.calendar_sha,
            source_route_version="BAO-TX-SINA.k4.v1",
            adapter_version="baostock-akshare-daily-bar.k4.v1",
            code_version="s1-08r3.v1",
            quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
            threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
            resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
            unit_policy_versions={"baostock": "b", "akshare": "a"},
        )
        self.manifest_path = tmp_path / "release-manifest.json"
        self.manifest_path.write_text(
            json.dumps(self.manifest.to_dict(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        self.attempts_root = tmp_path / "formal-attempts"

    def _build_envelope(
        self,
        *,
        calendar_version_id: str | None = None,
        code_tree_sha256: str | None = None,
        git_head: str | None = None,
        calendar_label_only: bool = False,
    ):
        return build_formal_authorization_envelope(
            manifest=self.manifest,
            authorization_marker="S1-08R3-E1-FORMAL-RELEASE-V1",
            authorization_id="S1-08R3-E1-4f7c2a90b1d3",
            idempotency_key="s1-08r3-formal-20260817-v1",
            git_head=git_head or self.head,
            code_tree_sha256=code_tree_sha256 or self.code_tree_sha,
            universe_version="configs/default.toml@9f5b9ea",
            universe_sha256="c" * 64,
            universe_total=len(_SYMBOLS),
            calendar_source="published-warehouse-exchange-calendar",
            calendar_version_id=(
                "exchange-calendar-2026h2.v1"
                if calendar_label_only
                else (calendar_version_id or str(self.calendar_version_id))
            ),
            base_coverage_binding="none: first formal release without a base",
        )

    def token(self) -> str:
        """ADR-028 content-bound token for the default envelope."""

        return self._build_envelope().authorization_token()

    def envelope_path(
        self,
        tmp_path: Path,
        *,
        calendar_version_id: str | None = None,
        release_manifest_sha256: str | None = None,
        code_tree_sha256: str | None = None,
        git_head: str | None = None,
        calendar_label_only: bool = False,
    ) -> Path:
        envelope = self._build_envelope(
            calendar_version_id=calendar_version_id,
            code_tree_sha256=code_tree_sha256,
            git_head=git_head,
            calendar_label_only=calendar_label_only,
        )
        if release_manifest_sha256 is not None:
            payload = json.loads(envelope.canonical_json())
            payload["release_manifest_sha256"] = release_manifest_sha256
            envelope_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        else:
            envelope_json = envelope.canonical_json()
        digest = hex(abs(hash(envelope_json)))[2:10]
        path = tmp_path / f"envelope-{digest}.json"
        path.write_text(envelope_json, encoding="utf-8")
        return path

    def run(
        self,
        *,
        authorization: str | None,
        envelope_path: Path,
        gateways: dict | None = None,
        git_head_reader=None,
    ) -> dict[str, object]:
        return run_formal_entry(
            manifest_path=self.manifest_path,
            envelope_path=envelope_path,
            attempts_root=self.attempts_root,
            authorization=authorization,
            settings=self.settings,
            code_root=self.code_root,
            gateways=gateways,
            git_head_reader=git_head_reader,
            declared_data_root=self.settings.paths.data_root,
            attempts_root_base=self.root,
        )


@pytest.fixture()
def formal(tmp_path: Path) -> _FormalEnv:
    return _FormalEnv(tmp_path)


def _published_versions(runtime: EodRuntime) -> list[str]:
    with runtime.data_uow_factory() as uow:
        versions = uow.data.list_versions(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            status=DatasetVersionStatus.PUBLISHED,
        )
    return [str(item.version_id) for item in versions]


def test_formal_entry_publishes_through_the_shared_stack(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    gateways, modules = _gateways()
    payload = formal.run(
        authorization=formal.token(),
        envelope_path=formal.envelope_path(tmp_path),
        gateways=gateways,
    )
    assert payload["status"] == "SUCCEEDED"
    assert payload["created"] is True
    budgets = formal.manifest.budgets
    assert len(gateways[DataUpstream.BAOSTOCK].calls) == budgets["a_baostock_endpoints"]
    assert len(modules[0].calls) == budgets["b_tencent_endpoints"]
    assert modules[1].calls == []  # C is never invoked when A/B agree
    assert (formal.attempts_root / "S1-08R3-E1-4f7c2a90b1d3" / "completed.json").is_file()

    # Read-only verification discipline (same as the R2 E2E): let the entry's
    # internal runtime be collected, checkpoint WAL away, switch to DELETE
    # journal mode, then verify with the immutable connection.
    import gc
    import sqlite3

    gc.collect()
    data_root = formal.settings.paths.data_root
    database = data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as read_only:
        rows = read_only.execute(
            "SELECT version_id FROM dataset_versions"
            " WHERE dataset = 'daily_bar_unadjusted' AND status = 'PUBLISHED'"
        ).fetchall()
    assert len(rows) == 1
    verify_manifest_release(
        data_root=data_root,
        version_id=rows[0][0],
        manifest=formal.manifest,
    )
    runtime = build_eod_runtime(formal.settings, gateways=_gateways()[0])
    assert _published_versions(runtime) == [rows[0][0]]


_AUTH_MARKER = "S1-08R3-E1-FORMAL-RELEASE-V1"
_AUTH_ID = "S1-08R3-E1-4f7c2a90b1d3"


def _mutated_token(formal: _FormalEnv, **mutate: object) -> str:
    """ADR-028 token whose SHA matches an exactly-mutated envelope content."""

    import hashlib

    envelope = formal._build_envelope(
        **{key: value for key, value in mutate.items() if key != "release_manifest_sha256"}
    )
    payload = json.loads(envelope.canonical_json())
    if "release_manifest_sha256" in mutate:
        payload["release_manifest_sha256"] = mutate["release_manifest_sha256"]
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_AUTH_MARKER}:{_AUTH_ID}:{sha}"


@pytest.mark.parametrize(
    ("authorization", "mutate", "code"),
    (
        (None, None, "FORMAL_AUTHORIZATION_REQUIRED"),
        ("wrong:token:shape", None, "FORMAL_AUTHORIZATION_MISMATCH"),
        # ADR-028: a legacy two-part marker/id token is refused outright.
        (
            _AUTH_MARKER + ":" + _AUTH_ID,
            {"code_tree_sha256": "0" * 64},
            "FORMAL_AUTHORIZATION_REQUIRED",
        ),
        (
            _AUTH_MARKER + ":" + _AUTH_ID + ":" + "0" * 64,
            None,
            "FORMAL_AUTHORIZATION_MISMATCH",
        ),
        (
            "mutated",
            {"release_manifest_sha256": "0" * 64},
            "FORMAL_ENVELOPE_MANIFEST_MISMATCH",
        ),
        (
            "mutated",
            {"code_tree_sha256": "0" * 64},
            "FORMAL_CODE_TREE_DRIFT",
        ),
        (
            "mutated",
            {"git_head": "0000000"},
            "FORMAL_GIT_HEAD_DRIFT",
        ),
    ),
)
def test_formal_entry_refuses_tampering_with_zero_provider_calls(
    formal: _FormalEnv,
    tmp_path: Path,
    authorization: str | None,
    mutate: dict | None,
    code: str,
) -> None:
    gateways, modules = _gateways()
    if authorization == "mutated":
        authorization = _mutated_token(formal, **(mutate or {}))
    envelope_path = formal.envelope_path(tmp_path, **(mutate or {}))
    with pytest.raises(FormalEntryError) as error:
        formal.run(
            authorization=authorization,
            envelope_path=envelope_path,
            gateways=gateways,
        )
    assert error.value.code == code
    assert gateways[DataUpstream.BAOSTOCK].calls == []
    assert all(module.calls == [] for module in modules)
    assert not formal.attempts_root.exists()


def test_formal_entry_refuses_a_static_calendar_label(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    gateways, _modules = _gateways()
    with pytest.raises(FormalEntryError) as error:
        formal.run(
            authorization=_mutated_token(formal, calendar_label_only=True),
            envelope_path=formal.envelope_path(tmp_path, calendar_label_only=True),
            gateways=gateways,
        )
    assert error.value.code == "FORMAL_CALENDAR_BINDING_INVALID"
    assert gateways[DataUpstream.BAOSTOCK].calls == []


def test_formal_entry_refuses_boundary_drift_before_runtime(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    """ADR-028: write-path/calendar-source/data-root/attempts-root guards."""

    from quantking.data.eod.formal_authorization import (
        FormalAuthorizationError,
        assert_formal_entry_boundary,
    )

    gateways, modules = _gateways()
    good = {
        "settings_data_root": formal.settings.paths.data_root,
        "declared_data_root": formal.settings.paths.data_root,
        "attempts_root": formal.attempts_root,
        "attempts_root_base": formal.root,
    }
    envelope = formal._build_envelope()
    # Every drift fails closed with its stable code.
    from dataclasses import replace as _replace

    from quantking.data.eod.formal_authorization import AllowedWritePath

    widened = _replace(
        envelope,
        allowed_write_paths=(
            *envelope.allowed_write_paths,
            AllowedWritePath(relative_path="warehouse/extra", maximum_entries=1),
        ),
    )
    with pytest.raises(FormalAuthorizationError, match="ENVELOPE_WRITE_PATHS_INVALID"):
        assert_formal_entry_boundary(widened, **good)
    relabeled = _replace(envelope, calendar_source="unknown-calendar-source")
    with pytest.raises(FormalAuthorizationError, match="CALENDAR_SOURCE_INVALID"):
        assert_formal_entry_boundary(relabeled, **good)
    with pytest.raises(FormalAuthorizationError, match="DATA_ROOT_INVALID"):
        assert_formal_entry_boundary(
            envelope,
            **{**good, "declared_data_root": formal.root / "other-root"},
        )
    with pytest.raises(FormalAuthorizationError, match="ATTEMPTS_ROOT_INVALID"):
        assert_formal_entry_boundary(
            envelope,
            **{**good, "attempts_root": formal.root.parent / "escape"},
        )
    # Nothing above constructed a runtime or called a provider.
    assert gateways[DataUpstream.BAOSTOCK].calls == []
    assert all(module.calls == [] for module in modules)
    assert not formal.attempts_root.exists()


def test_formal_entry_refuses_duplicate_and_reordered_write_paths(
    formal: _FormalEnv,
) -> None:
    """R3D2 P1: the write-path template comparison must keep the full sequence.

    A dict comparison folds duplicates and ignores order, so an envelope that
    repeats one allowed entry or swaps two entries would compare equal to the
    frozen template. The guard must compare the complete ordered tuple
    sequence instead (S1-08R3F2).
    """

    from dataclasses import replace as _replace

    from quantking.data.eod.formal_authorization import (
        FormalAuthorizationError,
        assert_formal_entry_boundary,
    )

    gateways, modules = _gateways()
    good = {
        "settings_data_root": formal.settings.paths.data_root,
        "declared_data_root": formal.settings.paths.data_root,
        "attempts_root": formal.attempts_root,
        "attempts_root_base": formal.root,
    }
    envelope = formal._build_envelope()
    # The untouched template in its original frozen order still passes.
    assert_formal_entry_boundary(envelope, **good)
    # Duplicating an existing entry at the end of the list must be rejected.
    duplicated = _replace(
        envelope,
        allowed_write_paths=(*envelope.allowed_write_paths, envelope.allowed_write_paths[0]),
    )
    with pytest.raises(FormalAuthorizationError, match="ENVELOPE_WRITE_PATHS_INVALID"):
        assert_formal_entry_boundary(duplicated, **good)
    # Swapping two legitimate entries (same set, different order) must be too.
    swapped = list(envelope.allowed_write_paths)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    reordered = _replace(envelope, allowed_write_paths=tuple(swapped))
    with pytest.raises(FormalAuthorizationError, match="ENVELOPE_WRITE_PATHS_INVALID"):
        assert_formal_entry_boundary(reordered, **good)
    # Every rejection happened before any runtime, provider call or ledger.
    assert gateways[DataUpstream.BAOSTOCK].calls == []
    assert all(module.calls == [] for module in modules)
    assert not formal.attempts_root.exists()


def test_replayed_authorization_is_refused_before_any_new_call(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    token = formal.token()
    first_gateways, _first_modules = _gateways()
    formal.run(
        authorization=token,
        envelope_path=formal.envelope_path(tmp_path),
        gateways=first_gateways,
    )
    second_gateways, second_modules = _gateways()
    with pytest.raises(FormalEntryError) as error:
        formal.run(
            authorization=token,
            envelope_path=formal.envelope_path(tmp_path),
            gateways=second_gateways,
        )
    assert error.value.code == "AUTHORIZATION_ALREADY_CONSUMED"
    assert second_gateways[DataUpstream.BAOSTOCK].calls == []
    assert all(module.calls == [] for module in second_modules)


def test_cross_process_worker_refuses_manifest_drift(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
    from quantking.data.eod.service import EodExecutionConfig

    # Enqueue with the manifest-bound config (what eod-formal-run does)...
    manifest_config = EodExecutionConfig.from_release_manifest(formal.manifest)
    enqueue_runtime = build_eod_runtime(
        formal.settings,
        gateways=_gateways()[0],
        execution_config=manifest_config,
    )
    outcome = enqueue_runtime.eod_commands.enqueue(
        EodRunRequest(
            start_date=_START,
            end_date=_END,
            datasets=("daily_bar_unadjusted",),
            publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
            base_versions={},
        ),
        idempotency_key="s1-08r3-drift-probe-20260817-v1",
        requester="drift-probe",
    )
    # ...then execute with a runtime rebuilt from the broad production config
    # (what a bare `worker-run` process constructs from configs/default.toml).
    gateways_b, modules_b = _gateways()
    broad_runtime = build_eod_runtime(
        formal.settings,
        gateways=gateways_b,
    )
    assert broad_runtime.eod_handler._expected_release_manifest_sha256 is None
    from quantking.data.adapters.eod_worker import EodExecutionError

    with pytest.raises(EodExecutionError, match="RELEASE_MANIFEST_DRIFT"):
        broad_runtime.worker.run(outcome.job_id)
    job = broad_runtime.jobs.get_job(outcome.job_id)
    assert job.status is JobStatus.FAILED
    assert job.failure is not None
    assert job.failure.error_type == "EodExecutionError"
    assert gateways_b[DataUpstream.BAOSTOCK].calls == []
    assert all(module.calls == [] for module in modules_b)
    runtime_check = build_eod_runtime(formal.settings, gateways=_gateways()[0])
    assert _published_versions(runtime_check) == []


def test_failing_providers_consume_authorization_without_publishing(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    # Both primaries must fail: with only A down, C legally replaces it and
    # B/C consensus can still publish (K4 design), which is not a failure.
    exploding_bao = _ExplodingBaostockGateway(_constant(10.0))

    class _ExplodingTencentModule(_FormalAkshareModule):
        def _call(self, endpoint: str, kwargs: dict[str, object]) -> FakeFrame:
            from quantking.data.adapters.sources.contracts import (
                SourceAdapterError,
                SourceErrorCode,
            )

            self.calls.append({"endpoint": endpoint, "kwargs": kwargs})
            raise SourceAdapterError(
                SourceErrorCode.NETWORK,
                "formal test injection",
                retryable=False,
            )

    tencent = _ExplodingTencentModule(DataUpstream.TENCENT)
    gateways: dict[DataUpstream, object] = {
        DataUpstream.BAOSTOCK: exploding_bao,
        DataUpstream.TENCENT: AkshareMarketDataGateway(
            AkshareDailyBarAdapter(
                DataUpstream.TENCENT,
                runner=FakeRunner(tencent),
                module_loader=lambda: tencent,
                clock=lambda: _NOW,
            )
        ),
        DataUpstream.SINA: AkshareMarketDataGateway(
            AkshareDailyBarAdapter(
                DataUpstream.SINA,
                runner=FakeRunner(_FormalAkshareModule(DataUpstream.SINA)),
                module_loader=lambda: _FormalAkshareModule(DataUpstream.SINA),
                clock=lambda: _NOW,
            )
        ),
    }
    with pytest.raises(FormalEntryError) as error:
        formal.run(
            authorization=formal.token(),
            envelope_path=formal.envelope_path(tmp_path),
            gateways=gateways,
        )
    assert error.value.code == "FORMAL_EXECUTION_FAILED"
    failed = formal.attempts_root / "S1-08R3-E1-4f7c2a90b1d3" / "failed.json"
    assert failed.is_file()
    runtime = build_eod_runtime(formal.settings, gateways=_gateways()[0])
    assert _published_versions(runtime) == []


def test_same_idempotency_key_with_a_different_manifest_is_rejected(
    formal: _FormalEnv,
) -> None:
    from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
    from quantking.data.eod.release_manifest import build_release_manifest
    from quantking.data.eod.service import EodExecutionConfig

    def _config(target_end: date):
        manifest = build_release_manifest(
            symbols=formal.manifest.symbols,
            target_start=_START,
            target_end=target_end,
            lookback_days=15,
            calendar_version_id="exchange-calendar-2026h2.v1",
            calendar_sha256=formal.calendar_sha,
            source_route_version="BAO-TX-SINA.k4.v1",
            adapter_version="baostock-akshare-daily-bar.k4.v1",
            code_version="s1-08r3.v1",
            quality_rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
            threshold_rule_version="comparison-thresholds.daily-bar-unadjusted.k4.v1",
            resolution_rule_version="source-resolution.daily-bar-unadjusted.k4.v1",
            unit_policy_versions={"baostock": "b", "akshare": "a"},
        )
        return EodExecutionConfig.from_release_manifest(manifest)

    first_runtime = build_eod_runtime(
        formal.settings,
        gateways=_gateways()[0],
        execution_config=_config(_END),
    )
    request = EodRunRequest(
        start_date=_START,
        end_date=_END,
        datasets=("daily_bar_unadjusted",),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
    )
    first_runtime.eod_commands.enqueue(
        request,
        idempotency_key="s1-08r3-shared-key-20260817-v1",
        requester="shared-key-probe",
    )
    second_runtime = build_eod_runtime(
        formal.settings,
        gateways=_gateways()[0],
        execution_config=_config(date(2026, 7, 31)),
    )
    with pytest.raises(Exception) as rejected:
        second_runtime.eod_commands.enqueue(
            request,
            idempotency_key="s1-08r3-shared-key-20260817-v1",
            requester="shared-key-probe",
        )
    del rejected
    assert _published_versions(first_runtime) == []
