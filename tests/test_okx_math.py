"""OKX 计算方法单测：强平价公式（对照 OKX 官方公式手算）、资金费、取整、结算时刻。"""
import pytest

from app.okx_math import (
    buffered_liq_price,
    funding_fee,
    liquidation_price,
    next_funding_time,
    round_to_lot,
)
from datetime import datetime, timezone

ENTRY, SIZE, MARGIN, MMR, FEE = 4000.0, 0.5, 20.0, 0.004, 0.0005


def test_liquidation_long_matches_okx_formula():
    # (20 - 4000*0.5) / (0.5*(0.004+0.0005-1)) = -1980 / -0.49775
    expected = -1980.0 / (0.5 * (MMR + FEE - 1.0))
    got = liquidation_price("long", ENTRY, SIZE, MARGIN, MMR, FEE)
    assert got == pytest.approx(expected, rel=1e-12)
    assert got < ENTRY  # 多仓强平价在开仓价下方


def test_liquidation_short_matches_okx_formula():
    # (20 + 4000*0.5) / (0.5*(0.004+0.0005+1)) = 2020 / 0.50225
    expected = 2020.0 / (0.5 * (MMR + FEE + 1.0))
    got = liquidation_price("short", ENTRY, SIZE, MARGIN, MMR, FEE)
    assert got == pytest.approx(expected, rel=1e-12)
    assert got > ENTRY  # 空仓强平价在开仓价上方


def test_margin_consistency_at_liquidation():
    """强平价处：剩余保证金恰好覆盖 MMR×仓位价值 + 平仓手续费。"""
    liq = liquidation_price("long", ENTRY, SIZE, MARGIN, MMR, FEE)
    loss = (ENTRY - liq) * SIZE
    remain = MARGIN - loss
    cover = liq * SIZE * MMR + liq * SIZE * FEE
    assert remain == pytest.approx(cover, rel=1e-9)


def test_buffered_liq_price_direction():
    liq = liquidation_price("long", ENTRY, SIZE, MARGIN, MMR, FEE)
    buf = buffered_liq_price("long", ENTRY, liq, 0.05)
    assert liq < buf < ENTRY  # 多仓缓冲价高于真实强平价
    liq_s = liquidation_price("short", ENTRY, SIZE, MARGIN, MMR, FEE)
    buf_s = buffered_liq_price("short", ENTRY, liq_s, 0.05)
    assert ENTRY < buf_s < liq_s  # 空仓缓冲价低于真实强平价


def test_round_to_lot():
    assert round_to_lot(2.345, 0.01) == pytest.approx(2.34)
    assert round_to_lot(2.345, 0.05) == pytest.approx(2.30)
    assert round_to_lot(0.001, 0.01) == 0.0


def test_funding_fee():
    # 0.5 ETH × 4000 × 0.0001 = 0.2 USDT
    assert funding_fee(0.5, 4000.0, 0.0001) == pytest.approx(0.2)


def test_next_funding_time():
    assert next_funding_time(datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)) == datetime(
        2026, 9, 2, 16, 0, tzinfo=timezone.utc)
    assert next_funding_time(datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)) == datetime(
        2026, 9, 3, 0, 0, tzinfo=timezone.utc)
    assert next_funding_time(datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)) == datetime(
        2026, 9, 2, 8, 0, tzinfo=timezone.utc)
