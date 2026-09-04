"""策略单测：海龟式通道突破（入场突破/移动通道止损/对称做空）。"""
import pytest

from app.strategy import DonchianBreakout, calc_adx, create_strategy


class _Pos:
    def __init__(self, side=""):
        self.side = side

    @property
    def is_open(self):
        return self.side != ""


class _Ctx:
    def __init__(self, history, position):
        self.history = history
        self.position = position
        self.price = history[-1]["c"] if history else 0.0


def bars(closes: list[float], start_ts: int = 1_700_000_000_000) -> list[dict]:
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        out.append({"ts": start_ts + i * 60_000, "o": o, "h": max(o, c) * 1.001,
                    "l": min(o, c) * 0.999, "c": c, "v": 1.0})
    return out


def test_factory_resolves_strategies():
    assert isinstance(create_strategy("donchian", {}), DonchianBreakout)
    assert create_strategy("unknown_name", {}).name == "trend_ema"  # 未知回退趋势策略


def test_entry_long_on_channel_breakout():
    s = DonchianBreakout({"entry_len": 10, "exit_len": 5})
    hist = bars([3000.0] * 30 + [3005.0])  # 收盘突破前高（≈3003）
    sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert sig.action == "open_long"
    assert s.sl_px is not None  # 开仓即带通道止损
    assert s.tp_px is None      # 无固定止盈（让利润奔跑）


def test_no_entry_inside_channel():
    s = DonchianBreakout({"entry_len": 10, "exit_len": 5})
    hist = bars([3000.0] * 30 + [3002.0])  # 未突破前高 3003
    sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert sig.action == "none"


def test_entry_short_on_channel_breakdown():
    s = DonchianBreakout({"entry_len": 10, "exit_len": 5})
    hist = bars([3000.0] * 30 + [2995.0])  # 跌破前低 3000×0.999≈2997
    sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert sig.action == "open_short"
    assert s.sl_px is not None


def test_channel_stop_ratchets_up_for_long():
    """持仓中逐 bar 移动止损只朝有利方向（多仓上移、不低于前值）。"""
    s = DonchianBreakout({"entry_len": 10, "exit_len": 5})
    hist = bars([3000.0] * 30 + [3005.0])
    s.on_bar(hist[-1], _Ctx(hist, _Pos()))  # 开多信号，sl 已设
    sl0 = s.sl_px
    # 价格上台阶后，窗口最低价抬高 -> sl 只上移
    hist2 = bars([3000.0] * 30 + [3005.0] + [3020.0] * 6)
    pos = _Pos("long")
    s.sl_px = sl0
    for i in range(len(hist) - 1, len(hist2)):
        s.on_bar(hist2[i], _Ctx(hist2[: i + 1], pos))
    assert s.sl_px >= sl0
    assert s.sl_px > 3000.0  # 已随新低点上移（旧低点滑出窗口）


def test_check_tp_sl_hits_trailing_stop():
    """引擎按 bar 低点触发通道止损（长仓跌穿移动止损价）。"""
    s = DonchianBreakout({"entry_len": 10, "exit_len": 5})
    hist = bars([3000.0] * 30 + [3005.0])
    sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert sig.action == "open_long"
    stop = s.sl_px
    # 长仓 bar 低点跌破止损 -> check_tp_sl 返回止损价
    hit_bar = {"ts": 0, "o": stop + 5, "h": stop + 8, "l": stop - 1, "c": stop - 0.5, "v": 1}
    hit = s.check_tp_sl(hit_bar, "long")
    assert hit is not None
    assert hit[0] == pytest.approx(stop)


def test_adx_low_on_flat_low_on_spike_high_on_trend():
    flat = bars([3000.0] * 40)
    assert calc_adx(flat, 14) is not None
    assert calc_adx(flat, 14) < 15  # 无趋势 ADX 低
    trend = bars([3000.0 + i * 2 for i in range(60)])  # 强单边
    assert calc_adx(trend, 14) > 40  # 强趋势 ADX 高


def test_adx_gate_blocks_chop_breakout():
    s = DonchianBreakout({"entry_len": 10, "exit_len": 5, "adx_min": 25})
    hist = bars([3000.0] * 30 + [3005.0])  # 平盘后单根突破，ADX 仍低
    sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert sig.action == "none"  # 被 ADX 过滤拦截


def test_adx_gate_allows_trend_breakout():
    s = DonchianBreakout({"entry_len": 10, "exit_len": 5, "adx_min": 25})
    hist = bars([3000.0] * 20 + [3000.0 + i * 5 for i in range(1, 30)])  # 持续上升趋势
    sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert sig.action == "open_long"
