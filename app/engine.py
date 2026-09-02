"""主循环：paper/live 模式。

bar 驱动策略 -> TP/SL -> 信号开平仓 -> 资金费结算 -> 强平保护 -> 挑战检查 -> 状态广播。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from . import okx_math
from .broker import LiveBroker, PaperBroker
from .challenge import Challenge, ChallengeConfig, Status
from .config import Config
from .okx_feed import OkxFeed
from .store import Store
from .strategy import Strategy, TrendEma


@dataclass
class Ctx:
    """策略上下文：历史 bar / 当前持仓 / 最新价。"""
    history: list
    position: object
    price: float
    equity: float = 0.0


class Engine:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.feed = OkxFeed(cfg.exchange, cfg.secrets, timeframe=cfg.challenge.timeframe)
        self.mode = cfg.exchange.mode
        if self.mode == "live":
            self.broker = LiveBroker(self.feed, cfg.exchange, cfg.risk)
            self.wallet = None
        else:
            from .wallet import Wallet
            self.wallet = Wallet.new(cfg.challenge.initial_balance)
            self.broker = PaperBroker(self.feed, cfg.exchange, cfg.risk, self.wallet)

        self.strategy: Strategy = TrendEma(cfg.strategy.params)
        self.challenge = Challenge(ChallengeConfig(
            initial_balance=cfg.challenge.initial_balance,
            target_multiple=cfg.challenge.target_multiple,
            max_drawdown_pct=cfg.challenge.max_drawdown_pct,
            duration_hours=cfg.challenge.duration_hours,
            timeframe=cfg.challenge.timeframe,
        ))
        self.run_id: Optional[int] = None

        self._bars: list[dict] = []
        self._history_limit = 500
        self._next_funding_ts: Optional[float] = None
        self._last_sample_ts = 0.0
        self._last_refresh_ts = 0.0
        self._notify = asyncio.Event()
        self._latest_snapshot: dict = {}
        self._stop_requested = False
        self._finished = False
        self.last_mark = 0.0

    # ---------- 看板推送 ----------
    def latest_snapshot(self) -> dict:
        return self._latest_snapshot

    async def wait_snapshot(self) -> None:
        await self._notify.wait()
        self._notify.clear()

    async def request_stop(self) -> None:
        """紧急停止：平仓并结算挑战。"""
        logger.warning("收到停止指令")
        self.challenge.status = Status.STOPPED
        self.challenge.result = "手动停止"
        self._stop_requested = True

    # ---------- 主流程 ----------
    async def run(self) -> None:
        await self.store.init()
        await self.feed.load_spec_and_fees()
        self.run_id = await self.store.start_run(
            self.mode, self.strategy.name, self.cfg.challenge.initial_balance,
            self.cfg.model_dump(exclude={"secrets"}),
        )
        warmup = await self.feed.fetch_ohlcv_history(self.cfg.challenge.timeframe, limit=200)
        self._bars = warmup
        self._next_funding_ts = okx_math.next_funding_time(datetime.now(timezone.utc)).timestamp()

        self.feed.subscribe("bar", self._on_bar)
        await self.feed.start()

        logger.info(
            f"挑战开始 [{self.mode}] 初始 {self.cfg.challenge.initial_balance}U "
            f"目标 {self.cfg.challenge.target_multiple} 倍 -> {self.challenge.cfg.target_equity}U "
            f"回撤出局 {self.cfg.challenge.max_drawdown_pct}%"
        )
        try:
            while not self._stop_requested and self.challenge.status == Status.RUNNING:
                mark = self.feed.price or self.last_mark
                if mark > 0:
                    self.last_mark = mark
                    await self._on_tick(mark)
                await asyncio.sleep(1.0)
        finally:
            await self.feed.close()
        if not self._finished:
            await self._finish()

    async def _on_tick(self, mark: float) -> None:
        if isinstance(self.broker, PaperBroker):
            await self.broker.check_liquidation(mark)
        elif time.time() - self._last_refresh_ts > 10:
            try:
                await self.broker.refresh_position()
                self._last_refresh_ts = time.time()
            except Exception as e:
                logger.warning(f"刷新持仓失败: {e}")
        await self._maybe_settle_funding()
        now = time.time()
        if now - self._last_sample_ts >= 2.0:
            self._last_sample_ts = now
            await self._post_state(mark)

    async def _maybe_settle_funding(self) -> None:
        """纸盘：资金费在 UTC 00:00/08:00/16:00 结算（按最新资金费率）。实盘由交易所自动处理。"""
        if isinstance(self.broker, LiveBroker) or self._next_funding_ts is None:
            return
        now = time.time()
        if now >= self._next_funding_ts:
            pos = self.broker.position
            if pos.is_open:
                rate = self.feed.funding_rate
                fee = okx_math.funding_fee(pos.size_eth, self.feed.price or pos.entry, rate)
                self.broker.wallet.pay_funding(fee)
                logger.info(f"资金费结算: {fee:+.6f} USDT (rate={rate})")
            self._next_funding_ts = okx_math.next_funding_time(datetime.now(timezone.utc)).timestamp()

    # ---------- bar 处理 ----------
    async def _on_bar(self, bar: dict) -> None:
        self._bars.append(bar)
        if len(self._bars) > self._history_limit:
            self._bars = self._bars[-self._history_limit:]

        pos = self.broker.position
        if pos.is_open:
            hit = self.strategy.check_tp_sl(bar, pos.side)
            if hit:
                await self.broker.close_position(reason=hit[1], fill_px=hit[0])
                self.strategy.on_open()

        sig = self.strategy.on_bar(bar, self._ctx())
        if sig.action in ("open_long", "open_short"):
            await self._try_open("long" if sig.action == "open_long" else "short", sig.reason)
        elif sig.action == "close":
            await self.broker.close_position(reason=sig.reason)
            self.strategy.on_open()

        if self.challenge.status != Status.RUNNING:
            return
        await self._post_state(bar["c"])

    async def _try_open(self, side: str, reason: str) -> None:
        if self.broker.position.is_open:
            return
        mark = self.feed.price or self.last_mark
        if mark <= 0:
            return
        contracts, size_eth = self.broker.compute_size(mark)
        if contracts <= 0:
            return
        await self.broker.open_position(side, size_eth)
        logger.info(f"信号[{reason}] 开仓 {side} {size_eth:.4f} ETH @ {mark:.2f}")

    def _ctx(self) -> Ctx:
        return Ctx(
            history=self._bars,
            position=self.broker.position,
            price=self.feed.price or self.last_mark,
        )

    # ---------- 状态与结算 ----------
    async def _post_state(self, mark: float) -> None:
        equity = self.broker.equity(mark)
        status = self.challenge.update(equity)
        drawdown = self.challenge.drawdown_pct(equity)
        sample = {
            "ts": time.time(), "equity": round(equity, 4),
            "balance": round(self.broker.wallet.balance, 4) if self.wallet else round(equity, 4),
            "margin": round(self.broker.position.margin, 4),
            "unrealized": round(self.broker.position.unrealized(mark), 4),
            "drawdown_pct": round(drawdown, 2),
            "challenge_status": status.value,
        }
        await self.store.add_equity(self.run_id, sample)
        self._latest_snapshot = self._snapshot(mark, equity, sample)
        self._notify.set()
        if status != Status.RUNNING:
            await self._finish()

    def _snapshot(self, mark: float, equity: float, sample: dict) -> dict:
        snap = self.broker.snapshot(mark)
        snap["challenge"] = self.challenge.progress(equity)
        snap["funding_rate"] = self.feed.funding_rate
        snap["bid"] = self.feed.bid
        snap["ask"] = self.feed.ask
        snap["ts"] = time.time()
        snap["running"] = self.challenge.status == Status.RUNNING
        if self.wallet:
            snap["wallet"] = self.broker.wallet_view()
        return snap

    async def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self.broker.position.is_open:
            try:
                await self.broker.close_position(reason="challenge_end")
            except Exception as e:
                logger.warning(f"结算平仓失败: {e}")
        mark = self.feed.price or self.last_mark
        equity = self.broker.equity(mark)
        status = self.challenge.status
        await self.store.finish_run(
            self.run_id, status.value, self.challenge.result or "手动停止",
            self.challenge.peak_equity, equity,
        )
        logger.info(f"挑战结束 [{status.value}] {self.challenge.result} 最终权益 {equity:.4f}")
        self._latest_snapshot = self._snapshot(mark, equity, {})
        self._notify.set()
