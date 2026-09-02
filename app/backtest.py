"""玩法适配回测：按轮次逐 K 线回放（无胜利点玩法）。

与纸盘/实盘共用：策略代码、轮次引擎（动态出局线）、撮合模型、资金费、强平。

- 无胜利点：轮次在动态出局线触发（GUARD）或超时（TIMEOUT）时结束，记录结束倍数，
  随即自动开新一轮（重置初始资金），直到数据跑完
- 成交时序：信号用"已收盘"bar 计算（无未来函数）→ 下一根 bar 开盘价成交；
  止盈/止损/强平/出局线按本根 bar 高低点或收盘精确撮合
"""
from __future__ import annotations

from typing import Optional

from . import fills, okx_math
from .broker import Position
from .challenge import Challenge, ChallengeConfig, Status
from .config import Config
from .okx_feed import InstrumentSpec
from .strategy import Signal, create_strategy
from .wallet import Wallet


class Backtest:
    def __init__(self, cfg: Config, bars: list[dict], funding: list[dict],
                 spec: InstrumentSpec | None = None):
        self.cfg = cfg
        self.bars = bars
        self.funding = funding
        self.spec = spec or InstrumentSpec()  # 默认值即 OKX ETH-USDT-SWAP 当前规格
        self.strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
        initial = cfg.challenge.initial_balance
        if initial <= 0:
            raise ValueError("回测 [challenge].initial_balance 必须 > 0")
        self.challenge = Challenge(ChallengeConfig(
            initial_balance=initial,
            base_drawdown_pct=cfg.challenge.base_drawdown_pct,
            tight_drawdown_pct=cfg.challenge.tight_drawdown_pct,
            tight_start_multiple=cfg.challenge.tight_start_multiple,
            duration_hours=cfg.challenge.duration_hours,
            timeframe=cfg.challenge.timeframe,
        ))
        self.wallet = Wallet.new(initial)
        self.position = Position()
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self.rounds: list[dict] = []
        self._round_no = 0
        self._funding_idx = 0
        self._next_funding_ts: Optional[float] = None
        self._max_equity_seen = initial

    def run(self) -> dict:
        if not self.bars:
            return {"error": "无K线数据"}
        self.challenge.start_ts = self.bars[0]["ts"] / 1000.0
        self._next_funding_ts = self._first_funding_after(self.bars[0]["ts"])

        pending: Optional[Signal] = None
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
            # 5) 权益 + 轮次引擎
            equity = self.wallet.equity(self.position.unrealized(bar["c"]))
            self._max_equity_seen = max(self._max_equity_seen, equity)
            status = self.challenge.update(equity, now=ts / 1000.0)
            self.equity_curve.append({
                "ts": ts, "equity": equity,
                "drawdown_pct": self.challenge.drawdown_pct(equity),
                "status": status.value,
            })
            if status != Status.RUNNING:
                # 出局线/超时触发：先平掉剩余仓位，记录本轮，开新一轮
                if self.position.is_open:
                    self._close(fill_px=bar["c"], reason=f"guard({self.challenge.guard_equity:.4f})"
                               if status == Status.GUARD else "timeout")
                self._record_round(status, bar)
                self._reset_round(bar)
                pending = None
                continue
            # 6) 计算下一根信号
            sig = self.strategy.on_bar(bar, _BarsCtx(self.bars[: i + 1], self.position, bar["c"]))
            if sig.action in ("open_long", "open_short") and not self.position.is_open:
                pending = sig
            elif sig.action == "close" and self.position.is_open:
                pending = sig

        # 收尾：数据跑完仍未触线 -> 记录最后一轮（若末根 bar 刚结束一轮则跳过）
        if not self.rounds or self.rounds[-1]["ts"] != self.bars[-1]["ts"]:
            self._record_round(Status.RUNNING, self.bars[-1], finished=False)
        return self._report()

    # ---------- 轮次 ----------
    def _record_round(self, status: Status, bar: dict, finished: bool = True) -> None:
        equity = self.wallet.equity(self.position.unrealized(bar["c"]))
        end_multiple = equity / self.cfg.challenge.initial_balance
        self.rounds.append({
            "round": self._round_no + 1,
            "status": status.value if finished else "end_of_data",
            "initial": self.cfg.challenge.initial_balance,
            "final": round(equity, 4),
            "multiple": round(end_multiple, 4),
            "peak": round(self.challenge.peak_equity, 4),
            "result": self.challenge.result,
            "ts": bar["ts"],
        })

    def _reset_round(self, bar: dict) -> None:
        self._round_no += 1
        initial = self.cfg.challenge.initial_balance
        self.strategy.on_open()
        self.position = Position()
        self.wallet = Wallet.new(initial)
        self.challenge.start_round(initial)
        self.challenge.start_ts = bar["ts"] / 1000.0

    # ---------- 内部撮合（与纸盘同一套口径） ----------
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

    # ---------- 报告 ----------
    def _report(self) -> dict:
        completed = [r for r in self.rounds if r["status"] != "end_of_data"]
        guards = [r for r in completed if r["status"] == Status.GUARD.value]
        pos_rounds = [r for r in completed if r["multiple"] > 1.0]
        neg_rounds = [r for r in completed if r["multiple"] <= 1.0]
        mults = [r["multiple"] for r in completed]
        pnls = [t["pnl"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        liq_count = sum(1 for t in self.trades if t["reason"] == "liquidation")
        max_dd = max((s["drawdown_pct"] for s in self.equity_curve), default=0.0)
        return {
            "rounds_total": len(self.rounds),
            "rounds_completed": len(completed),
            "rounds_positive": len(pos_rounds),
            "rounds_negative": len(neg_rounds),
            "round_positive_rate": round(100.0 * len(pos_rounds) / len(completed), 1)
            if completed else 0.0,
            "avg_end_multiple": round(sum(mults) / len(mults), 3) if mults else 0.0,
            "best_round_multiple": round(max(mults), 3) if mults else 0.0,
            "last_round_status": self.rounds[-1]["status"] if self.rounds else "-",
            "initial_balance": self.cfg.challenge.initial_balance,
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(100.0 * len(wins) / len(pnls), 1) if pnls else 0.0,
            "total_fees": round(self.wallet.fees_paid, 4),
            "total_funding": round(self.wallet.funding_paid, 4),
            "liquidation_count": liq_count,
            "max_equity_seen": round(self._max_equity_seen, 4),
            "max_drawdown_pct": round(max_dd, 2),
            "bars": len(self.bars),
            "elapsed_hours": round((self.bars[-1]["ts"] - self.bars[0]["ts"]) / 3.6e6, 2),
        }


class _BarsCtx:
    """最小化策略上下文（回测内联，避免依赖 engine.Ctx）。"""

    def __init__(self, history, position, price):
        self.history = history
        self.position = position
        self.price = price
