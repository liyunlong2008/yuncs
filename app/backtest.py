"""玩法适配回测：逐 K 线回放 10u 战神挑战。

与纸盘/实盘共用：策略代码、挑战引擎、撮合模型（K 线 open ± 滑点）、
OKX 强平价公式、资金费结算（UTC 00:00/08:00/16:00）。

成交时序：信号用"已收盘"bar 计算（无未来函数）→ 下一根 bar 开盘价成交；
止盈/止损/强平按本根 bar 高低点精确撮合。
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from . import fills, okx_math
from .broker import Position
from .challenge import Challenge, ChallengeConfig, Status
from .config import Config
from .okx_feed import InstrumentSpec
from .strategy import Signal, TrendEma
from .wallet import Wallet


class Backtest:
    def __init__(self, cfg: Config, bars: list[dict], funding: list[dict],
                 spec: InstrumentSpec | None = None):
        self.cfg = cfg
        self.bars = bars
        self.funding = funding
        self.spec = spec or InstrumentSpec()  # 默认值即 OKX ETH-USDT-SWAP 当前规格
        self.strategy = TrendEma(cfg.strategy.params)
        self.challenge = Challenge(ChallengeConfig(
            initial_balance=cfg.challenge.initial_balance,
            target_multiple=cfg.challenge.target_multiple,
            max_drawdown_pct=cfg.challenge.max_drawdown_pct,
            duration_hours=cfg.challenge.duration_hours,
            timeframe=cfg.challenge.timeframe,
        ))
        self.wallet = Wallet.new(cfg.challenge.initial_balance)
        self.position = Position()
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self._funding_idx = 0
        self._next_funding_ts: Optional[float] = None

    def run(self) -> dict:
        if not self.bars:
            return {"error": "无K线数据"}
        # 挑战时间轴从数据起点开始
        self.challenge.start_ts = self.bars[0]["ts"] / 1000.0
        # 资金费结算时间轴
        self._next_funding_ts = self._first_funding_after(self.bars[0]["ts"])

        pending: Optional[Signal] = None
        i = 0
        for i, bar in enumerate(self.bars):
            ts = bar["ts"]
            # 1) 上一根信号的执行（本根开盘成交）
            if pending is not None:
                self._execute_signal(pending, bar)
                pending = None
            # 2) 止盈/止损（本根高低点）
            if self.position.is_open:
                hit = self.strategy.check_tp_sl(bar, self.position.side)
                if hit:
                    self._close(fill_px=hit[0], reason=hit[1])
            # 3) 强平保护（带缓冲，提前于 OKX 强平价）
            if self.position.is_open:
                stop = okx_math.buffered_liq_price(
                    self.position.side, self.position.entry, self.position.liq_px,
                    self.cfg.risk.liquidation_buffer)
                if (self.position.side == "long" and bar["l"] <= stop) or \
                   (self.position.side == "short" and bar["h"] >= stop):
                    self._close(fill_px=stop, reason="liquidation")
            # 4) 资金费结算
            if self._next_funding_ts and ts >= self._next_funding_ts:
                self._settle_funding(bar["c"])
            # 5) 权益 + 挑战
            equity = self.wallet.equity(self.position.unrealized(bar["c"]))
            status = self.challenge.update(equity, now=ts / 1000.0)
            self.equity_curve.append({
                "ts": ts, "equity": equity,
                "drawdown_pct": self.challenge.drawdown_pct(equity),
                "status": status.value,
            })
            if status != Status.RUNNING:
                i += 1
                break
            # 6) 计算下一根信号
            sig = self.strategy.on_bar(bar, _BarsCtx(self.bars[: i + 1], self.position, bar["c"]))
            if sig.action in ("open_long", "open_short") and not self.position.is_open:
                pending = sig
            elif sig.action == "close" and self.position.is_open:
                pending = sig

        # 结束：未终结则按最后一根收盘价结算剩余仓位
        if self.position.is_open:
            last = self.bars[min(i, len(self.bars) - 1)]
            self._close(fill_px=last["c"], reason="end")
        return self._report()

    # ---------- 内部 ----------
    def _execute_signal(self, sig: Signal, bar: dict) -> None:
        if self.position.is_open or sig.action not in ("open_long", "open_short"):
            return
        side = "long" if sig.action == "open_long" else "short"
        contracts, size_eth = self._size(bar["o"])
        if contracts <= 0:
            return
        fill = fills.candle_fill_price(bar, "buy" if side == "long" else "sell",
                                       self.cfg.risk.slippage_bps)
        notional = okx_math.notional(size_eth, fill)
        fee = notional * self.cfg.exchange.taker_fee
        margin = okx_math.margin_required(notional, self.cfg.risk.leverage)
        if margin > self.wallet.balance:
            return
        self.wallet.lock_margin(margin)
        self.wallet.pay_fee(fee)
        liq = okx_math.liquidation_price(side, fill, size_eth, margin,
                                         self.spec.mmr, self.cfg.exchange.taker_fee)
        self.position = Position(
            side=side, size_eth=size_eth, entry=fill, margin=margin, liq_px=liq,
            cost_usdt=okx_math.cost_including_fee(fill, size_eth, self.cfg.exchange.taker_fee),
            cost_price=okx_math.cost_price_including_fee(fill, self.cfg.exchange.taker_fee),
            fee=fee, open_ts=bar["ts"] / 1000.0,
        )

    def _close(self, fill_px: float, reason: str) -> None:
        pos = self.position
        if not pos.is_open:
            return
        fee_rate = self.cfg.exchange.taker_fee
        pnl = okx_math.unrealized_pnl(pos.side, pos.entry, fill_px, pos.size_eth)
        fee = okx_math.notional(pos.size_eth, fill_px) * fee_rate
        self.wallet.unlock_margin(pos.margin)
        self.wallet.pay_fee(fee)
        self.wallet.add_pnl(pnl)
        self.trades.append({
            "ts": pos.open_ts, "side": pos.side, "size_eth": pos.size_eth,
            "entry": pos.entry, "exit": fill_px, "pnl": pnl,
            "fee": fee + pos.fee, "funding": 0.0, "reason": reason,
        })
        self.position = Position()
        self.strategy.on_open()

    def _settle_funding(self, mark: float) -> None:
        # 用结算时刻记录的资金费率
        rate = 0.0
        while self._funding_idx < len(self.funding) and \
                self.funding[self._funding_idx]["ts"] <= self._next_funding_ts:
            rate = self.funding[self._funding_idx]["rate"]
            self._funding_idx += 1
        if self.position.is_open and rate:
            fee = okx_math.funding_fee(self.position.size_eth, mark, rate)
            self.wallet.pay_funding(fee)
            if self.trades:
                self.trades[-1]["funding"] += fee
        nxt = self._first_funding_after(self._next_funding_ts)
        self._next_funding_ts = nxt if nxt else None

    def _first_funding_after(self, ts: float) -> Optional[float]:
        for f in self.funding:
            if f["ts"] > ts:
                return f["ts"]
        return None

    def _size(self, price: float) -> tuple[float, float]:
        notional = min(self.cfg.risk.margin_per_trade * self.cfg.risk.leverage,
                       self.cfg.risk.max_notional)
        eth = notional / price if price > 0 else 0.0
        contracts = okx_math.round_to_lot(
            okx_math.eth_to_contracts(eth, self.spec.ct_val), self.spec.lot_sz)
        if contracts < self.spec.min_sz:
            return 0, 0
        return contracts, okx_math.contracts_to_eth(contracts, self.spec.ct_val)

    def _report(self) -> dict:
        equity = self.wallet.equity(0.0)
        pnls = [t["pnl"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        liq_count = sum(1 for t in self.trades if t["reason"] == "liquidation")
        peak = max((s["equity"] for s in self.equity_curve), default=self.cfg.challenge.initial_balance)
        max_dd = max((s["drawdown_pct"] for s in self.equity_curve), default=0.0)
        return {
            "challenge_status": self.challenge.status.value,
            "challenge_result": self.challenge.result,
            "initial_balance": self.cfg.challenge.initial_balance,
            "final_equity": round(equity, 4),
            "total_pnl": round(equity - self.cfg.challenge.initial_balance, 4),
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(100.0 * len(wins) / len(pnls), 1) if pnls else 0.0,
            "total_fees": round(self.wallet.fees_paid, 4),
            "total_funding": round(self.wallet.funding_paid, 4),
            "liquidation_count": liq_count,
            "peak_equity": round(peak, 4),
            "max_drawdown_pct": round(max_dd, 2),
            "bars": len(self.bars),
            "elapsed_hours": round((self.equity_curve[-1]["ts"] - self.bars[0]["ts"]) / 3.6e6, 2)
            if self.equity_curve else 0.0,
        }


class _BarsCtx:
    """最小化策略上下文（回测内联，避免依赖 engine.Ctx）。"""

    def __init__(self, history, position, price):
        self.history = history
        self.position = position
        self.price = price
