"""挑战规则引擎：10u 战神玩法 —— 翻倍目标 + 回撤出局，不限时（可选时长）。

回测、纸盘、实盘共用同一份代码。
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional


class Status(str, Enum):
    RUNNING = "running"
    WON = "won"          # 达到翻倍目标
    LOST = "lost"        # 回撤出局
    TIMEOUT = "timeout"  # 可选时长限制超时
    STOPPED = "stopped"  # 手动停止


class ChallengeConfig:
    def __init__(self, initial_balance: float, target_multiple: float,
                 max_drawdown_pct: float, duration_hours: float = 0.0,
                 timeframe: str = "1m"):
        self.initial_balance = initial_balance
        self.target_multiple = target_multiple
        self.max_drawdown_pct = max_drawdown_pct
        self.duration_hours = duration_hours
        self.timeframe = timeframe

    @property
    def target_equity(self) -> float:
        return self.initial_balance * self.target_multiple


class Challenge:
    def __init__(self, cfg: ChallengeConfig):
        self.cfg = cfg
        self.status: Status = Status.RUNNING
        self.start_ts: float = time.time()
        self.peak_equity: float = cfg.initial_balance
        self.result: Optional[str] = None

    def update(self, equity: float, now: Optional[float] = None) -> Status:
        """每 tick/bar 调用；返回非 RUNNING 即挑战终结。"""
        if self.status != Status.RUNNING:
            return self.status
        now = now if now is not None else time.time()
        if equity > self.peak_equity:
            self.peak_equity = equity
        if equity >= self.cfg.target_equity:
            self.status = Status.WON
            self.result = f"翻倍目标达成: {self.cfg.target_multiple} 倍（{self.cfg.initial_balance} -> {self.cfg.target_equity}）"
        elif equity <= self.peak_equity * (1.0 - self.cfg.max_drawdown_pct / 100.0):
            self.status = Status.LOST
            self.result = f"回撤出局: 从峰值 {self.peak_equity:.4f} 回撤 {self.cfg.max_drawdown_pct}%"
        elif self.cfg.duration_hours > 0 and now - self.start_ts >= self.cfg.duration_hours * 3600.0:
            self.status = Status.TIMEOUT
            self.result = f"超时结算: {self.cfg.duration_hours} 小时"
        return self.status

    def drawdown_pct(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity * 100.0

    def progress(self, equity: float) -> dict:
        """看板展示：进度/回撤/剩余时间。"""
        target = self.cfg.target_equity
        total_gap = target - self.cfg.initial_balance
        done = equity - self.cfg.initial_balance
        progress_pct = 100.0 * done / total_gap if total_gap != 0 else 100.0
        elapsed = time.time() - self.start_ts
        remaining = 0
        if self.cfg.duration_hours > 0:
            remaining = max(0.0, self.cfg.duration_hours * 3600.0 - elapsed)
        return {
            "status": self.status.value,
            "result": self.result,
            "initial_balance": self.cfg.initial_balance,
            "equity": round(equity, 4),
            "target_multiple": self.cfg.target_multiple,
            "target_equity": round(target, 4),
            "progress_pct": round(max(0.0, min(progress_pct, 100.0)), 2),
            "peak_equity": round(self.peak_equity, 4),
            "drawdown_pct": round(self.drawdown_pct(equity), 2),
            "max_drawdown_pct": self.cfg.max_drawdown_pct,
            "drawdown_floor": round(self.peak_equity * (1.0 - self.cfg.max_drawdown_pct / 100.0), 4),
            "remaining_sec": int(remaining),
        }
