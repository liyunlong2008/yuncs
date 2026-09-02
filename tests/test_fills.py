"""撮合/滑点模型单测。"""
import pytest

from app import fills

BOOK = {
    "bids": [[4000.0, 1.0], [3999.0, 1.0]],
    "asks": [[4001.0, 1.0], [4002.0, 1.0]],
}


def test_depth_fill_buy_taker():
    # 买 0.5 ETH 吃卖一 4001
    assert fills.depth_fill_price(BOOK, "buy", 0.5, 0.0) == pytest.approx(4001.0)


def test_depth_fill_buy_through_levels():
    # 买 1.5 ETH: 4001*1 + 4002*0.5 加权
    assert fills.depth_fill_price(BOOK, "buy", 1.5, 0.0) == pytest.approx(4001.3333, rel=1e-4)


def test_depth_fill_sell_maker_side():
    assert fills.depth_fill_price(BOOK, "sell", 0.5, 0.0) == pytest.approx(4000.0)


def test_depth_fill_slippage_direction():
    buy = fills.depth_fill_price(BOOK, "buy", 0.5, 10.0)
    sell = fills.depth_fill_price(BOOK, "sell", 0.5, 10.0)
    assert buy > 4001.0   # 买价上滑
    assert sell < 4000.0  # 卖价下滑


def test_depth_fill_empty_book():
    assert fills.depth_fill_price({"bids": [], "asks": []}, "buy", 0.5, 0.0) == 0.0


def test_candle_fill_price():
    bar = {"o": 4000.0}
    assert fills.candle_fill_price(bar, "buy", 10.0) == pytest.approx(4004.0)
    assert fills.candle_fill_price(bar, "sell", 10.0) == pytest.approx(3996.0)
    assert fills.candle_fill_price(bar, "buy", 0.0) == pytest.approx(4000.0)
