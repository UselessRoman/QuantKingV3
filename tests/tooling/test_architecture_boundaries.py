from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_architecture_boundaries.py"


def _write(root: Path, relative: str, source: str) -> None:
    target = root / "src" / "quantking" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_checker_accepts_every_explicit_adapter_and_entry_boundary(tmp_path: Path) -> None:
    allowed = {
        "data/adapters/ak.py": "from akshare.stock import history\n",
        "research/adapters/qlib_engine.py": "from qlib.data import D\nimport lightgbm.basic\n",
        "backtest/adapters/engines.py": "import qlib\nfrom backtrader import Cerebro\n",
        "jobs/adapters/persistence/sqlite.py": (
            "from sqlalchemy.orm import Session\nfrom alembic import command\n"
        ),
        "data/adapters/query.py": "import duckdb\nfrom pyarrow import Table\n",
        "web/adapters/compatibility.py": "from fastapi import FastAPI\n",
        "web/app.py": "from fastapi import FastAPI, Request\n",
        "web/schemas.py": "from fastapi import HTTPException\n",
    }
    for relative, source in allowed.items():
        _write(tmp_path, relative, source)

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "architecture boundaries: OK" in result.stdout


def test_checker_rejects_import_and_from_import_with_stable_sorted_output(tmp_path: Path) -> None:
    violations = {
        "portfolio/z_last.py": "import akshare\nfrom fastapi import Request\n",
        "research/service.py": "from qlib.contrib import model\nimport backtrader\n",
        "web/services.py": "from fastapi.requests import Request\nimport sqlalchemy.orm\n",
        "data/service.py": "import duckdb.sqltypes\nfrom lightgbm import Booster\n",
    }
    for relative, source in violations.items():
        _write(tmp_path, relative, source)

    result = _run(tmp_path)

    assert result.returncode == 1
    lines = [line for line in result.stdout.splitlines() if ": forbidden import " in line]
    assert len(lines) == 8
    assert lines == sorted(lines, key=str.casefold)
    assert any("data/service.py:1: forbidden import duckdb" in line for line in lines)
    assert any("portfolio/z_last.py:1: forbidden import akshare" in line for line in lines)
    assert any("web/services.py:1: forbidden import fastapi.Request" in line for line in lines)


def test_checker_rejects_retired_vendors_everywhere_in_active_source(tmp_path: Path) -> None:
    violations = {
        "data/adapters/retired.py": (
            "import xtquant.xtdata\nfrom baostock import query_history_k_data_plus\n"
        ),
        "research/retired.py": "import tushare\nfrom qmt import Client\nimport miniqmt\n",
    }
    for relative, source in violations.items():
        _write(tmp_path, relative, source)

    result = _run(tmp_path)

    assert result.returncode == 1
    lines = [line for line in result.stdout.splitlines() if ": forbidden import " in line]
    assert len(lines) == 5
    assert all("nowhere in active src (retired vendor)" in line for line in lines)
    for vendor in ("baostock", "miniqmt", "qmt", "tushare", "xtquant"):
        assert any(f"forbidden import {vendor} " in line for line in lines)


def test_fastapi_request_is_not_allowed_in_schema_or_web_business_module(tmp_path: Path) -> None:
    _write(tmp_path, "web/schemas.py", "import fastapi.requests\n")
    _write(tmp_path, "web/api/orders.py", "from fastapi import Request\n")
    _write(tmp_path, "web/services.py", "import fastapi\n")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "web/api/orders.py" not in result.stdout
    assert "web/schemas.py:1: forbidden import fastapi.Request" in result.stdout
    assert "web/services.py:1: forbidden import fastapi" in result.stdout


def test_web_must_consume_an_injected_port_instead_of_persistence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "web/api.py",
        "from quantking.jobs.adapters.persistence.sqlite import create_sqlite_engine\n",
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "web/api.py:1: forbidden import jobs.persistence" in result.stdout
    assert "allowed: injected Web application port" in result.stdout
