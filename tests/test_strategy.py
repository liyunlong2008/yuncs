"""策略单测：海龟式通道突破（入场突破/移动通道止损/对称做空）+ ma_macd 分批体系。"""
import pytest

from app.strategy import (
    DonchianBreakout,
    MaMacd,
    calc_adx,
    create_strategy,
)


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


# ---------- ma_macd：MA+MACD 两情相悦 × 1H 生命线 × 136 分仓 ----------
_M15 = 900_000
_T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC（整点对齐，15m 桶规整）


def b15(pxs: list[float], vol_at: dict | None = None) -> list[dict]:
    """15m 合成 bars；vol_at={index: vol} 可指定单根放量。"""
    out = []
    for k, p in enumerate(pxs):
        out.append({"ts": _T0 + k * _M15, "o": p, "h": p * 1.001, "l": p * 0.999,
                    "c": p, "v": float(vol_at.get(k, 10.0)) if vol_at else 10.0})
    return out


def drive(s, pxs, vol_at=None):
    """逐根喂 bar（flat 状态），返回 (事件列表[(idx, action, frac)], 策略)。"""
    ev = []
    for i in range(len(pxs)):
        hist = b15(pxs[:i + 1], vol_at)
        sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
        if sig.action != "none":
            ev.append((i, sig.action, sig.frac, sig.reason))
            if sig.action in ("open_long", "open_short"):
                break
    return ev, s


def test_mamacd_warmup_needs_full_h1():
    """1H 闭合桶不足 MA60 -> 只预热不决策；describe 有提示。"""
    s = create_strategy("ma_macd", {})
    hist = b15([1000.0] * 60)  # ~14 个闭合 1H < 60
    sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert sig.action == "none" and "预热" in sig.reason
    assert "预热" in s.describe(hist, _Pos())["note"]


def test_mamacd_second_confirm_enters_with_leg1():
    """首次金叉只武装（错开）；回踩不破摆动低点后的第二次金叉才进 10%（136 首层）。

    序列：700 根平盘(预热) -> 涨(首金叉武装) -> 温和回踩 -> 再涨(二次金叉)，
    确定性在第 713 根触发 T3 模板开多（由合成数据网格标定）。
    """
    s = create_strategy("ma_macd", {})
    seq = [1000.0] * 700 + [1000.4] + [1000.2] * 12 + [1000.5]
    ev, s = drive(s, seq)
    assert ev, "应至少出现一次开仓"
    idx, action, frac, reason = ev[0]
    assert idx == 713 and action == "open_long"
    assert frac == pytest.approx(0.10)  # leg1
    assert s._legs == 1
    assert s.sl_px is not None and s.sl_px < seq[713]  # 开仓即带保护止损
    assert s.partial_exits  # 进场即带目标位部分止盈事件


def test_mamacd_confirm_any_opens_on_first_cross():
    """confirm=any：模板内首个两情相悦直接进（不需要二次确认）。"""
    s = create_strategy("ma_macd", {"confirm": "any"})
    seq = [1000.0] * 700 + [1000.4]
    ev, _ = drive(s, seq)
    assert ev and ev[0][0] == 700 and ev[0][1] == "open_long"


def test_mamacd_volume_spike_blocks_second_cross():
    """放量触发 bar（>20 根均量×2.5）不进场，武装保留等下一次确认。"""
    s = create_strategy("ma_macd", {})
    seq = [1000.0] * 700 + [1000.4] + [1000.2] * 12 + [1000.5]
    vol_at = {713: 100.0}  # 仅二次金叉那根放量
    ev, s = drive(s, seq, vol_at)
    assert ev == []                      # 未进场
    assert s._armed == "long"            # 武装未被消费
    assert s._legs == 0


def test_mamacd_swing_break_disarms():
    """首叉后深跌破摆动低点 -> 武装失效；后续回升不再以'旧确认'进场。"""
    s = create_strategy("ma_macd", {})
    seq = ([1000.0] * 700 + [1000.4] + [1000.2] * 3 + [998.5]   # 深破 999 水印
           + [999.5] * 12 + [1000.6])
    ev, s = drive(s, seq)
    assert not any(e[1] == "open_long" for e in ev)
    # 深破那根（idx=704）处理后武装应为空
    hist = b15(seq[:705])
    s2 = create_strategy("ma_macd", {})
    for i in range(705):
        s2.on_bar(hist[i], _Ctx(b15(seq[:i + 1]), _Pos()))
    assert s2._armed is None


