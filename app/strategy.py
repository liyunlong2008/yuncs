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
