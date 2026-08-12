from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _isolated_contract() -> dict[str, object]:
    script = r"""
import inspect
import json
from importlib.metadata import metadata, version
import baostock
import baostock.common.contants as constants
import baostock.common.context as context
import baostock.data.resultset as resultset
import baostock.security.history as history
import baostock.util.socketutil as socketutil

socket_source = inspect.getsource(socketutil)
paging_source = inspect.getsource(resultset.ResultData.next)
print(json.dumps({
    "distribution_version": version("baostock"),
    "runtime_version": baostock.__version__,
    "license": metadata("baostock")["License"],
    "login_parameters": list(inspect.signature(baostock.login).parameters),
    "query_parameters": list(inspect.signature(baostock.query_history_k_data_plus).parameters),
    "global_socket": "global default_socket" in inspect.getsource(context),
    "page_count_2000": "BAOSTOCK_PER_PAGE_COUNT = 2000" in inspect.getsource(constants),
    "has_default_timeout": "socket.setdefaulttimeout" in socket_source,
    "has_socket_timeout": ".settimeout(" in socket_source,
    "blocking_connect": "mySockect.connect" in socket_source,
    "blocking_recv": "default_socket.recv(8192)" in socket_source,
    "swallows_socket_exception": "except Exception" in socket_source,
    "first_page_query": "return __query_history_k_data_plus_page(1" in inspect.getsource(
        history.query_history_k_data_plus
    ),
    "paging_threshold": "if len(self.data) < cons.BAOSTOCK_PER_PAGE_COUNT" in paging_source,
    "paging_none_failure": "receive_data is None" in paging_source,
    "paging_false_failure": "return False" in paging_source,
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def test_locked_baostock_distribution_and_public_daily_bar_contract_is_isolated() -> None:
    assert "baostock" not in sys.modules
    contract = _isolated_contract()
    assert "baostock" not in sys.modules
    assert contract["distribution_version"] == "0.9.3"
    assert contract["runtime_version"] == "00.9.30"
    assert contract["license"] == "BSD License"
    assert contract["login_parameters"] == ["user_id", "password"]
    assert contract["query_parameters"] == [
        "code",
        "fields",
        "start_date",
        "end_date",
        "frequency",
        "adjustflag",
    ]


def test_locked_baostock_source_requires_process_level_timeout_isolated() -> None:
    contract = _isolated_contract()
    assert contract["global_socket"] is True
    assert contract["page_count_2000"] is True
    assert contract["has_default_timeout"] is False
    assert contract["has_socket_timeout"] is False
    assert contract["blocking_connect"] is True
    assert contract["blocking_recv"] is True
    assert contract["swallows_socket_exception"] is True
    assert contract["first_page_query"] is True
    assert contract["paging_threshold"] is True
    assert contract["paging_none_failure"] is True
    assert contract["paging_false_failure"] is True


def test_lock_records_exact_official_baostock_artifact_hashes() -> None:
    lock = (Path(__file__).parents[2] / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "baostock"\nversion = "0.9.3"' in lock
    assert "sha256:acbd19403285bc4e254cee8297cf0e2646ae2276e5af7e549deed3988ab02293" in lock
    assert "sha256:16699d82d05037a8c133577fcdeb9ac0d5a7f31edc2432c4e883004e0a95e3f7" in lock