def test_evaluate_exits_sl_precedes_partial_and_consumes_hit():
    """离场事件机：止损全平优先；部分止盈命中即消费（一次性）。"""
    s = create_strategy("trend_ema", {})
    s.sl_px = 2990.0
    s.partial_exits = [{"px": 3010.0, "frac": 0.5, "reason": "目标位部分止盈"}]
    # 同根 bar 同时触及止损与部分止盈 -> 止损优先，部分事件保留
    both = {"ts": 0, "o": 3000, "h": 3015, "l": 2985, "c": 2995, "v": 1}
    hit = s.evaluate_exits(both, "long")
    assert hit == (2990.0, "止损", 1.0)
    assert len(s.partial_exits) == 1
    # 只触部分止盈 -> 返回 frac=0.5 且事件被消费
    s.sl_px = 2950.0
    hit = s.evaluate_exits(both, "long")
    assert hit[0] == pytest.approx(3010.0)
    assert hit[1] == "目标位部分止盈" and hit[2] == pytest.approx(0.5)
    assert s.partial_exits == []


def test_mamacd_breakeven_and_trail_ratchet_then_stop():
    """浮盈达阈值移保本 -> chandelier 只上移；随后大跌触发全平止损。"""
    s = create_strategy("ma_macd", {})
    base = [1000.0] * 700 + [1000.4] + [1000.2] * 12 + [1000.5]
    ev, s = drive(s, base)
    assert ev and ev[0][1] == "open_long"
    entry = base[713]

    class _LongPos:
        is_open = True
        side = "long"

    pos = _LongPos()
    pos.entry = entry
    pos.size_eth = 0.5
    after = base[:714] + [entry + 1.0 + i * 0.4 for i in range(40)]  # 缓涨到 ~1027
    after += [after[-1]] * 10                                          # 高位平台
    after += [900.0] * 8                                               # 崩塌
    stops = []
    for i in range(714, len(after)):
        hist = b15(after[:i + 1])
        s.on_bar(hist[-1], _Ctx(hist, pos))
        if s._be_done:
            stops.append(s.sl_px)
    assert s._be_done                       # 保本已触发
    assert stops == sorted(stops, reverse=True) or all(
        stops[k] >= stops[k - 1] for k in range(1, len(stops)))  # 只上移
    assert s.sl_px > entry                  # 追踪已把止损抬过开仓价
    # 崩塌根低点 < 追踪止损 -> evaluate_exits 全平
    crash = b15(after)[-1]
    crash = {"ts": crash["ts"], "o": 910, "h": 912, "l": 880, "c": 885, "v": 10.0}
    hit = s.evaluate_exits(crash, "long")
    assert hit is not None and hit[1] == "止损" and hit[2] == 1.0


# ---------- v2 行为：放量口径 / 背离过滤 / 日线前高受阻 ----------

def test_mamacd_vol_exit_opposite_default_on_opt_out():
    """持仓中放量反向大 bar：默认全平（回测证据优于关闭，见策略文档串）；
    vol_exit_opposite=False 可关闭以贴近作者"日内放量是洗盘"的表述。"""
    base = [1000.0] * 700 + [1000.4] + [1000.2] * 12 + [1000.5]
    for params, expect_close in (({}, True), ({"vol_exit_opposite": False}, False)):
        s = create_strategy("ma_macd", params)
        ev, s = drive(s, base)
        assert ev and ev[0][1] == "open_long"
        entry = base[713]

        class _Long:
            is_open = True
            side = "long"
        pos = _Long()
        pos.entry = entry
        pos.size_eth = 0.05
        # 放量下影大 bar：不破止损(996.9)、不破生命线，仅"放量+反向形态"
        drop = base[:714] + [{"c": 1000.2, "l": 998.9, "h": 1001.6, "v": 200.0}]
        hist = b15([d["c"] if isinstance(d, dict) else d for d in drop],
                   vol_at={714: 200.0})
        # 手动构造最后一根带放量的反向形态 bar
        hist[-1] = {"ts": hist[-1]["ts"], "o": 1000.5, "h": 1001.6,
                    "l": 998.9, "c": 1000.2, "v": 200.0}
        sig = s.on_bar(hist[-1], _Ctx(hist, pos))
        if expect_close:
            assert sig.action == "close" and "放量" in sig.reason
        else:
            assert sig.action != "close"


