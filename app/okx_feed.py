"""OKX 行情与接口封装（ccxt.pro）。

- spec：instruments + position-tiers 启动时从 OKX 拉取，不硬编码
- 实际费率：fetch_trading_fee（/api/v5/account/trade-fee），失败回退配置默认
- 行情：watch_trades / watch_order_book / watch_ohlcv / watch_funding_rate（自动重连）
- 代理：开发机 aiohttp_proxy；VPS 直连
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import ccxt.pro as ccxtpro
from loguru import logger

from .config import ExchangeConfig, Secrets

Handler = Callable[[dict], Awaitable[None]]


@dataclass
class InstrumentSpec:
    inst_id: str = "ETH-USDT-SWAP"
    ct_val: float = 0.1    # 面值 ETH/张
    min_sz: float = 0.01   # 最小下单（张）
    lot_sz: float = 0.01   # 下单步进（张）
    tick_sz: float = 0.01  # 价格最小变动
    max_lever: float = 100  # 第 1 档最大杠杆
    mmr: float = 0.004     # 第 1 档维持保证金率
    imr: float = 0.01      # 第 1 档初始保证金率
    contracts_mult: int = 1


class OkxFeed:
    def __init__(self, cfg: ExchangeConfig, secrets: Secrets, timeframe: str = "1m"):
        self.cfg = cfg
        self.timeframe = timeframe
        params: dict[str, Any] = {"enableRateLimit": True, "timeout": 30000}
        if cfg.proxy:
            params["aiohttp_proxy"] = cfg.proxy
        if secrets.api_key:
            params["apiKey"] = secrets.api_key
            params["secret"] = secrets.api_secret
            params["password"] = secrets.passphrase
        self.exchange = ccxtpro.okx(params)
        # 只加载 swap 市场；关闭 currencies 预拉（base load_markets 会调私有 /asset/currencies，本项目用不到）
        self.exchange.has["fetchCurrencies"] = False
        self.exchange.options.setdefault("fetchMarkets", {})["types"] = ["swap"]
        self.spec = InstrumentSpec()
        self.taker_fee = cfg.taker_fee
        self.maker_fee = cfg.maker_fee

        # 最新行情状态
        self.price: float = 0.0
        self.bid: float = 0.0
        self.ask: float = 0.0
        self.order_book: dict | None = None
        self.funding_rate: float = 0.0
        self.funding_time: float = 0.0
        self.candles: list[list] = []      # 1m 窗口 [ts,o,h,l,c,v]
        self.last_closed_ts: float = 0.0

        self._handlers: dict[str, list[Handler]] = {
            "trade": [], "bar": [], "funding": [], "book": [],
        }
        self._tasks: list[asyncio.Task] = []
        self._rest_mode = False  # 当前是否运行在 REST 轮询模式
        self._last_ws_ts = 0.0   # 最近一次 WS 收到数据的时间
        self.feed_mode = cfg.feed or "auto"

    # ---------- 订阅 ----------
    def subscribe(self, channel: str, fn: Handler) -> None:
        self._handlers[channel].append(fn)

    async def _emit(self, channel: str, payload: dict) -> None:
        for fn in self._handlers[channel]:
            try:
                await fn(payload)
            except Exception as e:  # 订阅方异常不影响行情循环
                logger.warning(f"handler {channel} 异常: {e}")

    # ---------- 规格与费率（启动时调用） ----------
    async def load_spec_and_fees(self) -> None:
        await self.exchange.load_markets()
        market = self.exchange.market(self.cfg.symbol)
        inst_id = market["id"]
        try:
            res = await self.exchange.publicGetPublicInstruments(
                {"instType": "SWAP", "instId": inst_id}
            )
            d = res["data"][0]
            self.spec = InstrumentSpec(
                inst_id=inst_id,
                ct_val=float(d["ctVal"]),
                min_sz=float(d["minSz"]),
                lot_sz=float(d["lotSz"]),
                tick_sz=float(d["tickSz"]),
            )
            logger.info(f"合约规格 {inst_id}: 面值 {self.spec.ct_val} ETH/张, "
                        f"min {self.spec.min_sz} 张, lot {self.spec.lot_sz}, tick {self.spec.tick_sz}")
        except Exception as e:
            logger.warning(f"拉取 instruments 失败，用默认规格: {e}")

        try:
            family = inst_id.rsplit("-SWAP", 1)[0]
            res = await self.exchange.publicGetPublicPositionTiers(
                {"instType": "SWAP", "instFamily": family, "tdMode": "isolated"}
            )
            t1 = res["data"][0]
            self.spec.max_lever = float(t1["maxLever"])
            self.spec.mmr = float(t1["mmr"])
            self.spec.imr = float(t1["imr"])
            logger.info(f"isolated 第 1 档: 最大杠杆 {self.spec.max_lever}x, "
                        f"IMR {self.spec.imr}, MMR {self.spec.mmr}")
        except Exception as e:
            logger.warning(f"拉取 position-tiers 失败，用默认档位: {e}")

        if self.cfg.fetch_fees_from_okx and self.exchange.apiKey:
            try:
                fee = await self.exchange.fetch_trading_fee(self.cfg.symbol)
                if fee.get("taker") and fee.get("maker"):
                    self.taker_fee = float(fee["taker"])
                    self.maker_fee = float(fee["maker"])
                    logger.info(f"实际费率: taker {self.taker_fee} maker {self.maker_fee}")
            except Exception as e:
                logger.warning(f"拉取实际费率失败，用默认 {self.taker_fee}/{self.maker_fee}: {e}")

    # ---------- 行情任务 ----------
    async def start(self) -> None:
        if self.feed_mode == "rest":
            await self._switch_to_rest()
            return
        # ws 或 auto：先起 WS 任务
        self._tasks += [
            asyncio.create_task(self._loop_trades()),
            asyncio.create_task(self._loop_order_book()),
            asyncio.create_task(self._loop_ohlcv()),
            asyncio.create_task(self._loop_funding()),
        ]
        if self.feed_mode == "auto":
            # 看门狗：8 秒内无行情（如本地代理不支持 WS）则降级 REST 轮询
            self._tasks.append(asyncio.create_task(self._watchdog()))

    async def _watchdog(self) -> None:
        await asyncio.sleep(8)
        if time.time() - self._last_ws_ts > 8 and not self._rest_mode:
            logger.warning("WS 行情 8 秒无数据，自动降级为 REST 轮询（开发机走代理常见）")
            await self._switch_to_rest()

    async def _switch_to_rest(self) -> None:
        if self._rest_mode:
            return
        self._rest_mode = True
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        self._tasks += [
            asyncio.create_task(self._poll_price()),
            asyncio.create_task(self._poll_ohlcv()),
            asyncio.create_task(self._poll_funding()),
        ]

    def _bar_duration_ms(self) -> int:
        """周期时长(ms)：'1m'->60000, '5m'->300000 ... 未知周期兜底 1m。"""
        tf = (self.timeframe or "1m").lower()
        unit = tf[-1]
        try:
            n = int(tf[:-1])
        except ValueError:
            return 60_000
        mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "s": 1000}.get(unit, 60_000)
        return n * mult

    async def _poll_price(self) -> None:
        while True:
            try:
                t = await self.exchange.fetch_ticker(self.cfg.symbol)
                self.price = float(t["last"] or 0)
                self.bid = float(t.get("bid") or 0)
                self.ask = float(t.get("ask") or 0)
                await self._emit("book", {"bid": self.bid, "ask": self.ask})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"REST ticker 拉取失败: {e}")
            await asyncio.sleep(2)

    async def _poll_ohlcv(self) -> None:
        bar_ms = self._bar_duration_ms()
        while True:
            try:
                candles = await self.exchange.fetch_ohlcv(self.cfg.symbol, self.timeframe, limit=10)
                self.candles = candles
                now_ms = int(time.time() * 1000)
                for c in candles:
                    ts = float(c[0])
                    if ts > self.last_closed_ts and ts <= now_ms - bar_ms:
                        self.last_closed_ts = ts
                        bar = {"ts": ts, "o": float(c[1]), "h": float(c[2]),
                               "l": float(c[3]), "c": float(c[4]), "v": float(c[5])}
                        self.price = bar["c"]
                        await self._emit("bar", bar)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"REST ohlcv 拉取失败: {e}")
            await asyncio.sleep(30)

    async def _poll_funding(self) -> None:
        while True:
            try:
                fr = await self.exchange.fetch_funding_rate(self.cfg.symbol)
                rate = fr.get("fundingRate")
                if rate is not None:
                    self.funding_rate = float(rate)
                    self.funding_time = float(fr.get("fundingTimestamp") or 0)
                    await self._emit("funding", {"rate": self.funding_rate, "ts": self.funding_time})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"REST funding 拉取失败: {e}")
            await asyncio.sleep(300)

    async def ensure_order_book(self) -> None:
        """下单前保证有最新盘口（REST 模式下按需拉取；WS 模式下已持续更新）。"""
        if self.order_book is None:
            try:
                ob = await self.exchange.fetch_order_book(self.cfg.symbol, 20)
                self.order_book = ob
                if ob["bids"]:
                    self.bid = float(ob["bids"][0][0])
                if ob["asks"]:
                    self.ask = float(ob["asks"][0][0])
            except Exception as e:
                logger.warning(f"盘口拉取失败，用最新价撮合: {e}")

    async def _loop_trades(self) -> None:
        while True:
            try:
                trades = await self.exchange.watch_trades(self.cfg.symbol)
                self._last_ws_ts = time.time()
                for t in trades:
                    self.price = float(t["price"])
                    await self._emit("trade", t)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"trades 流断开: {e}")
                await asyncio.sleep(2)

    async def _loop_order_book(self) -> None:
        while True:
            try:
                ob = await self.exchange.watch_order_book(self.cfg.symbol, 20)
                self._last_ws_ts = time.time()
                self.order_book = ob
                if ob["bids"]:
                    self.bid = float(ob["bids"][0][0])
                if ob["asks"]:
                    self.ask = float(ob["asks"][0][0])
                await self._emit("book", {"bid": self.bid, "ask": self.ask})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"orderbook 流断开: {e}")
                await asyncio.sleep(2)

    async def _loop_ohlcv(self) -> None:
        bar_ms = self._bar_duration_ms()
        while True:
            try:
                candles = await self.exchange.watch_ohlcv(self.cfg.symbol, self.timeframe or "1m")
                self._last_ws_ts = time.time()
                self.candles = candles
                # WS 推送是增量数据，不能假设最后一根"进行中"；
                # 统一按时间判断是否已收盘（ts <= 当前时间 - 周期），避免把刚收盘的 K 线漏掉
                now_ms = int(time.time() * 1000)
                for c in candles:
                    ts = float(c[0])
                    if ts > self.last_closed_ts and ts <= now_ms - bar_ms:
                        self.last_closed_ts = ts
                        bar = {"ts": ts, "o": float(c[1]), "h": float(c[2]),
                               "l": float(c[3]), "c": float(c[4]), "v": float(c[5])}
                        self.price = bar["c"]
                        logger.info(f"新 K 线 {self.timeframe} 收盘 {bar['c']:.2f} @ {ts}")
                        await self._emit("bar", bar)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"ohlcv 流断开: {e}")
                await asyncio.sleep(2)

    async def _loop_funding(self) -> None:
        while True:
            try:
                fr = await self.exchange.watch_funding_rate(self.cfg.symbol)
                self._last_ws_ts = time.time()
                rate = fr.get("fundingRate")
                if rate is not None:
                    self.funding_rate = float(rate)
                    self.funding_time = float(fr.get("fundingTimestamp") or fr.get("timestamp") or 0)
                    logger.info(f"资金费率更新: {self.funding_rate}")
                    await self._emit("funding", {"rate": self.funding_rate, "ts": self.funding_time})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"funding 流断开: {e}")
                await asyncio.sleep(2)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        try:
            await self.exchange.close()
        except Exception:
            pass

    # ---------- 历史数据（预热与回测） ----------
    async def fetch_ohlcv_history(self, timeframe: str, limit: int = 300) -> list[dict]:
        """拉最近 N 根 K 线做指标预热。"""
        raw = await self.exchange.fetch_ohlcv(self.cfg.symbol, timeframe, limit=min(limit, 300))
        bars = [{"ts": float(c[0]), "o": float(c[1]), "h": float(c[2]),
                 "l": float(c[3]), "c": float(c[4]), "v": float(c[5])} for c in raw]
        if bars:
            self.last_closed_ts = bars[-1]["ts"]
            self.price = bars[-1]["c"]
        return bars

    async def fetch_funding_rate_history(self, since_ms: int, until_ms: int) -> list[dict]:
        """拉历史资金费率（OKX 每 8 小时一条，limit 100/次）。"""
        out: list[dict] = []
        cursor = since_ms
        while cursor < until_ms:
            batch = await self.exchange.fetch_funding_rate_history(
                self.cfg.symbol, since=cursor, limit=100
            )
            if not batch:
                break
            for r in batch:
                out.append({
                    "ts": float(r["timestamp"]),
                    "rate": float(r["fundingRate"]),
                })
            cursor = float(batch[-1]["timestamp"]) + 1
            if len(batch) < 100:
                break
            await asyncio.sleep(0.1)
        return out
