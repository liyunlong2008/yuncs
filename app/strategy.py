"""策略框架：on_bar 决策 + 内置策略（trend_ema / donchian）。

同一套策略代码跑 回测 / 纸盘 / 实盘。
开仓即声明止盈价（tp）与止损价（sl），由引擎按 K 线高低点精确撮合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import okx_math


@dataclass
class Signal:
    action: str   # open_long / open_short / add_long / add_short / close / none
    reason: str = ""
    frac: float = 1.0  # 分批比例：open 首笔 / add 补批用（相对计划量）；1.0=整仓口径


def create_strategy(name: str, params: dict) -> "Strategy":
    """策略工厂：引擎/回测只通过名字取策略，新增策略只需加类并在此注册。"""
    registry = {
        TrendEma.name: TrendEma,
        DonchianBreakout.name: DonchianBreakout,
        RsiRevert.name: RsiRevert,
        BollRevert.name: BollRevert,
        EmaCrossTrail.name: EmaCrossTrail,
        TsMom.name: TsMom,
        HybridRangeTrend.name: HybridRangeTrend,
        MaMacd.name: MaMacd,
    }
    cls = registry.get(name or TrendEma.name, TrendEma)
    return cls(params or {})


class Strategy:
    name = "base"

    def __init__(self, params: dict):
        self.params = params
        self.sl_px: Optional[float] = None
        self.tp_px: Optional[float] = None
        # 部分止盈事件表：[{"px": float, "frac": float, "reason": str}]，命中即消费
        self.partial_exits: list[dict] = []

    def on_bar(self, bar: dict, ctx) -> Signal:
        raise NotImplementedError

    def on_open(self) -> None:
        """开仓后清空目标价与部分止盈事件（由子类在开仓信号时设置 sl/tp）。"""
        self.sl_px = None
        self.tp_px = None
        self.partial_exits = []

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

    def evaluate_exits(self, bar: dict, side: str) -> Optional[tuple[float, str, float]]:
        """按本根 K 线检查离场事件，返回 (成交价, 原因, 平仓比例 frac)。

        约定（引擎与回测共用同一调用）：
        - 止损/硬止盈整仓先于部分止盈；
        - 部分止盈事件命中后从内部表中移除（剩余仓位继续管理，策略状态不清）；
        - 整仓离场事件由调用方在仓位平尽后调 on_open() 复位。
        基类默认 = 旧 check_tp_sl 语义（全平，frac=1.0），旧策略零改动。
        """
        if side == "long":
            if self.sl_px is not None and bar["l"] <= self.sl_px:
                return self.sl_px, "止损", 1.0
            if self.tp_px is not None and bar["h"] >= self.tp_px:
                return self.tp_px, "止盈", 1.0
        else:
            if self.sl_px is not None and bar["h"] >= self.sl_px:
                return self.sl_px, "止损", 1.0
            if self.tp_px is not None and bar["l"] <= self.tp_px:
                return self.tp_px, "止盈", 1.0
        return self._partial_exit_hit(bar, side)

    def _partial_exit_hit(self, bar: dict, side: str) -> Optional[tuple[float, str, float]]:
        """部分止盈事件表命中检查：命中即消费该事件，返回 (px, reason, frac)。"""
        for ev in self.partial_exits:
            if side == "long":
                hit = bar["h"] >= ev["px"]
            else:
                hit = bar["l"] <= ev["px"]
            if hit:
                self.partial_exits.remove(ev)
                return ev["px"], ev["reason"], ev["frac"]
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

    def describe(self, history: list[dict], position) -> dict:
        """看板：等待条件 = 顺势方向 + RSI 极值。展示趋势线与当前 RSI。"""
        if getattr(position, "is_open", False):
            return {"pos": position.side}
        tail = history[-(self.sma_len * 2 + 60):]
        closes = [b["c"] for b in tail]
        sma = calc_sma(closes, self.sma_len)
        rsi = calc_rsi(closes, self.rsi_len)
        return {"pos": "flat", "sma": sma, "rsi": rsi,
                "note": f"做多需：价格在 SMA{self.sma_len} 上方 且 RSI{self.rsi_len} 跌破 {self.lo:.0f}；"
                        f"做空需：价格在 SMA{self.sma_len} 下方 且 RSI{self.rsi_len} 冲上 {self.hi:.0f}"}


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


class HybridRangeTrend(Strategy):
    """震荡/趋势双模式：通道内做均值回归，通道突破后切换趋势骑行。

    - 通道外收盘突破前 entry_len 根高低(且顺 sma_len 大趋势) -> TREND 模式：
      持单用 chandelier 止损(3×ATR 逐bar上移)，收盘破短均线(exit_ma)或止损离场
    - 通道内且 RSI(2) 超卖/超买 -> RANGE 模式：回归到短均线即离场
    单仓，先判突破后判回归；记录入场模式决定离场规则。
    """

    name = "hybrid_range_trend"

    def __init__(self, params: dict):
        super().__init__(params)
        self.entry_len = int(params.get("entry_len", 40))
        self.rsi_len = int(params.get("rsi_len", 2))
        self.lo = float(params.get("lo", 10.0))
        self.hi = float(params.get("hi", 90.0))
        self.sma_len = int(params.get("sma_len", 200))
        self.exit_ma = int(params.get("exit_ma", 20))
        self.atr_period = int(params.get("atr_period", 14))
        self.sl_mult = float(params.get("sl_mult", 3.0))
        self._mode = -1  # 0=range 1=trend

    def on_bar(self, bar: dict, ctx) -> Signal:
        tail = ctx.history[-(self.sma_len + self.entry_len + 60):]
        closes = [b["c"] for b in tail]
        if len(closes) < self.sma_len + 5:
            return Signal("none", "预热中")
        n = len(closes)
        prev = tail[:-1]
        hi40 = max(b["h"] for b in prev[-self.entry_len:])
        lo40 = min(b["l"] for b in prev[-self.entry_len:])
        sma = calc_sma(closes, self.sma_len)
        rsi = calc_rsi(closes, self.rsi_len)
        if None in (sma, rsi):
            return Signal("none")
        c = bar["c"]
        atr = calc_atr(ctx.history, self.atr_period) or c * 0.005
        ema_exit = calc_sma(closes, self.exit_ma)
        pos = ctx.position
        if pos.is_open:
            # 模式内离场
            if self._mode == 1:  # trend: 破短均线离场
                if (pos.side == "long" and c < ema_exit) or (pos.side == "short" and c > ema_exit):
                    return Signal("close", "趋势破坏")
                stop = c - self.sl_mult * atr if pos.side == "long" else c + self.sl_mult * atr
                self.sl_px = stop
                return Signal("none")
            # range: 回归到短均线(5)或 RSI 回中
            ema5 = calc_sma(closes, 5)
            if pos.side == "long" and (c >= (ema5 or c) or rsi > 50):
                return Signal("close", "回归到位")
            if pos.side == "short" and (c <= (ema5 or c) or rsi < 50):
                return Signal("close", "回归到位")
            return Signal("none")
        # 空仓：先突破(趋势)后回归
        prev_c = closes[-2]
        broke_hi = c > hi40 and prev_c <= hi40 and c > sma
        broke_lo = c < lo40 and prev_c >= lo40 and c < sma
        if broke_hi:
            self._mode = 1
            self.sl_px = c - self.sl_mult * atr
            self.tp_px = None
            return Signal("open_long", "通道突破")
        if broke_lo:
            self._mode = 1
            self.sl_px = c + self.sl_mult * atr
            self.tp_px = None
            return Signal("open_short", "通道跌破")
        if c < lo40 and c > sma and rsi < self.lo:
            self._mode = 0
            self.sl_px = c - 2.0 * atr
            self.tp_px = None
            return Signal("open_long", f"通道内超卖 RSI={rsi:.0f}")
        if c > hi40 and c < sma and rsi > self.hi:
            self._mode = 0
            self.sl_px = c + 2.0 * atr
            self.tp_px = None
            return Signal("open_short", f"通道内超买 RSI={rsi:.0f}")
        return Signal("none")

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        atr = calc_atr(history, self.atr_period) or entry * 0.005
        self.sl_px = entry - self.sl_mult * atr if side == "long" else entry + self.sl_mult * atr
        self.tp_px = None


# ---------- ma_macd：MA+MACD 两情相悦 × 1H 生命线（VC_kxs 体系机械版） ----------
H1_MS = 3_600_000


def h1_closed_tail(history15: list[dict], max_h1: int = 150) -> list[dict]:
    """从 15m 已收盘历史尾部聚合出最多 max_h1 个已收盘 1H 桶（进行中桶丢弃）。"""
    need = max_h1 * 4 + 8
    tail = history15[-need:] if len(history15) > need else history15
    return okx_math.aggregate_closed(tail, H1_MS)[-max_h1:]


class MaMacd(Strategy):
    """MA+MACD 双确认 × 1H 生命线 × 四位置 × 136 分仓 × 锁利离场（机械版）。

    帖子规则 -> 代码映射（可回测解读，v1，参数可调）：
    - 周期栈：15m 已收盘 bar 决策；1H 由 15m 内部聚合（h1_closed_tail）
    - 两情相悦 = 同根 15m bar：MA快上穿MA慢 且 MACD DIF 上穿 DEA（做多；死叉镜像）
    - 四位置模板（enable_t1..t4 可关）：
      T1 底部反弹多：1H 收盘在生命线下、贴近摆动低点 -> 金叉做多（目标回生命线）
      T2 高位回调空：1H 收盘在生命线上、贴近摆动高点 -> 死叉做空（镜像）
      T3 空中加油多：1H 收盘在生命线上、15m 回踩生命线附近 -> 金叉做多（目标前高）
      T4 确认位空：  1H 收盘在生命线下、15m 反抽生命线附近 -> 死叉做空（镜像）
    - 首次两情相悦只武装等第二次；二次确认须同向且未击穿首叉摆动极值（confirm=second）
    - 放量跳过；持仓遇放量反向大 bar 直接全平
    - 136：首层 leg1(10%) 进场；持仓中再现金叉/死叉补 leg2(30%)；
      向有利方向延伸 ≥ l3_atr×ATR 补 leg3(60%)；条件不满足则放弃后续批次
    - 离场：初始 ATR 止损 -> 浮盈≥be_atr×ATR 移保本 -> chandelier 追踪；
      目标位（生命线/前高前低）部分止盈 pt_frac；顺线仓 1H 收盘反向穿越清仓
    """

    name = "ma_macd"

    def __init__(self, params: dict):
        super().__init__(params)
        f = lambda k, d: float(params.get(k, d))
        i = lambda k, d: int(params.get(k, d))
        self.ma_fast = i("ma_fast", 5)
        self.ma_slow = i("ma_slow", 10)
        self.macd_fast = i("macd_fast", 12)
        self.macd_slow = i("macd_slow", 26)
        self.macd_sig = i("macd_sig", 9)
        self.gate_ma = i("gate_ma", 60)      # 1H 生命线 MA 周期
        self.h1_max = i("h1_max", 150)
        self.sw_look = i("sw_look", 24)      # 支撑/压力/前高前低 用近 N 根 1H
        self.atr_p = i("atr_p", 14)
        self.zone_mult = f("zone_mult", 1.0)   # 贴近判定 = atr1h × mult
        self.confirm = str(params.get("confirm", "second"))  # any | second
        self.vol_spike_mult = f("vol_spike_mult", 2.5)       # 0=关闭放量过滤
        self.enable_t1 = params.get("enable_t1", True)
        self.enable_t2 = params.get("enable_t2", True)
        self.enable_t3 = params.get("enable_t3", True)
        self.enable_t4 = params.get("enable_t4", True)
        self.leg1 = f("leg1_pct", 0.10)
        self.leg2 = f("leg2_pct", 0.30)
        self.leg3 = f("leg3_pct", 0.60)
        self.l3_atr_mult = f("l3_atr_mult", 1.2)  # 主仓（第3批）确认距离
        self.stop_atr_mult = f("stop_atr_mult", 1.8)    # 初始保护止损
        self.be_atr_mult = f("be_atr_mult", 1.0)        # 浮盈达 X×ATR 移保本
        self.trail_atr_mult = f("trail_atr_mult", 1.5)  # 保本后 chandelier 距离
        self.pt_frac = f("pt_frac", 0.5)                # 目标位部分止盈比例
        self.armed_max_bars = i("armed_max_bars", 96)   # 武装存活上限（1天15m）
        self.armed_look = i("armed_look", 12)           # 武装水印回看（首推动摆动极值）
        self._reset_state()

    # ---- 持仓/等待期状态（on_open 复位） ----
    def _reset_state(self) -> None:
        self._armed: Optional[str] = None   # 等待二次确认的方向
        self._armed_tpl: str = ""           # 武装时的位置模板（决定失效规则）
        self._armed_swing: float = 0.0      # 首推动摆动极值（多=低/空=高），跌破即失效
        self._armed_bars: int = 0           # 武装存活 bar 数（防陈旧信号）
        self._legs: int = 0                 # 已进批次数
        self._tpl: str = ""                 # t1/t2/t3/t4/recovered
        self._hh: float = 0.0               # 持仓以来有利方向极值（多=高/空=低）
        self._be_done: bool = False
        self._recovered: bool = False       # 重启恢复仓：不再补批

    def on_open(self) -> None:
        super().on_open()
        self._reset_state()

    # ---------- 指标工具 ----------
    def _smacross(self, closes: list[float]) -> tuple[bool, bool]:
        """15m MA 金叉/死叉（与上一根比较）。"""
        if len(closes) < self.ma_slow + 2:
            return False, False
        nf, ns = self.ma_fast, self.ma_slow
        fp = sum(closes[-(nf + 1):-1]) / nf
        fc = sum(closes[-nf:]) / nf
        sp = sum(closes[-(ns + 1):-1]) / ns
        sc = sum(closes[-ns:]) / ns
        return fc > sc and fp <= sp, fc < sc and fp >= sp

    def _macdcross(self, closes: list[float]) -> tuple[bool, bool]:
        """15m MACD DIF/DEA 金叉/死叉。"""
        need = self.macd_slow + self.macd_sig + 2
        if len(closes) < need:
            return False, False
        ef = ema(closes, self.macd_fast)
        es = ema(closes, self.macd_slow)
        n = len(es)
        dif = [ef[i + (self.macd_slow - self.macd_fast)] - es[i] for i in range(n)]
        dea = ema(dif, self.macd_sig)
        if len(dea) < 2:
            return False, False
        up = dif[-1] > dea[-1] and dif[-2] <= dea[-2]
        down = dif[-1] < dea[-1] and dif[-2] >= dea[-2]
        return up, down

    def _vol_spike(self, history: list[dict], bar: dict) -> bool:
        mult = self.vol_spike_mult
        if mult <= 0:
            return False
        tail = [b["v"] for b in history[-21:-1]]
        if len(tail) < 20:
            return False
        avg = sum(tail) / len(tail)
        return avg > 0 and bar["v"] > avg * mult

    # ---------- 1H 结构 ----------
    def _context(self, history: list[dict]):
        """(h1, closes15, line, atr1, support, resistance)；预热不足返回 None。"""
        h1 = h1_closed_tail(history, self.h1_max)
        if len(h1) < self.gate_ma + 3:
            return None
        closes = [b["c"] for b in history]
        if len(closes) < self.macd_slow + self.macd_sig + 2:
            return None
        line = calc_sma([b["c"] for b in h1], self.gate_ma)
        atr1 = calc_atr(h1, self.atr_p)
        if line is None or atr1 is None or atr1 <= 0:
            return None
        sw = h1[-self.sw_look:]
        support = min(b["l"] for b in sw)
        resistance = max(b["h"] for b in sw)
        return h1, closes, line, atr1, support, resistance

    def _zone(self, atr1: float, px: float) -> float:
        z = atr1 * self.zone_mult
        return z if z > px * 0.001 else px * 0.002

    def _template(self, c: float, h1last: float, line: float, support: float,
                  resistance: float, zone: float) -> str:
        """当前 15m 收盘价命中的位置模板（空串 = 不在任何位置，不交易）。"""
        if h1last < line:
            if self.enable_t1 and support - zone * 0.6 <= c <= support + zone * 1.2:
                return "t1"
            if self.enable_t4 and line - zone * 1.2 <= c <= line + zone * 0.5:
                return "t4"
        else:
            if self.enable_t3 and line - zone * 0.5 <= c <= line + zone * 1.2:
                return "t3"
            if self.enable_t2 and resistance - zone * 1.2 <= c <= resistance + zone * 0.6:
                return "t2"
        return ""

    @staticmethod
    def _long_target(entry: float, line: float, resistance: float) -> Optional[float]:
        cands = [x for x in (line, resistance) if x and x > entry * 1.0005]
        return min(cands) if cands else None

    @staticmethod
    def _short_target(entry: float, line: float, support: float) -> Optional[float]:
        cands = [x for x in (line, support) if x and 0 < x < entry * 0.9995]
        return max(cands) if cands else None

    # ---------- 主入口 ----------
    def on_bar(self, bar: dict, ctx) -> Signal:
        ctx0 = self._context(ctx.history)
        if ctx0 is None:
            return Signal("none", "预热中(等1H)")
        h1, closes, line, atr1, support, resistance = ctx0
        if ctx.position.is_open:
            return self._manage(ctx.position, bar, ctx.history, h1, closes, line, atr1)
        return self._hunt(bar, ctx.history, closes, h1, line, atr1, support, resistance)

    def _hunt(self, bar, history, closes, h1, line, atr1, support, resistance) -> Signal:
        zone = self._zone(atr1, bar["c"])
        c = bar["c"]
        h1last = h1[-1]["c"]
        ma_up, ma_down = self._smacross(closes)
        macd_up, macd_down = self._macdcross(closes)
        gx = ma_up and macd_up          # 两情相悦金叉
        dx = ma_down and macd_down      # 两情相悦死叉
        spike = self._vol_spike(history, bar)

        # 武装失效（帖子语义：等"第二次同向"而非每次回落都重来）：
        # 中间小反向交叉不断言失败，只有 结构破坏 / 生命线翻向不利侧 / 超时 才失效
        if self._armed:
            self._armed_bars += 1
            dead = self._armed_bars > self.armed_max_bars
            if self._armed == "long":
                if self._armed_tpl == "t1":       # 底部反弹：涨到生命线=目标达成
                    dead = dead or h1last >= line
                else:                             # t3 空中加油：跌破生命线=方向证伪
                    dead = dead or h1last < line
                dead = dead or bar["l"] < self._armed_swing
            else:
                if self._armed_tpl == "t2":
                    dead = dead or h1last <= line
                else:                             # t4
                    dead = dead or h1last > line
                dead = dead or bar["h"] > self._armed_swing
            if dead:
                self._armed = None

        tpl = self._template(c, h1last, line, support, resistance, zone)

        if self.confirm == "second":
            # 二次确认进场：同向交叉 + 已武装 + 当前仍命中模板
            if gx and not spike and self._armed == "long" and tpl in ("t1", "t3"):
                return self._open("long", tpl, bar, line, support, resistance, atr1)
            if dx and not spike and self._armed == "short" and tpl in ("t2", "t4"):
                return self._open("short", tpl, bar, line, support, resistance, atr1)
            # 首次信号 -> 只武装（不重复覆盖既有武装）
            if self._armed is None and gx and not spike and tpl in ("t1", "t3"):
                self._armed, self._armed_tpl = "long", tpl
                window = history[-self.armed_look:]
                self._armed_swing = min(b["l"] for b in window)
                self._armed_bars = 0
            elif self._armed is None and dx and not spike and tpl in ("t2", "t4"):
                self._armed, self._armed_tpl = "short", tpl
                window = history[-self.armed_look:]
                self._armed_swing = max(b["h"] for b in window)
                self._armed_bars = 0
            return Signal("none")
        # confirm=any：模板内两情相悦直接进
        if gx and not spike and tpl in ("t1", "t3"):
            return self._open("long", tpl, bar, line, support, resistance, atr1)
        if dx and not spike and tpl in ("t2", "t4"):
            return self._open("short", tpl, bar, line, support, resistance, atr1)
        return Signal("none")

    def _open(self, side: str, tpl: str, bar, line, support, resistance,
              atr1: float) -> Signal:
        entry = bar["c"]
        self._reset_state()
        self._legs = 1
        self._tpl = tpl
        if side == "long":
            self.sl_px = entry - atr1 * self.stop_atr_mult
            self._hh = bar["h"]
            tgt = self._long_target(entry, line, resistance)
            if tgt is not None:
                self.partial_exits = [{"px": tgt, "frac": self.pt_frac,
                                       "reason": "目标位部分止盈"}]
            note = f"{tpl.upper()}金叉进场做多 生命线{line:.0f}" + \
                   (f" 目标{tgt:.0f}" if tgt else "")
            return Signal("open_long", note, frac=self.leg1)
        self.sl_px = entry + atr1 * self.stop_atr_mult
        self._hh = bar["l"]
        tgt = self._short_target(entry, line, support)
        if tgt is not None:
            self.partial_exits = [{"px": tgt, "frac": self.pt_frac,
                                   "reason": "目标位部分止盈"}]
        note = f"{tpl.upper()}死叉进场做空 生命线{line:.0f}" + \
               (f" 目标{tgt:.0f}" if tgt else "")
        return Signal("open_short", note, frac=self.leg1)

    def _manage(self, pos, bar, history, h1, closes, line, atr1: float) -> Signal:
        """持仓管理：保本/追踪移动止损、放量反向、顺线反向清仓、按批补仓。"""
        c, entry = bar["c"], pos.entry
        h1last = h1[-1]["c"]
        ma_up, ma_down = self._smacross(closes)
        macd_up, macd_down = self._macdcross(closes)
        spike = self._vol_spike(history, bar)

        if pos.side == "long":
            self._hh = max(self._hh, bar["h"])
            if not self._be_done and self.sl_px is not None and \
                    c - entry >= self.be_atr_mult * atr1:
                self.sl_px = max(self.sl_px, entry)
                self._be_done = True
            if self._be_done and self.sl_px is not None:
                cand = self._hh - self.trail_atr_mult * atr1
                if cand > self.sl_px:
                    self.sl_px = cand
            # 放量大反向 bar（与持仓相反且幅度足够）-> 直接止盈
            if spike and bar["l"] <= c - atr1 * 0.5:
                return Signal("close", "放量大反向直接止盈")
            # 顺线仓（t3）：1H 收盘跌破生命线 -> 高周期反向清仓
            if self._tpl == "t3" and h1last < line:
                return Signal("close", "高周期反向：1H收盘跌破生命线清多")
            if self._legs == 1 and not self._recovered and not spike \
                    and ma_up and macd_up:
                return Signal("add_long", "再现金叉二次确认补仓", frac=self.leg2)
            if self._legs == 2 and not self._recovered and not spike \
                    and c - entry >= self.l3_atr_mult * atr1:
                return Signal("add_long", "趋势延伸确认补主仓", frac=self.leg3)
        else:
            self._hh = min(self._hh, bar["l"])
            if not self._be_done and self.sl_px is not None and \
                    entry - c >= self.be_atr_mult * atr1:
                self.sl_px = min(self.sl_px, entry)
                self._be_done = True
            if self._be_done and self.sl_px is not None:
                cand = self._hh + self.trail_atr_mult * atr1
                if cand < self.sl_px:
                    self.sl_px = cand
            if spike and bar["h"] >= c + atr1 * 0.5:
                return Signal("close", "放量大反向直接止盈")
            if self._tpl == "t4" and h1last > line:
                return Signal("close", "高周期反向：1H收盘升破生命线清空")
            if self._legs == 1 and not self._recovered and not spike \
                    and ma_down and macd_down:
                return Signal("add_short", "再现死叉二次确认补仓", frac=self.leg2)
            if self._legs == 2 and not self._recovered and not spike \
                    and entry - c >= self.l3_atr_mult * atr1:
                return Signal("add_short", "趋势延伸确认补主仓", frac=self.leg3)
        return Signal("none")

    def arm_open_stop(self, history: list[dict], side: str, entry: float) -> None:
        """重启恢复持仓：ATR 保护止损；恢复仓不补批、不带目标事件。"""
        h1 = h1_closed_tail(history, self.h1_max)
        atr1 = calc_atr(h1, self.atr_p) or entry * 0.005
        self.sl_px = entry - atr1 * self.stop_atr_mult if side == "long" \
            else entry + atr1 * self.stop_atr_mult
        self.tp_px = None
        self.partial_exits = []
        self._reset_state()
        self._recovered = True
        self._tpl = "recovered"
        self._hh = entry

    def describe(self, history: list[dict], position) -> dict:
        if getattr(position, "is_open", False):
            return {"pos": position.side, "legs": self._legs}
        ctx0 = self._context(history)
        if ctx0 is None:
            return {"pos": "flat", "note": "预热中（等 1H MA60 数据足够）"}
        h1, _c, line, atr1, support, resistance = ctx0
        above = h1[-1]["c"] >= line
        armed_txt = {"long": "（首金叉已现，等二次确认做多）",
                     "short": "（首死叉已现，等二次确认做空）"}.get(self._armed, "")
        return {"pos": "flat",
                "note": f"1H生命线 {line:.1f}，现价在其{'上方' if above else '下方'}"
                        f" · 支撑 {support:.1f} 压力 {resistance:.1f}"
                        f" · 等两情相悦信号{armed_txt}"}


