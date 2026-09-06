"""ma_macd 策略单测（项目唯一内置策略）。

覆盖：工厂、预热、二次确认进场(136首层)、直进模式、放量过滤/止盈口径、
摆动击穿失效、离场事件机、保本/追踪止损、背离过滤、日线前高受阻守卫。
"""
import pytest

from app.strategy import MaMacd, create_strategy, d1_closed_tail


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


_M15 = 900_000
_T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC（整点对齐）


def b15(pxs: list[float], vol_at: dict | None = None) -> list[dict]:
    """15m 合成 bars；vol_at={index: vol} 可指定单根放量。"""
    out = []
    for k, p in enumerate(pxs):
        out.append({"ts": _T0 + k * _M15, "o": p, "h": p * 1.001, "l": p * 0.999,
                    "c": p, "v": float(vol_at.get(k, 10.0)) if vol_at else 10.0})
    return out


def drive(s, pxs, vol_at=None):
    """逐根喂 bar（flat 状态），返回 (事件列表[(idx, action, frac, reason)], 策略)。"""
    ev = []
    for i in range(len(pxs)):
        hist = b15(pxs[:i + 1], vol_at)
        sig = s.on_bar(hist[-1], _Ctx(hist, _Pos()))
        if sig.action != "none":
            ev.append((i, sig.action, sig.frac, sig.reason))
            if sig.action in ("open_long", "open_short"):
                break
    return ev, s


def test_factory_always_mamacd():
    """项目只保留一个内置策略：任何名字都解析到 ma_macd。"""
    assert isinstance(create_strategy("ma_macd", {}), MaMacd)
    assert create_strategy("不存在的策略", {}).name == "ma_macd"


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
    idx, action, frac, _reason = ev[0]
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
    seq = ([1000.0] * 700 + [1000.4] + [1000.2] * 3 + [998.5]   # 深破 999 水印
           + [999.5] * 12 + [1000.6])
    ev, _ = drive(create_strategy("ma_macd", {}), seq)
    assert not any(e[1] == "open_long" for e in ev)
    # 深破那根（idx=704）处理后武装应为空
    s2 = create_strategy("ma_macd", {})
    for i in range(705):
        s2.on_bar(b15(seq[:i + 1])[-1], _Ctx(b15(seq[:i + 1]), _Pos()))
    assert s2._armed is None


def test_evaluate_exits_sl_precedes_partial_and_consumes_hit():
    """离场事件机：止损全平优先；部分止盈命中即消费（一次性）。"""
    s = create_strategy("ma_macd", {})
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
    assert all(stops[k] >= stops[k - 1] for k in range(1, len(stops)))  # 只上移
    assert s.sl_px > entry                  # 追踪已把止损抬过开仓价
    # 崩塌根低点 < 追踪止损 -> evaluate_exits 全平
    crash = {"ts": b15(after)[-1]["ts"], "o": 910, "h": 912, "l": 880, "c": 885, "v": 10.0}
    hit = s.evaluate_exits(crash, "long")
    assert hit is not None and hit[1] == "止损" and hit[2] == 1.0


def test_mamacd_vol_exit_opposite_default_on_opt_out():
    """持仓中放量反向大 bar：默认全平（回测证据优于关闭，见策略文档串）；
    vol_exit_opposite=False 可关闭以贴近作者"日内放量是洗盘"的表述。"""
    base = [1000.0] * 700 + [1000.4] + [1000.2] * 12 + [1000.5]
    for params, expect_close in (({}, True), ({"vol_exit_opposite": False}, False)):
        ev, s = drive(create_strategy("ma_macd", params), base)
        assert ev and ev[0][1] == "open_long"
        entry = base[713]

        class _Long:
            is_open = True
            side = "long"
        pos = _Long()
        pos.entry = entry
        pos.size_eth = 0.05
        # 放量下影大 bar：不破止损(996.9)、不破生命线，仅"放量+反向形态"
        seq = base[:714] + [1000.2]
        hist = b15(seq, vol_at={714: 200.0})
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
    s = create_strategy("ma_macd", {"d1_enable": True, "d1_swing_days": 10,
                                    "d1_tests": 2, "d1_tol_pct": 0.002})
    # 11 天爬升（给前高窗口）-> 试探日1（摸旧高回落）
    pxs, cur = [], 1000.0
    for k in range(11):
        tgt = 1000 + k * 10
        pxs += _d1_day(cur, tgt + 3, tgt)
        cur = tgt
    pxs += _d1_day(cur, cur + 3, cur - 8)
    for i in range(300, len(pxs)):
        s.on_bar(b15(pxs[:i + 1])[-1], _Ctx(b15(pxs[:i + 1]), _Pos()))
    assert s._d1_guard is None  # 试探 1 次不足
    # 试探日2 + 次日 4 根封口（试探日收满后才能计入前高窗口）
    X = max(b["h"] for b in d1_closed_tail(b15(pxs), 12)[-11:-1])
    pxs += _d1_day(pxs[-1], X / 1.001 + 0.2, pxs[-1] - 8)
    pxs += [pxs[-1]] * 4
    hist = b15(pxs)
    s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert s._d1_guard is not None and s._long_blocked()
    guard_x = s._d1_guard["x"]
    # 突破日：收盘越过前高 -> 次日封口确认后解除
    pxs += _d1_day(pxs[-1], guard_x / 1.001 + 20, guard_x + 15)
    pxs += [pxs[-1]] * 4
    hist = b15(pxs)
    s.on_bar(hist[-1], _Ctx(hist, _Pos()))
    assert s._d1_guard is None


def _day_const(close, days):
    """days 天价格恒定的 15m 序列（日线收盘=close）。"""
    out = []
    for d in range(days):
        out += [close] * 96
    return out


def test_mamacd_d1_bias_up_down_classification():
    """日线趋势偏置：收盘 vs D1 MA20；多头日禁空/空头日禁多的门控。"""
    s = create_strategy("ma_macd", {"d1_bias_ma": 20})
    # 前 5 天 1000 + 后 18 天 1010 -> 最近收盘 1010 >= MA20(≈1009) -> 多头日
    hist = b15(_day_const(1000.0, 5) + _day_const(1010.0, 18))
    s._update_d1_bias(hist)
    assert s._d1_bias_up is True
    assert s._bias_ok("long") and not s._bias_ok("short")
    # 后 3 天跌回 1000 -> 最近收盘 1000 < MA20(≈1008.5) -> 空头日
    hist2 = b15(_day_const(1000.0, 5) + _day_const(1010.0, 18) + _day_const(1000.0, 3))
    s._update_d1_bias(hist2)
    assert s._d1_bias_up is False
    assert s._bias_ok("short") and not s._bias_ok("long")


def test_mamacd_d1_guard_closes_open_long():
    """守卫激活时持仓中的多单 -> '日线前高受阻'离场信号。"""
    s = create_strategy("ma_macd", {"d1_enable": True, "d1_swing_days": 10,
                                    "d1_tests": 2, "d1_tol_pct": 0.002})
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
