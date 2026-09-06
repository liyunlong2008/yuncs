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
from .broker import (
    Position,
    add_position_math,
    close_position_math,
    fraction_eth_for_spec,
    open_position_math,
)
from .challenge import Challenge, ChallengeConfig, Status
from .config import Config
from .okx_feed import InstrumentSpec
from .strategy import Signal, create_strategy
from .wallet import Wallet


class Backtest:
    def __init__(self, cfg: Config, bars: list[dict], funding: list[dict],
                 spec: InstrumentSpec | None = None, compounding: bool = False):
        self.cfg = cfg
        self.bars = bars
        self.funding = funding
        self.spec = spec or InstrumentSpec()  # 默认值即 OKX ETH-USDT-SWAP 当前规格
        # compounding=True = 实盘语义：周期结束不重置钱包，下一周期从当前余额起算（连续复利）
        self.compounding = compounding
        # margin_frac>0 = 保证金随权益缩放（实盘避免跌破固定保证金停摆；0=固定 margin_per_trade）
        self.margin_frac = float(getattr(cfg.risk, "margin_frac", 0.0) or 0.0)
        self._initial0 = cfg.challenge.initial_balance
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
        self._plan_eth = 0.0  # 分批计划全仓目标（首笔开仓时锁定）

    def run(self) -> dict:
        if not self.bars:
            return {"error": "无K线数据"}
        self.challenge.start_ts = self.bars[0]["ts"] / 1000.0
        self._next_funding_ts = self._first_funding_after(self.bars[0]["ts"])

        pending: Optional[Signal] = None
        for i, bar in enumerate(self.bars):
            ts = bar["ts"]
            # 1) 上一根信号的执行（本根开盘成交；close 为策略主动平仓信号）
            if pending is not None:
                if pending.action == "close" and self.position.is_open:
                    side = "sell" if self.position.side == "long" else "buy"
                    px = fills.candle_fill_price(bar, side, self.cfg.risk.slippage_bps)
                    self._close(fill_px=px, reason=pending.reason or "策略平仓")
                else:
                    self._execute_signal(pending, bar)
                pending = None
            # 2) 止盈/止损/部分止盈（本根高低点；按策略返回的 frac 平仓）
            if self.position.is_open:
                hit = self.strategy.evaluate_exits(bar, self.position.side)
                if hit:
                    self._close(fill_px=hit[0], reason=hit[1], frac=hit[2])
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
            elif sig.action in ("add_long", "add_short") and self.position.is_open and \
                    self.position.side == ("long" if sig.action == "add_long" else "short"):
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
        r_initial = self.challenge.initial_balance or self._initial0
        end_multiple = equity / r_initial if r_initial > 0 else 0.0
        self.rounds.append({
            "round": self._round_no + 1,
            "status": status.value if finished else "end_of_data",
            "initial": r_initial,
            "final": round(equity, 4),
            "multiple": round(end_multiple, 4),
            "peak": round(self.challenge.peak_equity, 4),
            "result": self.challenge.result,
            "ts": bar["ts"],
        })

    def _reset_round(self, bar: dict) -> None:
        self._round_no += 1
        self.strategy.on_open()
        self.position = Position()
        self._plan_eth = 0.0
        if self.compounding:
            # 实盘语义：不重置钱包，下一周期从当前余额起算（连续复利）
            initial = self.wallet.equity(0.0)
            if initial <= 0:
                initial = self._initial0
        else:
            initial = self._initial0
            self.wallet = Wallet.new(initial)
        self.challenge.start_round(initial)
        self.challenge.start_ts = bar["ts"] / 1000.0

    # ---------- 内部撮合（与纸盘同一套记账核心，见 broker.py） ----------
    def _execute_signal(self, sig: Signal, bar: dict) -> None:
        if sig.action in ("open_long", "open_short"):
            self._open_leg("long" if sig.action == "open_long" else "short", sig, bar)
        elif sig.action in ("add_long", "add_short"):
            self._add_signal("long" if sig.action == "add_long" else "short", sig, bar)

    def _open_leg(self, side: str, sig: Signal, bar: dict) -> None:
        if self.position.is_open:
            return
        # 保证金预算：缩放模式按当前可用余额比例；否则固定 margin_per_trade（受余额限制）
        if self.margin_frac > 0:
            margin_budget = self.wallet.balance * self.margin_frac
        else:
            margin_budget = min(self.cfg.risk.margin_per_trade, self.wallet.balance)
        if margin_budget <= 0:
            return
        notional_cap = min(margin_budget * self.cfg.risk.leverage, self.cfg.risk.max_notional)
        contracts, size_eth = self._size(bar["o"], notional_cap)
        if contracts <= 0:
            return
        # 首笔按计划切分批比例（136：第一层 10%）；旧策略 frac=1.0 即全额开仓
        plan = size_eth
        if sig.frac < 1.0:
            size_eth = fraction_eth_for_spec(self.spec, plan, sig.frac)
            if size_eth <= 0:
                return
        fill = fills.candle_fill_price(bar, "buy" if side == "long" else "sell",
                                       self.cfg.risk.slippage_bps)
        fee_rate = self.cfg.exchange.taker_fee
        pos = open_position_math(self.wallet, side, size_eth, fill, fee_rate,
                                 self.cfg.risk.leverage, self.spec.mmr,
                                 now_ts=bar["ts"] / 1000.0)
        if pos is None:
            return
        self.position = pos
        self._plan_eth = plan  # 分批计划：首笔按当时预算锁定的全仓目标

    def _add_signal(self, side: str, sig: Signal, bar: dict) -> None:
        """同向补批：只允许与持仓同向，尺寸按锁定计划切。"""
        if not self.position.is_open or self.position.side != side or self._plan_eth <= 0:
            return
        size_eth = fraction_eth_for_spec(self.spec, self._plan_eth, sig.frac)
        if size_eth <= 0:
            return
        fill = fills.candle_fill_price(bar, "buy" if side == "long" else "sell",
                                       self.cfg.risk.slippage_bps)
        self._add_leg(size_eth, fill)

    def _add_leg(self, size_eth: float, fill: float) -> None:
        """同向补批（回测与纸盘同一套累加记账）。"""
        if not self.position.is_open or size_eth <= 0:
            return
        fee_rate = self.cfg.exchange.taker_fee
        merged = add_position_math(self.wallet, self.position, size_eth, fill,
                                   fee_rate, self.cfg.risk.leverage, self.spec.mmr)
        if merged is None:
            return
        self.position = merged

    def _close(self, fill_px: float, reason: str, frac: float = 1.0) -> None:
        pos = self.position
        if not pos.is_open:
            return
        rec, rest = close_position_math(self.wallet, pos, fill_px,
                                        self.cfg.exchange.taker_fee, self.spec.mmr,
                                        frac=frac, reason=reason)
        if rec:
            self.trades.append(rec)
        self.position = rest
        if not rest.is_open:
            self._plan_eth = 0.0
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

    def _size(self, price: float, notional: float | None = None) -> tuple[float, float]:
        if notional is None:
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
        # 期末权益与复合倍数（实盘连续语义的核心指标）
        end_equity = self.wallet.equity(self.position.unrealized(self.bars[-1]["c"]))
        compounded = end_equity / self._initial0 if self._initial0 > 0 else 0.0
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
            "initial_balance": self._initial0,
            "end_equity": round(end_equity, 4),
            "compounded_multiple": round(compounded, 3),
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
