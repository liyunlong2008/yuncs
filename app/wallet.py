"""纸盘钱包（DryRunWallet）：独立记账，与真实账户完全隔离。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Wallet:
    initial_balance: float
    balance: float            # 可用现金（不含锁定保证金）
    margin_locked: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    realized_pnl: float = 0.0

    @classmethod
    def new(cls, initial_balance: float) -> "Wallet":
        return cls(initial_balance=initial_balance, balance=initial_balance)

    def lock_margin(self, margin: float) -> None:
        self.balance -= margin
        self.margin_locked += margin

    def unlock_margin(self, margin: float) -> None:
        self.margin_locked = max(0.0, self.margin_locked - margin)
        self.balance += margin

    def pay_fee(self, fee: float) -> None:
        self.balance -= fee
        self.fees_paid += fee

    def pay_funding(self, amount: float) -> None:
        self.balance -= amount
        self.funding_paid += amount

    def add_pnl(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.balance += pnl

    def equity(self, unrealized_pnl: float) -> float:
        """账户权益 = 可用现金 + 锁定保证金 + 未实现盈亏。"""
        return self.balance + self.margin_locked + unrealized_pnl
