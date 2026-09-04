"""策略框架：on_bar 决策 + 内置策略（trend_ema / donchian）。

同一套策略代码跑 回测 / 纸盘 / 实盘。
开仓即声明止盈价（tp）与止损价（sl），由引擎按 K 线高低点精确撮合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Signal:
    action: str   # open_long / open_short / close / none
    reason: str = ""


def create_strategy(name: str, params: dict) -> "Strategy":
    """策略工厂：引擎/回测只通过名字取策略，新增策略只需加类并在此注册。"""
    registry = {
        TrendEma.name: TrendEma,
        DonchianBreakout.name: DonchianBreakout,
        RsiRevert.name: RsiRevert,
        BollRevert.name: BollRevert,
        EmaCrossTrail.name: EmaCrossTrail,
        TsMom.name: TsMom,
    }
    cls = registry.get(name or TrendEma.name, TrendEma)
    return cls(params or {})


class Strategy:
    name = "base"

    def __init__(self, params: dict):
        self.params = params
        self.sl_px: Optional[float] = None
        self.tp_px: Optional[float] = None

    def on_bar(self, bar: dict, ctx) -> Signal:
        raise NotImplementedError

    def on_open(self) -> None:
        """开仓后清空目标价（由子类在开仓信号时设置 sl/tp）。"""
        self.sl_px = None
        self.tp_px = None

    def check_tp_sl(self, bar: dict, side: str) -> Optional[tuple[float, str]]:
        """按本根 K 线高低检查止盈止损，返回 (成交价, 原因)；先检查止损。"""
        if self.sl_px is None and self.tp_px is None:
            return None
        if side == "long":
            if self.sl_px is not None and bar["l"] <= self.sl_px:
                return self.sl_px, "止损"
            if self.tp_px is not None and bar["h"] >= self.tp_px:
                return self.tp_px, "止盈"
        else:
            if self.sl_px is not None and bar["h"] >= self.sl_px:
                return self.sl_px, "止损"
            if self.tp_px is not None and bar["l"] <= self.tp_px:
                return self.tp_px, "止盈"
        return None

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        """进程重启恢复持仓后重挂保护止损（子类按各自规则实现；基类默认不动作）。"""
        pass

    def describe(self, history: list[dict], position) -> dict:
        """看板用：策略当前状态（子类覆盖）。position 为 broker.Position。"""
        return {"pos": position.side if getattr(position, "is_open", False) else "flat"}


def ema(values: list[float], period: int) -> list[float]:
    """EMA 序列（长度与输入一致，预热期为 None 由调用方保证足够长度）。"""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def calc_atr(bars: list[dict], period: int) -> Optional[float]:
    """简单平均 ATR（基于最近 period 根 K 线）。"""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def calc_adx(bars: list[dict], period: int = 14) -> Optional[float]:
    """ADX（趋势强度 0~100）：>20 一般视为有趋势。返回最近 period 的平均 DX。"""
    n = len(bars)
    if n < period * 2 + 1:
        return None
    trs, pdm, ndm = [], [], []
    for i in range(1, n):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - bars[i - 1]["h"], bars[i - 1]["l"] - l
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)

    def wilder(vals: list[float]) -> list[float]:
        out = [sum(vals[:period])]
        for v in vals[period:]:
            out.append(out[-1] - out[-1] / period + v)
        return out

    trs_w, pdm_w, ndm_w = wilder(trs), wilder(pdm), wilder(ndm)
    pdi = [100.0 * a / b if b > 0 else 0.0 for a, b in zip(pdm_w, trs_w)]
    ndi = [100.0 * a / b if b > 0 else 0.0 for a, b in zip(ndm_w, trs_w)]
    dx = [100.0 * abs(a - b) / (a + b) if a + b > 0 else 0.0 for a, b in zip(pdi, ndi)]
    return sum(dx[-period:]) / period


class TrendEma(Strategy):
    """EMA 快慢交叉顺势 + ATR 动态止损 + 固定盈亏比止盈。"""

    name = "trend_ema"
    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        """重启恢复持仓：按 ATR 重挂硬止损（无止盈目标，等信号/移动逻辑接管）。"""
        atr = calc_atr(history, self.atr_period)
        if atr:
            self.sl_px = entry - atr * self.atr_sl_mult if side == "long" \
                else entry + atr * self.atr_sl_mult
            self.tp_px = None
    def __init__(self, params: dict):
        super().__init__(params)
        self.ema_fast = int(params.get("ema_fast", 5))
        self.ema_slow = int(params.get("ema_slow", 20))
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_sl_mult = float(params.get("atr_sl_mult", 2.0))
        self.tp_ratio = float(params.get("tp_ratio", 2.0))

    def _warm(self, closes: list[float]) -> bool:
        return len(closes) >= self.ema_slow + 1

    def on_bar(self, bar: dict, ctx) -> Signal:
        closes = [b["c"] for b in ctx.history]
        if not self._warm(closes):
            return Signal("none", "预热中")
        fast = ema(closes, self.ema_fast)
        slow = ema(closes, self.ema_slow)
        prev_fast, prev_slow = fast[-2], slow[-2]
        cross_up = fast[-1] > slow[-1] and prev_fast <= prev_slow
        cross_down = fast[-1] < slow[-1] and prev_fast >= prev_slow

        pos = ctx.position
        if pos.is_open:
            if pos.side == "long" and cross_down:
                return Signal("close", "死叉平多")
            if pos.side == "short" and cross_up:
                return Signal("close", "金叉平空")
            return Signal("none")

        if cross_up:
            return self._open_signal("open_long", "金叉开多", bar, ctx)
        if cross_down:
            return Signal("open_short", "死叉开空")
        return Signal("none")

    def _open_signal(self, action: str, reason: str, bar: dict, ctx) -> Signal:
        # 设置止损止盈：基于当前价与 ATR
        atr = calc_atr(ctx.history, self.atr_period) or (bar["h"] - bar["l"])
        entry = bar["c"]
        if action == "open_long":
            self.sl_px = entry - atr * self.atr_sl_mult
            self.tp_px = entry + atr * self.atr_sl_mult * self.tp_ratio
        else:
            self.sl_px = entry + atr * self.atr_sl_mult
            self.tp_px = entry - atr * self.atr_sl_mult * self.tp_ratio
        return Signal(action, reason)


class DonchianBreakout(Strategy):
    """海龟式通道突破（吃单边趋势，入场快、规则机械、只用已收盘 K 线）。

    - 入场：收盘价突破前 entry_len 根的最高价开多 / 最低价开空
    - 离场：移动通道止损 = 前 exit_len 根的最低价（多）/最高价（空），逐 bar 上移/下移，
      由引擎按 bar 低/高精确触发；无止盈目标（让利润奔跑，出场看结构）
    - 可选 atr_stop_mult>0 时附加 ATR 硬止损兜底（默认 0 仅通道止损）
    """

    name = "donchian"

    def __init__(self, params: dict):
        super().__init__(params)
        self.entry_len = int(params.get("entry_len", 30))
        self.exit_len = int(params.get("exit_len", 15))
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_stop_mult = float(params.get("atr_stop_mult", 0.0))
        # 趋势强度过滤：adx_min>0 时只在 ADX>=adx_min 允许开仓（砍震荡期假突破）
        self.adx_min = float(params.get("adx_min", 0.0))
        self.adx_period = int(params.get("adx_period", 14))

    def on_bar(self, bar: dict, ctx) -> Signal:
        hist = ctx.history
        if len(hist) < self.entry_len + 1:
            return Signal("none", "预热中")
        pos = ctx.position
        if pos.is_open:
            self._update_channel_stop(hist, pos.side)
            return Signal("none")
        prev_highs = [b["h"] for b in hist[-(self.entry_len + 1):-1]]
        prev_lows = [b["l"] for b in hist[-(self.entry_len + 1):-1]]
        channel_high = max(prev_highs)
        channel_low = min(prev_lows)
        if self.adx_min > 0:
            # ADX 只取最近一小段历史计算（避免对全量历史 O(n²)）
            tail = hist[-(self.adx_period * 8):]
            adx = calc_adx(tail, self.adx_period)
            if adx is None or adx < self.adx_min:
                return Signal("none", f"ADX 过滤 {adx if adx is not None else '-'}")
        if bar["c"] > channel_high:
            self.sl_px = self._channel_stop(hist, "long")
            self.tp_px = None
            return Signal("open_long", f"突破前{self.entry_len}根高点 {channel_high:.2f}")
        if bar["c"] < channel_low:
            self.sl_px = self._channel_stop(hist, "short")
            self.tp_px = None
            return Signal("open_short", f"跌破前{self.entry_len}根低点 {channel_low:.2f}")
        return Signal("none")

    def _channel_stop(self, hist: list[dict], side: str) -> float:
        """离场止损：多仓 = 前 exit_len 根最低价；空仓 = 前 exit_len 根最高价。"""
        bars = hist[-(self.exit_len + 1):-1]
        if side == "long":
            return min(b["l"] for b in bars)
        return max(b["h"] for b in bars)

    def _update_channel_stop(self, hist: list[dict], side: str) -> None:
        """逐 bar 更新移动止损：只朝有利方向移动（多仓只上移，空仓只下移）。"""
        stop = self._channel_stop(hist, side)
        if self.sl_px is None:
            self.sl_px = stop
        elif side == "long":
            self.sl_px = max(self.sl_px, stop)
        else:
            self.sl_px = min(self.sl_px, stop)

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        """重启恢复持仓：用历史 K 线立即重挂移动通道止损。"""
        self.sl_px = self._channel_stop(history, side)
        self.tp_px = None

    def describe(self, history: list[dict], position) -> dict:
        """看板：空仓时给出当前等待突破的触发位（多/空），有仓则只报方向。"""
        if getattr(position, "is_open", False):
            return {"pos": position.side}
        if len(history) < self.entry_len + 1:
            return {"pos": "flat", "note": "预热中…"}
        prev_highs = [b["h"] for b in history[-(self.entry_len + 1):-1]]
        prev_lows = [b["l"] for b in history[-(self.entry_len + 1):-1]]
        return {"pos": "flat", "hi": max(prev_highs), "lo": min(prev_lows),
                "note": f"突破前{self.entry_len}根通道"}


def calc_rsi(closes: list[float], period: int = 2) -> Optional[float]:
    """RSI（Wilder 平滑），取序列末端值。"""
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))

    def wilder(vals):
        out = [sum(vals[:period]) / period]
        for v in vals[period:]:
            out.append((out[-1] * (period - 1) + v) / period)
        return out

    ag, al = wilder(gains), wilder(losses)
    avg_gain, avg_loss = ag[-1], al[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


class RsiRevert(Strategy):
    """均值回归：RSI(2) 超卖/超买 + 长周期均线趋势过滤（Connors 风格）。

    - 做多：RSI(2)<lo 且 收盘 > 长均线(趋势向上不逆势) → 反弹到短均线止盈
    - 做空：RSI(2)>hi 且 收盘 < 长均线 → 回落到短均线止盈
    - 保护：ATR 硬止损（防极端行情），无固定止盈（均值回归到短均线自然离场）
    """

    name = "rsi_revert"

    def __init__(self, params: dict):
        super().__init__(params)
        self.rsi_len = int(params.get("rsi_len", 2))
        self.lo = float(params.get("lo", 10.0))
        self.hi = float(params.get("hi", 90.0))
        self.sma_len = int(params.get("sma_len", 200))
        self.exit_ma = int(params.get("exit_ma", 5))
        self.exit_rsi = bool(params.get("exit_rsi", True))  # False=只按均线离场，让反弹跑到位
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_sl_mult = float(params.get("atr_sl_mult", 3.0))

    def _sma(self, closes: list[float], n: int) -> Optional[float]:
        if len(closes) < n:
            return None
        return sum(closes[-n:]) / n

    def on_bar(self, bar: dict, ctx) -> Signal:
        # 限窗：只取最近一段历史，避免 O(n²)
        tail = ctx.history[-(self.sma_len * 2 + 60):]
        closes = [b["c"] for b in tail]
        if len(closes) < self.sma_len + 5:
            return Signal("none", "预热中")
        sma = self._sma(closes, self.sma_len)
        ema5 = self._sma(closes, self.exit_ma)
        rsi = calc_rsi(closes, self.rsi_len)
        if None in (sma, ema5, rsi):
            return Signal("none", "预热中")
        pos = ctx.position
        c = bar["c"]
        if pos.is_open:
            # 反弹/回落到短均线即离场
            if pos.side == "long" and (c > ema5 or (self.exit_rsi and rsi > 50)):
                return Signal("close", "反弹到位")
            if pos.side == "short" and (c < ema5 or (self.exit_rsi and rsi < 50)):
                return Signal("close", "回落到位")
            return Signal("none")
        # 开仓：只在趋势方向顺势做均值回归
        if c > sma and rsi < self.lo:
            self.sl_px = c - (calc_atr(ctx.history, self.atr_period) or c * 0.005) * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_long", f"超卖反弹 RSI={rsi:.0f}")
        if c < sma and rsi > self.hi:
            self.sl_px = c + (calc_atr(ctx.history, self.atr_period) or c * 0.005) * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_short", f"超买回落 RSI={rsi:.0f}")
        return Signal("none")

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        atr = calc_atr(history, self.atr_period) or entry * 0.005
        self.sl_px = entry - atr * self.atr_sl_mult if side == "long" \
            else entry + atr * self.atr_sl_mult
        self.tp_px = None


def calc_sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


class BollRevert(Strategy):
    """布林带均值回归：触下轨且价格在长均线上方做多，回中轨离场（做空镜像）。"""

    name = "boll_revert"

    def __init__(self, params: dict):
        super().__init__(params)
        self.bb_len = int(params.get("bb_len", 20))
        self.bb_mult = float(params.get("bb_mult", 2.0))
        self.sma_len = int(params.get("sma_len", 200))
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_sl_mult = float(params.get("atr_sl_mult", 2.0))

    def _bands(self, closes):
        if len(closes) < self.bb_len:
            return None, None, None
        w = closes[-self.bb_len:]
        mid = sum(w) / self.bb_len
        sd = (sum((x - mid) ** 2 for x in w) / self.bb_len) ** 0.5
        return mid, mid - self.bb_mult * sd, mid + self.bb_mult * sd

    def on_bar(self, bar: dict, ctx) -> Signal:
        tail = ctx.history[-(self.sma_len + self.bb_len + 60):]
        closes = [b["c"] for b in tail]
        if len(closes) < self.sma_len + 5:
            return Signal("none", "预热中")
        mid, lo, hi = self._bands(closes)
        sma = calc_sma(closes, self.sma_len)
        if None in (mid, lo, hi, sma):
            return Signal("none")
        c = bar["c"]
        pos = ctx.position
        if pos.is_open:
            if pos.side == "long" and c >= mid:
                return Signal("close", "回到中轨")
            if pos.side == "short" and c <= mid:
                return Signal("close", "回到中轨")
            return Signal("none")
        if c < lo and c > sma:
            atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
            self.sl_px = c - atr * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_long", "触下轨")
        if c > hi and c < sma:
            atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
            self.sl_px = c + atr * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_short", "触上轨")
        return Signal("none")

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        atr = calc_atr(history, self.atr_period) or entry * 0.005
        self.sl_px = entry - atr * self.atr_sl_mult if side == "long" \
            else entry + atr * self.atr_sl_mult
        self.tp_px = None


class EmaCrossTrail(Strategy):
    """均线交叉入场 + 移动通道/ATR 止损离场（趋势跟随，让利润奔跑）。"""

    name = "ema_cross_trail"

    def __init__(self, params: dict):
        super().__init__(params)
        self.fast = int(params.get("fast", 12))
        self.slow = int(params.get("slow", 26))
        self.atr_period = int(params.get("atr_period", 14))
        self.trail_mult = float(params.get("trail_mult", 3.0))

    def on_bar(self, bar: dict, ctx) -> Signal:
        tail = ctx.history[-(self.slow * 3 + 60):]
        closes = [b["c"] for b in tail]
        if len(closes) < self.slow + 3:
            return Signal("none", "预热中")
        ef, es = ema(closes, self.fast), ema(closes, self.slow)
        pos = ctx.position
        c = bar["c"]
        if pos.is_open:
            # 移动止损只朝有利方向
            atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
            if pos.side == "long":
                self.sl_px = max(self.sl_px or 0, c - atr * self.trail_mult)
            else:
                self.sl_px = min(self.sl_px or 1e18, c + atr * self.trail_mult)
            if pos.side == "long" and ef[-1] < es[-1]:
                return Signal("close", "死叉")
            if pos.side == "short" and ef[-1] > es[-1]:
                return Signal("close", "金叉")
            return Signal("none")
        if ef[-1] > es[-1] and ef[-2] <= es[-2]:
            atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
            self.sl_px = c - atr * self.trail_mult
            self.tp_px = None
            return Signal("open_long", "金叉")
        if ef[-1] < es[-1] and ef[-2] >= es[-2]:
            atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
            self.sl_px = c + atr * self.trail_mult
            self.tp_px = None
            return Signal("open_short", "死叉")
        return Signal("none")

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        atr = calc_atr(history, self.atr_period) or entry * 0.005
        self.sl_px = entry - atr * self.trail_mult if side == "long" \
            else entry + atr * self.trail_mult
        self.tp_px = None


class SuperTrend(Strategy):
    """SuperTrend：ATR 上下轨跟随，方向翻转即换仓（经典趋势/跟踪止损）。"""

    name = "supertrend"

    def __init__(self, params: dict):
        super().__init__(params)
        self.period = int(params.get("period", 10))
        self.mult = float(params.get("mult", 3.0))
        self.atr_period = int(params.get("atr_period", 10))
        self._dir = 1

    def on_bar(self, bar: dict, ctx) -> Signal:
        tail = ctx.history[-(self.period * 4 + 40):]
        closes = [b["c"] for b in tail]
        if len(closes) < self.period * 3:
            return Signal("none", "预热中")
        c = bar["c"]
        atr = calc_atr(ctx.history, self.atr_period)
        if not atr:
            return Signal("none", "预热中")
        hl2 = (bar["h"] + bar["l"]) / 2.0
        ub, lb = hl2 + self.mult * atr, hl2 - self.mult * atr
        # 简化方向判定：价格相对轨的延续（非严格递归，够筛选用）
        prev_c = closes[-2]
        if c > prev_c:
            d = 1 if prev_c >= self._dir * 0 else 1
        pos = ctx.position
        if pos.is_open:
            stop = c - self.mult * atr if pos.side == "long" else c + self.mult * atr
            self.sl_px = stop
            self._dir = 1 if pos.side == "long" else -1
            return Signal("none")
        # 空仓：跟踪最近 N 根的上/下轨方向
        up_ok = c > lb
        dn_ok = c < ub
        if up_ok and not dn_ok and closes[-1] > closes[-min(len(closes), 5)]:
            self.sl_px = c - self.mult * atr
            self.tp_px = None
            self._dir = 1
            return Signal("open_long", "super上行")
        if dn_ok and not up_ok:
            self.sl_px = c + self.mult * atr
            self.tp_px = None
            self._dir = -1
            return Signal("open_short", "super下行")
        return Signal("none")


class TsMom(Strategy):
    """时间序列动量：N 期收益>0 且趋势过滤(价>EMA200)做多，收益转负离场（做空镜像）。"""

    name = "ts_momentum"

    def __init__(self, params: dict):
        super().__init__(params)
        self.lookback = int(params.get("lookback", 96))   # 96 根 15m = 24h
        self.ema_len = int(params.get("ema_len", 200))
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_sl_mult = float(params.get("atr_sl_mult", 3.0))

    def on_bar(self, bar: dict, ctx) -> Signal:
        tail = ctx.history[-(self.lookback + self.ema_len + 30):]
        closes = [b["c"] for b in tail]
        if len(closes) < self.lookback + 5:
            return Signal("none", "预热中")
        c = bar["c"]
        ret = c / closes[-self.lookback - 1] - 1.0
        ema_l = calc_sma(closes, self.ema_len)
        if ema_l is None:
            return Signal("none")
        pos = ctx.position
        if pos.is_open:
            if pos.side == "long" and ret <= 0:
                return Signal("close", "动量转负")
            if pos.side == "short" and ret >= 0:
                return Signal("close", "动量转正")
            return Signal("none")
        atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
        if ret > 0.002 and c > ema_l:
            self.sl_px = c - atr * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_long", f"动量 {ret*100:.1f}%")
        if ret < -0.002 and c < ema_l:
            self.sl_px = c + atr * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_short", f"动量 {ret*100:.1f}%")
        return Signal("none")


class StochRsiRevert(Strategy):
    """随机 RSI 均值回归：K<lo 且价>长均线做多，K>exit 或反弹到短均线离场。"""

    name = "stoch_rsi"

    def __init__(self, params: dict):
        super().__init__(params)
        self.rsi_len = int(params.get("rsi_len", 14))
        self.stoch_len = int(params.get("stoch_len", 14))
        self.lo = float(params.get("lo", 10.0))
        self.exit_k = float(params.get("exit_k", 70.0))
        self.sma_len = int(params.get("sma_len", 200))
        self.exit_ma = int(params.get("exit_ma", 5))
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_sl_mult = float(params.get("atr_sl_mult", 2.0))

    def _stoch(self, closes):
        rs = []
        for i in range(self.stoch_len + 1, len(closes) + 1):
            w = closes[i - self.stoch_len - 1:i]
            lo, hi = min(w), max(w)
            last = calc_rsi(w, self.rsi_len)
            if last is None:
                return None
            rs.append(100.0 * (last - lo) / (hi - lo) if hi > lo else 50.0)
        return rs[-1] if rs else None

    def on_bar(self, bar: dict, ctx) -> Signal:
        tail = ctx.history[-(self.sma_len + self.stoch_len * 3 + 60):]
        closes = [b["c"] for b in tail]
        if len(closes) < self.sma_len + self.stoch_len * 2:
            return Signal("none", "预热中")
        sma = calc_sma(closes, self.sma_len)
        ema5 = calc_sma(closes, self.exit_ma)
        k = self._stoch(closes)
        if None in (sma, ema5, k):
            return Signal("none")
        c = bar["c"]
        pos = ctx.position
        if pos.is_open:
            if pos.side == "long" and (k > self.exit_k or c > ema5):
                return Signal("close", "回抽到位")
            if pos.side == "short" and (k < 100 - self.exit_k or c < ema5):
                return Signal("close", "回抽到位")
            return Signal("none")
        atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
        if k < self.lo and c > sma:
            self.sl_px = c - atr * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_long", f"StochK={k:.0f}")
        if k > 100 - self.lo and c < sma:
            self.sl_px = c + atr * self.atr_sl_mult
            self.tp_px = None
            return Signal("open_short", f"StochK={k:.0f}")
        return Signal("none")
