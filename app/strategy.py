"""策略框架：on_bar 决策 + 内置 trend_ema。

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


class TrendEma(Strategy):
    """EMA 快慢交叉顺势 + ATR 动态止损 + 固定盈亏比止盈。"""

    name = "trend_ema"

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