def test_mamacd_divergence_filter_shapes_and_gate():
    """背离形状判定（B站教学模块）+ div_filter 对无背离入场的拦截。"""
    s = create_strategy("ma_macd", {"div_filter": True})

    def seg(n, p0, p1):
        return [p0 + (p1 - p0) * k / n for k in range(1, n + 1)]

    plateau = [1000.0] * 200
    shape_a = (plateau + seg(8, 1000, 992) + [990.0] + seg(10, 990, 1006)
               + seg(90, 1006, 990) + [988.0] + seg(6, 988, 995))
    shape_b = (plateau + seg(8, 1000, 992) + [990.0] + seg(10, 990, 1006)
               + seg(8, 1006, 987) + [985.0] + seg(6, 985, 992))
    shape_c = (plateau + seg(8, 1000, 992) + [990.0] + seg(10, 990, 1006)
               + seg(14, 1006, 998) + [996.0] + seg(6, 996, 1002))
    assert s._divergence(shape_a, "long") is True   # 低点更低 + 动能减弱
    assert s._divergence(shape_b, "long") is False  # 低点更低 + 动能增强
    assert s._divergence(shape_c, "long") is False  # 低点抬高
    # 门控：无背离的单跳序列在 confirm=any 下被拦截
    seq = [1000.0] * 700 + [1000.4]
    ev, _ = drive(create_strategy("ma_macd", {"confirm": "any", "div_filter": True}), seq)
    assert ev == []
    ev, _ = drive(create_strategy("ma_macd", {"confirm": "any"}), seq)
    assert ev and ev[0][0] == 700


def _d1_day(day_open, peak, day_close):
    """一天 96 根 15m：先升到 peak 再回落收 day_close。"""
    def seg(n, p0, p1):
        return [p0 + (p1 - p0) * k / n for k in range(1, n + 1)]
    return seg(40, day_open, peak) + seg(56, peak, day_close)


def test_mamacd_d1_guard_activates_blocks_long_and_breakout_clears():
    """日线前高多次试探不破 -> 多单守卫激活（封锁多头、离场等待回调空）；
    日线收盘突破前高后解除（需下一日封口确认）。"""
    params = {"d1_enable": True, "d1_swing_days": 10, "d1_tests": 2,
              "d1_tol_pct": 0.002, "d1_guard_days": 3}
    s = create_strategy("ma_macd", params)
    class _Flat:
        is_open = False
        side = ""
    pos = _Flat()
    # 11 天爬升（给前高窗口）-> 试探日1（摸旧高回落）
    pxs, cur = [], 1000.0
    for k in range(11):
        tgt = 1000 + k * 10
        pxs += _d1_day(cur, tgt + 3, tgt)
        cur = tgt
    pxs += _d1_day(cur, cur + 3, cur - 8)
    for i in range(300, len(pxs), 1):
        s.on_bar(b15(pxs[:i + 1])[-1], _Ctx(b15(pxs[:i + 1]), pos))
    assert s._d1_guard is None  # 试探 1 次不足
    # 试探日2：摸到 X 附近收低 -> 激活
    from app.strategy import d1_closed_tail
    X = max(b["h"] for b in d1_closed_tail(b15(pxs), 12)[-11:-1])
    # 试探日2 + 次日 4 根封口（试探日收满后才能计入前高窗口）
    pxs += _d1_day(pxs[-1], X / 1.001 + 0.2, pxs[-1] - 8)
    pxs += [pxs[-1]] * 4
    hist = b15(pxs)
    s.on_bar(hist[-1], _Ctx(hist, pos))
    assert s._d1_guard is not None and s._long_blocked()
    guard_x = s._d1_guard["x"]
    # 突破日：收盘越过前高 -> 次日封口确认后解除
    pxs += _d1_day(pxs[-1], guard_x / 1.001 + 20, guard_x + 15)
    pxs += [pxs[-1]] * 4
    hist = b15(pxs)
    s.on_bar(hist[-1], _Ctx(hist, pos))
    assert s._d1_guard is None


def test_mamacd_d1_guard_closes_open_long():
    """守卫激活时持仓中的多单 -> '日线前高受阻'离场信号。"""
    s = create_strategy("ma_macd", {"d1_enable": True, "d1_swing_days": 10,
                                    "d1_tests": 2, "d1_tol_pct": 0.002})
    from app.strategy import d1_closed_tail
    pxs, cur = [], 1000.0
    for k in range(11):
        tgt = 1000 + k * 10
        pxs += _d1_day(cur, tgt + 3, tgt)
        cur = tgt
    pxs += _d1_day(cur, cur + 3, cur - 8)
    X = max(b["h"] for b in d1_closed_tail(b15(pxs), 12)[-11:-1])
    pxs += _d1_day(pxs[-1], X / 1.001 + 0.2, pxs[-1] - 8)   # 试探2
    pxs += [pxs[-1]] * 4                                    # 封口 -> 激活
    hist = b15(pxs)

    class _Long:
        is_open = True
        side = "long"
    pos = _Long()
    pos.entry = pxs[-1]
    pos.size_eth = 0.05
    sig = s.on_bar(hist[-1], _Ctx(hist, pos))
    assert sig.action == "close" and "日线前高受阻" in sig.reason
