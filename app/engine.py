"""主循环：paper/live 模式，进程内自动连续轮次。

- 无胜利点：不设达标结算，动态出局线触发即结束本轮，随后自动开新一轮
  （纸盘重置初始资金；实盘从交易所当前真实余额起算，自动复合）
- bar 驱动策略 -> TP/SL -> 信号开平仓 -> 资金费结算 -> 强平保护 -> 出局线检查
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from . import okx_math
from .broker import LiveBroker, PaperBroker
from .challenge import Challenge, ChallengeConfig, Status
from .config import Config
from .okx_feed import OkxFeed
from .store import Store
from .strategy import Strategy, create_strategy
from .wallet import Wallet


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
            self.wallet = Wallet.new(0.0)  # 每轮 _start_round 重建
            self.broker = PaperBroker(self.feed, cfg.exchange, cfg.risk, self.wallet)

        self.strategy: Strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
        self.challenge = Challenge(ChallengeConfig(
            initial_balance=0.0,  # 每轮由 _start_round 覆盖
            base_drawdown_pct=cfg.challenge.base_drawdown_pct,
            tight_drawdown_pct=cfg.challenge.tight_drawdown_pct,
            tight_start_multiple=cfg.challenge.tight_start_multiple,
            duration_hours=cfg.challenge.duration_hours,
            timeframe=cfg.challenge.timeframe,
        ))
        self.run_id: Optional[int] = None
        self.round_no: int = 0

        self._bars: list[dict] = []
        self._history_limit = 500
        self._warmed = False
        self._next_funding_ts: Optional[float] = None
        self._last_sample_ts = 0.0
        self._last_refresh_ts = 0.0
        self._notify = asyncio.Event()
        self._latest_snapshot: dict = {}
        self._stop_requested = False
        self.last_mark = 0.0
        self._persisted_trades = 0  # 已入库的成交数（本进程内增量落库）

    # ---------- 看板推送 ----------
    def latest_snapshot(self) -> dict:
        return self._latest_snapshot

    async def wait_snapshot(self) -> None:
        await self._notify.wait()
        self._notify.clear()

    async def request_stop(self) -> None:
        """紧急停止：结算当前轮并退出进程。"""
        logger.warning("收到停止指令")
        self.challenge.status = Status.STOPPED
        self.challenge.result = "手动停止"
        self._stop_requested = True

    # ---------- 轮次管理 ----------
    def _round_initial(self) -> float:
        if self.mode == "live":
            return self.broker.equity_usdt  # 实盘：当前真实权益
        return self.cfg.challenge.initial_balance  # 纸盘：配置初始资金

    async def _start_round(self, initial: float) -> None:
        if initial <= 0:
            raise RuntimeError(f"本轮初始资金无效: {initial}")
        self.round_no += 1
        if self.mode == "paper":
            self.wallet = Wallet.new(initial)
            self.broker.wallet = self.wallet
        self.challenge.start_round(initial)
        self._persisted_trades = len(self.broker.trades)  # 新周期只统计新成交
        self.run_id = await self.store.start_run(
            self.mode, self.strategy.name, initial, self.cfg.model_dump(exclude={"secrets"}),
        )
        if not self._warmed:
            warmup = await self.feed.fetch_ohlcv_history(self.cfg.challenge.timeframe, limit=200)
            self._bars = warmup
            self._warmed = True
            self._next_funding_ts = okx_math.next_funding_time(
                datetime.now(timezone.utc)).timestamp()
        logger.info(f"第 {self.round_no} 轮开始 [{self.mode}] 初始 {initial:.4f}U "
                    f"出局线 {self.challenge.guard_level():.4f}U")
        await self._post_state(self.feed.price or self.last_mark or initial, force=True)

    async def _flush_trades(self) -> None:
        """把 broker 里新增的成交增量写入数据库（面板"最近成交"的数据源）。"""
        try:
            while len(self.broker.trades) > self._persisted_trades:
                await self.store.add_trade(self.run_id, self.broker.trades[self._persisted_trades])
                self._persisted_trades += 1
        except Exception as e:
            logger.warning(f"成交入库失败: {e}")

    async def _end_round(self, status: Status) -> None:
        """本轮结算（平仓 + 记录），随后自动开新一轮；手动停止则结束。"""
        if status == Status.STOPPED:
            if self.broker.position.is_open:
                try:
                    await self.broker.close_position(reason="manual_stop")
                except Exception as e:
                    logger.warning(f"停止平仓失败: {e}")
            await self._flush_trades()
            mark = self.feed.price or self.last_mark
            equity = self.broker.equity(mark)
            await self.store.finish_run(self.run_id, status.value,
                                        self.challenge.result or "手动停止",
                                        self.challenge.peak_equity, equity)
            logger.info(f"第 {self.round_no} 轮结束 [stopped] 权益 {equity:.4f}")
            return
        # 平仓（动态线触发或超时）
        if self.broker.position.is_open:
            try:
                await self.broker.close_position(reason="round_end")
            except Exception as e:
                logger.warning(f"轮次平仓失败: {e}")
        await self._flush_trades()
        self.strategy.on_open()
        mark = self.feed.price or self.last_mark
        equity = self.broker.equity(mark)
        await self.store.finish_run(self.run_id, status.value,
                                    self.challenge.result or "轮次结束",
                                    self.challenge.peak_equity, equity)
        logger.info(f"第 {self.round_no} 轮结束 [{status.value}] {self.challenge.result} "
                    f"权益 {equity:.4f}")
        # 自动开新一轮
        if self.mode == "live":
            try:
                await self.broker.refresh_position()
            except Exception as e:
                logger.warning(f"刷新余额失败，用本地权益: {e}")
        await self._start_round(self._round_initial())

    # ---------- 主流程 ----------
    async def run(self) -> None:
        await self.store.init()
        await self.feed.load_spec_and_fees()
        if self.mode == "live":
            await self.broker.refresh_position()
        else:
            cfg_initial = self.cfg.challenge.initial_balance
            if cfg_initial <= 0:
                raise SystemExit("纸盘模式 [challenge].initial_balance 必须 > 0（实盘才支持 auto=0）")
        await self._start_round(self._round_initial())

        self.feed.subscribe("bar", self._on_bar)
        await self.feed.start()
        try:
            while not self._stop_requested:
                mark = self.feed.price or self.last_mark
                if mark > 0:
                    self.last_mark = mark
                    await self._on_tick(mark)
                await asyncio.sleep(1.0)
        finally:
            await self.feed.close()
        # 手动停止收尾：无论当前轮状态都平仓结算
        if self._stop_requested and self.run_id:
            if self.challenge.status == Status.RUNNING:
                self.challenge.status = Status.STOPPED
                self.challenge.result = "手动停止"
            await self._end_round(self.challenge.status)

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
        if self.challenge.status != Status.RUNNING:
            return
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

        await self._flush_trades()
        if self.challenge.status != Status.RUNNING:
            return
        await self._post_state(bar["c"])

    async def _try_open(self, side: str, reason: str) -> None:
        if self.broker.position.is_open or self.challenge.status != Status.RUNNING:
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

    # ---------- 状态与轮次结算 ----------
    async def _post_state(self, mark: float, force: bool = False) -> None:
        if self.challenge.status != Status.RUNNING:
            return
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
        self._latest_snapshot = self._snapshot(mark, equity)
        self._notify.set()
        if status != Status.RUNNING:
            await self._end_round(status)

    def _snapshot(self, mark: float, equity: float) -> dict:
        snap = self.broker.snapshot(mark)
        prog = self.challenge.progress(equity)
        prog["round"] = self.round_no
        snap["challenge"] = prog
        snap["funding_rate"] = self.feed.funding_rate
        snap["bid"] = self.feed.bid
        snap["ask"] = self.feed.ask
        snap["ts"] = time.time()
        snap["running"] = not self._stop_requested
        snap["feed_mode"] = "rest" if self.feed._rest_mode else "ws"
        if self.wallet:
            snap["wallet"] = self.broker.wallet_view()
        return snap
