"""OKX 计算方法单测：强平价公式（对照 OKX 官方公式手算）、资金费、取整、结算时刻。"""
import pytest

from app.okx_math import (
    aggregate_closed,
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


# ---------- aggregate_closed：15m -> 1H 聚合（UTC 桶对齐，丢弃进行中桶） ----------

M15, H1 = 900_000, 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC（整点对齐）


def bar(ts, o, h, l, c, v=1.0):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v}


def fifteen(offset_m, o, h, l, c, v=1.0):
    """T0 起第 offset_m 根 15m bar（offset_m=0 即 00:00-00:15）。"""
    return bar(T0 + offset_m * M15, o, h, l, c, v)


def test_aggregate_drops_incomplete_last_bucket():
    # 只有 00:00~00:45 四根（1H 未收满）：00:00 桶被 01:00 前的数据"顶出"才算完成
    bars = [fifteen(0, 100, 104, 99, 103, 2.0),
            fifteen(1, 103, 105, 101, 102, 1.0),
            fifteen(2, 102, 103, 98, 99, 3.0),
            fifteen(3, 99, 101, 97, 100, 0.5)]
    out = aggregate_closed(bars, H1)
    assert out == []  # 00:00 桶尚未收满，不能当历史用


def test_aggregate_bucket_boundary_and_ohlc():
    # 出现 01:00 bar -> 00:00 桶收满（o 首根/h 最高/l 最低/c 末根/v 求和）
    bars = [fifteen(0, 100, 104, 99, 103, 2.0),
            fifteen(1, 103, 105, 101, 102, 1.0),
            fifteen(2, 102, 103, 98, 99, 3.0),
            fifteen(3, 99, 101, 97, 100, 0.5),
            fifteen(4, 100, 106, 99, 105, 1.5)]  # 01:00 开新桶
    out = aggregate_closed(bars, H1)
    assert len(out) == 1
    b0 = out[0]
    assert b0["ts"] == T0
    assert b0["o"] == 100 and b0["h"] == 105 and b0["l"] == 97 and b0["c"] == 100
    assert b0["v"] == pytest.approx(6.5)


def test_aggregate_multiple_full_buckets_ascending():
    # 覆盖两个完整小时 + 一个进行中小时（02:00 只到 02:00 第一根）-> 只出 00:00 与 01:00 两桶
    bars = [fifteen(i, 100 + i, 102 + i, 98 + i, 101 + i) for i in range(4)]   # 00:xx
    bars += [fifteen(i, 110, 112, 108, 111) for i in range(4, 8)]              # 01:xx
    bars += [fifteen(8, 120, 122, 118, 121)]                                   # 02:00 起
    out = aggregate_closed(bars, H1)
    assert [b["ts"] for b in out] == [T0, T0 + H1]
    assert out[0]["o"] == 100 and out[0]["c"] == 104
    assert out[1]["o"] == 110 and out[1]["c"] == 111


def test_aggregate_not_aligned_to_epoch_still_utc_aligned():
    # 桶边界始终按 ts % bucket_ms == 0，与源数据第一根是否整点无关
    # 数据到 01:45（01:00 桶的最后一根）：02:00 bar 未到 -> 01:00 桶仍视为未收满
    bars = [fifteen(3 + i, 100 + i, 102 + i, 98 + i, 101 + i) for i in range(5)]  # 00:45..01:45
    out = aggregate_closed(bars, H1)
    assert [b["ts"] for b in out] == [T0]
    # 02:00 bar 一到 -> 01:00 桶收满
    bars += [fifteen(8, 110, 112, 108, 111)]
    out = aggregate_closed(bars, H1)
    assert [b["ts"] for b in out] == [T0, T0 + H1]


def test_aggregate_empty_and_bad_args():
    assert aggregate_closed([], H1) == []
    assert aggregate_closed([fifteen(0, 1, 2, 1, 2)], 0) == []
