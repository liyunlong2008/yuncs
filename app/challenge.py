"""轮次规则引擎：动态出局线（无胜利点玩法核心）。

- 无"胜利点"：不存在达到某倍数就结算的终点，核心是持续正确操作、把权益做大
- 动态出局线：出局线 = 运营峰值 × (1 - 容忍率)
- 容忍率随权益倍数平滑收紧（无开关阈值、无悬崖）：
  1x → base_drawdown_pct，线性过渡到 tight_start_multiple 倍 → tight_drawdown_pct，之后保持
- 权益触及出局线 → 本轮结束（锁利或止损，按结束倍数记录），进程内自动开新一轮

回测、纸盘、实盘共用同一份代码。
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional


class Status(str, Enum):
    RUNNING = "running"
    GUARD = "guard"        # 动态出局线触发，本轮结束（结果可能为正锁利或负止损）
    TIMEOUT = "timeout"    # 可选时长限制超时
    STOPPED = "stopped"    # 手动停止


class ChallengeConfig:
    def __init__(self, initial_balance: float, base_drawdown_pct: float = 30.0,
                 tight_drawdown_pct: float = 10.0, tight_start_multiple: float = 2.0,
                 duration_hours: float = 0.0, timeframe: str = "1m"):
        self.initial_balance = initial_balance
        self.base_drawdown_pct = base_drawdown_pct
        self.tight_drawdown_pct = tight_drawdown_pct
        self.tight_start_multiple = tight_start_multiple
        self.duration_hours = duration_hours
        self.timeframe = timeframe

    def tolerance(self, multiple: float) -> float:
        """回撤容忍率（%）：权益倍数越高容忍越小，1x→base、2x(默认)→tight，线性无悬崖。"""
        if multiple <= 1.0:
            return self.base_drawdown_pct
        if multiple >= self.tight_start_multiple:
            return self.tight_drawdown_pct
        k = (multiple - 1.0) / (self.tight_start_multiple - 1.0)
        return self.base_drawdown_pct - (self.base_drawdown_pct - self.tight_drawdown_pct) * k


class Challenge:
    """单轮状态机。每轮独立：initial/peak 从本轮起点算。"""

    def __init__(self, cfg: ChallengeConfig):
        self.cfg = cfg
        self.status: Status = Status.RUNNING
        self.initial_balance: float = cfg.initial_balance
        self.start_ts: float = time.time()
        self.peak_equity: float = cfg.initial_balance
        self.result: Optional[str] = None
        self.guard_equity: float = 0.0  # 触发时的出局线位置

    def start_round(self, initial_balance: float) -> None:
        self.cfg.initial_balance = initial_balance
        self.initial_balance = initial_balance
        self.status = Status.RUNNING
        self.start_ts = time.time()
        self.peak_equity = initial_balance
        self.result = None
        self.guard_equity = 0.0

    @property
    def multiple(self) -> float:
        if self.initial_balance <= 0:
            return 1.0
        return self.peak_equity / self.initial_balance

    def tolerance(self) -> float:
        return self.cfg.tolerance(self.multiple)

    def guard_level(self) -> float:
        """当前出局线（权益触及即结束本轮）。"""
        return self.peak_equity * (1.0 - self.tolerance() / 100.0)

    def update(self, equity: float, now: Optional[float] = None) -> Status:
        """每 tick/bar 调用；返回非 RUNNING 即本轮结束。"""
        if self.status != Status.RUNNING:
            return self.status
        now = now if now is not None else time.time()
        if equity > self.peak_equity:
            self.peak_equity = equity
        guard = self.guard_level()
        if equity <= guard:
            self.status = Status.GUARD
            self.guard_equity = guard
            end_multiple = equity / self.initial_balance if self.initial_balance > 0 else 0.0
            kind = "锁利" if end_multiple > 1.0 else "止损"
            self.result = (f"动态线触发[{kind}]: 峰值 {self.peak_equity:.4f} -> "
                           f"{equity:.4f} (结束 {end_multiple:.2f} 倍)")
        elif self.cfg.duration_hours > 0 and now - self.start_ts >= self.cfg.duration_hours * 3600.0:
            self.status = Status.TIMEOUT
            self.result = f"单轮超时结算: {self.cfg.duration_hours} 小时"
        return self.status

    def drawdown_pct(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity * 100.0

    def progress(self, equity: float) -> dict:
        """看板展示：本轮初始/当前权益/倍数/峰值/容忍率/出局线。"""
        elapsed = time.time() - self.start_ts
        remaining = 0
        if self.cfg.duration_hours > 0:
            remaining = max(0.0, self.cfg.duration_hours * 3600.0 - elapsed)
        multiple = equity / self.initial_balance if self.initial_balance > 0 else 1.0
        return {
            "status": self.status.value,
            "result": self.result,
            "initial_balance": round(self.initial_balance, 4),
            "equity": round(equity, 4),
            "multiple": round(multiple, 4),
            "peak_equity": round(self.peak_equity, 4),
            "tolerance_pct": round(self.tolerance(), 2),
            "guard_level": round(self.guard_level(), 4),
            "drawdown_pct": round(self.drawdown_pct(equity), 2),
            "remaining_sec": int(remaining),
        }
