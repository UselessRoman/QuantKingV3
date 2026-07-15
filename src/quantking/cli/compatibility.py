"""One-process offline compatibility probe orchestration and CLI."""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from quantking.backtest.adapters.compatibility import probe_backtrader
from quantking.backtest.adapters.qlib_probe import probe_qlib
from quantking.common.compatibility import ProbeReport, ProbeResult, ProbeStatus
from quantking.common.errors import InvalidPathError
from quantking.common.paths import resolve_under
from quantking.common.sqlite_probe import probe_sqlite
from quantking.data.adapters.compatibility import probe_duckdb
from quantking.research.adapters.compatibility import probe_lightgbm
from quantking.web.adapters.compatibility import probe_fastapi

ProbeFunction = Callable[[Path], ProbeResult]

OFFLINE_PROBES: tuple[ProbeFunction, ...] = (
    probe_sqlite,
    probe_duckdb,
    probe_lightgbm,
    probe_backtrader,
    probe_fastapi,
    probe_qlib,
)


def run_offline_probe(root: Path) -> ProbeReport:
    """Run every offline dependency sequentially in this exact Python process."""
    results = tuple(probe(root) for probe in OFFLINE_PROBES)
    return ProbeReport.from_results(results)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline suite and emit one JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="relative JSON output path managed under the current directory",
    )
    args = parser.parse_args(argv)
    output: Path | None = None
    if args.output is not None:
        if args.output.is_absolute() or args.output.drive or ".." in args.output.parts:
            parser.error("--output must be relative to the managed current directory")
        if args.output.suffix.casefold() != ".json":
            parser.error("--output must name a JSON file")
        try:
            output = resolve_under(Path.cwd().resolve(), args.output)
        except InvalidPathError:
            parser.error("--output must remain inside the managed current directory")
    with tempfile.TemporaryDirectory(prefix="quantking-compatibility-") as temporary:
        report = run_offline_probe(Path(temporary))
    rendered = f"{report.to_json()}\n"
    if output is None:
        print(report.to_json())
    else:
        output.write_text(rendered, encoding="utf-8", newline="\n")
    return 0 if report.status is ProbeStatus.PASSED else 1
