from __future__ import annotations

import inspect
from collections.abc import Callable

import akshare
import akshare.stock.stock_zh_a_sina as sina_module
import akshare.stock_feature.stock_hist_tx as tencent_module


def _parameter_names(function: Callable[..., object]) -> tuple[str, ...]:
    return tuple(inspect.signature(function).parameters)


def test_installed_akshare_version_and_endpoint_signatures_are_pinned() -> None:
    assert akshare.__version__ == "1.18.83"
    assert _parameter_names(akshare.stock_zh_a_hist_tx) == (
        "symbol",
        "start_date",
        "end_date",
        "adjust",
        "timeout",
    )
    assert _parameter_names(akshare.stock_zh_a_daily) == (
        "symbol",
        "start_date",
        "end_date",
        "adjust",
    )


def test_installed_endpoint_sources_expose_expected_columns_without_pre_close() -> None:
    tencent_source = inspect.getsource(akshare.stock_zh_a_hist_tx)
    sina_source = inspect.getsource(akshare.stock_zh_a_daily)

    assert all(
        f'"{column}"' in tencent_source
        for column in ("date", "open", "close", "high", "low", "volume", "turnover", "amount")
    )
    assert "pre_close" not in tencent_source
    assert all(
        f'"{column}"' in sina_source
        for column in (
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "outstanding_share",
            "turnover",
        )
    )
    assert 'del data_df["prevclose"]' in sina_source


def test_installed_endpoint_sources_disclose_internal_http_shape_without_retry() -> None:
    tencent_source = inspect.getsource(akshare.stock_zh_a_hist_tx)
    tencent_start_source = inspect.getsource(tencent_module.get_tx_start_year)
    sina_source = inspect.getsource(akshare.stock_zh_a_daily)

    assert "get_tx_start_year(symbol=symbol)" in tencent_source
    assert "for year in tqdm(range(range_start, range_end)" in tencent_source
    assert tencent_source.count("requests.get(") == 1
    assert tencent_start_source.count("requests.get(") == 2
    assert "if not demjson.decode" in tencent_start_source
    unadjusted_calls = sina_source.split("r = requests.get(zh_sina_a_stock_hist_url", maxsplit=1)[
        1
    ].split('if adjust == ""', maxsplit=1)[0]
    assert unadjusted_calls.count("requests.get(") == 1
    assert "zh_sina_a_stock_amount_url" in unadjusted_calls
    assert "大量抓取容易封 IP" in sina_source
    for source in (tencent_source, tencent_start_source, sina_source):
        assert "retry" not in source.lower()


def test_installed_tencent_source_exposes_its_symbol_dependent_volume_branch() -> None:
    tencent_source = inspect.getsource(akshare.stock_zh_a_hist_tx)
    assert "volume 统一为股, amount 统一为元" in tencent_source
    assert "not symbol.startswith" in tencent_source
    assert all(prefix in tencent_source for prefix in ("sh688", "sz399", "sh000", "sz000"))
    assert 'big_df["volume"] = big_df["volume"] * 100' in tencent_source
    assert 'big_df["amount"] = big_df["amount"] * 10000' in tencent_source
    assert "zh_sina_a_stock_amount_url" in inspect.getsource(sina_module.stock_zh_a_daily)
