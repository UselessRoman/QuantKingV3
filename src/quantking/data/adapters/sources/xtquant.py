"""Delayed, explicit-root XTQuant read-only market-data gateway."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from quantking.common.paths import is_reparse_point
from quantking.data.adapters.sources.capabilities import capability_for
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    CodeFormat,
    DateFormat,
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
    classify_source_error,
)
from quantking.data.incremental import FetchChunk, validate_fetch_chunk
from quantking.data.models import DatasetKind

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _is_below(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


class ExplicitXtQuantModuleLoader:
    """Load only ``xtquant.xtdata`` from one configured site-packages root."""

    def __init__(self, site_packages_root: Path) -> None:
        if not isinstance(site_packages_root, Path) or not site_packages_root.is_absolute():
            raise ValueError("XTQuant site-packages root must be explicit and absolute")
        try:
            root = site_packages_root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("XTQuant site-packages root is unavailable") from error
        if not root.is_dir() or root.is_symlink() or is_reparse_point(root):
            raise ValueError("XTQuant site-packages root must be a real directory")
        package = root / "xtquant"
        if (
            not package.is_dir()
            or not (package / "__init__.py").is_file()
            or package.is_symlink()
            or is_reparse_point(package)
        ):
            raise ValueError("XTQuant package is missing from explicit root")
        self._root = root

    def load(self) -> ModuleType:
        """Delay the import and restore ``sys.path`` immediately afterward."""
        previous = {
            name: module
            for name, module in sys.modules.items()
            if name == "xtquant" or name.startswith("xtquant.")
        }
        previous_state = {name: dict(vars(module)) for name, module in previous.items()}
        try:
            for existing_module in previous.values():
                module_file = getattr(existing_module, "__file__", None)
                if not isinstance(module_file, str) or not _is_below(
                    Path(module_file).resolve(strict=False), self._root
                ):
                    raise SourceAdapterError(SourceErrorCode.CAPABILITY_UNAVAILABLE)
            original_path = tuple(sys.path)
            try:
                sys.path.insert(0, str(self._root))
                importlib.invalidate_caches()
                module = importlib.import_module("xtquant.xtdata")
            finally:
                sys.path[:] = original_path
            module_file = getattr(module, "__file__", None)
            if not isinstance(module_file, str) or not _is_below(
                Path(module_file).resolve(strict=False), self._root
            ):
                raise SourceAdapterError(SourceErrorCode.CAPABILITY_UNAVAILABLE)
            trading_name = ".".join(("xtquant", "xt" + "trader"))
            if trading_name in sys.modules:
                raise SourceAdapterError(SourceErrorCode.CAPABILITY_UNAVAILABLE)
            return module
        except Exception as error:
            self._restore_modules(previous, previous_state)
            mapped = classify_source_error(error)
            if mapped is error:
                raise
            raise mapped from error

    @staticmethod
    def _restore_modules(
        previous: Mapping[str, ModuleType],
        previous_state: Mapping[str, dict[str, object]],
    ) -> None:
        for name in tuple(sys.modules):
            if (name == "xtquant" or name.startswith("xtquant.")) and name not in previous:
                sys.modules.pop(name, None)
        for name, module in previous.items():
            sys.modules[name] = module
            module.__dict__.clear()
            module.__dict__.update(previous_state[name])
        importlib.invalidate_caches()


def _days(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _trading_date(value: object) -> date:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(_SHANGHAI).date()
    except (OSError, OverflowError, ValueError) as error:
        raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT) from error


class XtQuantMarketDataGateway:
    """Narrow source port backed only by evidence-approved public interfaces."""

    def __init__(self, *, module_loader: Callable[[], object]) -> None:
        if not callable(module_loader):
            raise TypeError("module_loader must be callable")
        self._module_loader = module_loader

    def fetch(self, chunk: FetchChunk) -> SupplierResponse:
        validate_fetch_chunk(chunk)
        capability = capability_for(chunk.dataset)
        if not capability.available:
            raise SourceAdapterError(SourceErrorCode.CAPABILITY_UNAVAILABLE)
        if chunk.dataset is not DatasetKind.EXCHANGE_CALENDAR:
            raise SourceAdapterError(SourceErrorCode.CAPABILITY_UNAVAILABLE)
        try:
            module = self._module_loader()
            function = getattr(module, capability.public_interface, None)
            if not callable(function):
                raise SourceAdapterError(SourceErrorCode.CAPABILITY_UNAVAILABLE)
            records: list[dict[str, object]] = []
            raw_results: dict[str, list[int]] = {}
            empty_markets: list[str] = []
            for market in chunk.scope:
                returned = function(
                    market,
                    start_time=chunk.start.value.strftime("%Y%m%d"),
                    end_time=chunk.end.value.strftime("%Y%m%d"),
                    count=-1,
                )
                if not isinstance(returned, (list, tuple)):
                    raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
                timestamps = list(returned)
                open_dates = {_trading_date(value) for value in timestamps}
                if len(open_dates) != len(timestamps):
                    raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
                if any(
                    value < chunk.start.value or value > chunk.end.value for value in open_dates
                ):
                    raise SourceAdapterError(SourceErrorCode.RESPONSE_CONTRACT)
                raw_results[market] = [
                    value
                    for value in timestamps
                    if isinstance(value, int) and not isinstance(value, bool)
                ]
                if not timestamps:
                    empty_markets.append(market)
                    continue
                for calendar_date in _days(chunk.start.value, chunk.end.value):
                    records.append(
                        {
                            "market": market,
                            "calendar_date": calendar_date.isoformat(),
                            "open_flag": calendar_date in open_dates,
                        }
                    )
            provider_payload = {
                "interface": capability.public_interface,
                "request": {
                    "scope": list(chunk.scope),
                    "start_time": chunk.start.value.strftime("%Y%m%d"),
                    "end_time": chunk.end.value.strftime("%Y%m%d"),
                    "count": -1,
                },
                "response": raw_results,
            }
            contract = SupplierResponseContract(
                dataset=DatasetKind.EXCHANGE_CALENDAR,
                contract_version="xtquant-public-source-20260718.v1",
                required_fields=("market", "calendar_date", "open_flag"),
                code_format=CodeFormat.CANONICAL,
                date_format=DateFormat.ISO_YYYY_MM_DD,
                exchange_map={"SZ": "SZ", "SH": "SH", "BJ": "BJ"},
                volume_unit=VolumeUnit.UNVERIFIED,
                amount_unit=AmountUnit.UNVERIFIED,
                point_in_time=False,
                unadjusted=False,
                official_status=False,
                adjustment_formula=None,
                semantics_verified=True,
            )
            complete = not empty_markets or not records
            return SupplierResponse.create_bound(
                chunk=chunk,
                records=tuple(records),
                contract=contract,
                complete=complete,
                partial_reason=(None if complete else SourceErrorCode.PARTIAL_RESPONSE.value),
                provider_payload=provider_payload,
            )
        except SourceAdapterError:
            raise
        except Exception as error:
            mapped = classify_source_error(error)
            raise mapped from error


__all__ = ["ExplicitXtQuantModuleLoader", "XtQuantMarketDataGateway"]
