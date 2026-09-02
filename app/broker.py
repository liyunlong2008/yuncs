"""持仓与下单：paper=本地撮合（按 OKX 计算方法），live=真实下单（以交易所数据为准）。

统一对外暴露 USDT 口径字段：可买 / 成本 / 预估成本价。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from . import fills, okx_math
from .config import ExchangeConfig, RiskConfig
from .okx_feed import OkxFeed
from .wallet import Wallet


@dataclass
class Position:
    side: str = ""            # long / short / ""
    size_eth: float = 0.0
    entry: float = 0.0
    margin: float = 0.0       # 占用保证金 USDT
    liq_px: float = 0.0
    cost_usdt: float = 0.0    # 含手续费总成本 USDT
    cost_price: float = 0.0   # 含手续费平均成本价 USDT/ETH
    fee: float = 0.0
    open_ts: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.size_eth > 0 and self.side != ""

    def unrealized(self, mark: float) -> float:
        if not self.is_open:
            return 0.0
        return okx_math.unrealized_pnl(self.side, self.entry, mark, self.size_eth)

    def to_dict(self, mark: float) -> dict:
        upl = self.unrealized(mark)
        return {
            "side": self.side or "flat",
            "size_eth": round(self.size_eth, 6),
            "notional_usdt": round(okx_math.notional(self.size_eth, self.entry), 2),
            "entry": round(self.entry, 2),
            "mark": round(mark, 2),
            "margin_usdt": round(self.margin, 4),
            "cost_usdt": round(self.cost_usdt, 4),
            "cost_price": round(self.cost_price, 2),
            "liq_px": round(self.liq_px, 2) if self.liq_px else None,
            "unrealized_pnl": round(upl, 4),
            "open_ts": self.open_ts,
        }


class Broker:
    """统一接口：策略与引擎只依赖这个抽象。"""

    mode = "abstract"

    def __init__(self, feed: OkxFeed, cfg: ExchangeConfig, risk: RiskConfig):
        self.feed = feed
        self.cfg = cfg
        self.risk = risk
        self.position = Position()
        self.trades: list[dict] = []

    # ---- 子类实现 ----
    async def open_position(self, side: str, size_eth: float) -> Position:
        raise NotImplementedError

    async def close_position(self, reason: str = "", fill_px: Optional[float] = None) -> dict:
        raise NotImplementedError

    def equity(self, mark: float) -> float:
        raise NotImplementedError

    # ---- 公共 ----
    def compute_size(self, price: float) -> tuple[float, float]:
        """按保证金×杠杆计算开仓量，上限 max_notional，取整到 lot。

        返回 (张数, ETH 数量)；不满足最小下单量时返回 (0, 0)。
        """
        spec = self.feed.spec
        notional = min(self.risk.margin_per_trade * self.risk.leverage, self.risk.max_notional)
        eth = notional / price if price > 0 else 0.0
        contracts = okx_math.round_to_lot(okx_math.eth_to_contracts(eth, spec.ct_val), spec.lot_sz)
        if contracts < spec.min_sz:
            logger.warning(f"下单量 {contracts} 张 < 最小 {spec.min_sz} 张，跳过开仓")
            return 0, 0
        return contracts, okx_math.contracts_to_eth(contracts, spec.ct_val)

    def buyable_usdt(self) -> float:
        """可买（USDT）：可用资金 × 杠杆，受 max_notional 约束。"""
        return 0.0

    def snapshot(self, mark: float) -> dict:
        return {
            "mode": self.mode,
            "price": round(mark, 2),
            "position": self.position.to_dict(mark),
            "buyable_usdt": round(self.buyable_usdt(), 2),
            "trades_count": len(self.trades),
        }

    def _record_trade(self, side: str, size_eth: float, entry: float, exit_px: float,
                      pnl: float, fee: float, funding: float, reason: str) -> None:
        self.trades.append({
            "ts": time.time(), "side": side, "size_eth": size_eth,
            "entry": entry, "exit": exit_px, "pnl": pnl,
            "fee": fee, "funding": funding, "reason": reason,
        })


class PaperBroker(Broker):
    """纸盘：真实行情 + 本地撮合 + 独立钱包。"""

    mode = "paper"

    def __init__(self, feed: OkxFeed, cfg: ExchangeConfig, risk: RiskConfig, wallet: Wallet):
        super().__init__(feed, cfg, risk)
        self.wallet = wallet

    @property
    def taker_fee(self) -> float:
        return self.feed.taker_fee

    async def _fill_price(self, side: str, size_eth: float) -> float:
        await self.feed.ensure_order_book()
        ob = self.feed.order_book
        if ob is None:
            px = self.feed.price
            if px <= 0:
                raise RuntimeError("尚无行情，无法撮合")
            return px
        return fills.depth_fill_price(ob, side, size_eth, self.risk.slippage_bps)

    async def open_position(self, side: str, size_eth: float) -> Position:
        spec = self.feed.spec
        fill = await self._fill_price(side, size_eth)
        notional = okx_math.notional(size_eth, fill)
        fee = notional * self.taker_fee
        margin = okx_math.margin_required(notional, self.risk.leverage)
        if margin > self.wallet.balance:
            logger.warning(f"保证金不足: 需 {margin:.4f} 可用 {self.wallet.balance:.4f}")
            return self.position

        self.wallet.lock_margin(margin)
        self.wallet.pay_fee(fee)
        liq = okx_math.liquidation_price(side, fill, size_eth, margin, spec.mmr, self.taker_fee)
        self.position = Position(
            side=side, size_eth=size_eth, entry=fill, margin=margin,
            liq_px=liq,
            cost_usdt=okx_math.cost_including_fee(fill, size_eth, self.taker_fee),
            cost_price=okx_math.cost_price_including_fee(fill, self.taker_fee),
            fee=fee, open_ts=time.time(),
        )
        logger.info(f"纸盘开仓 {side} {size_eth:.4f} ETH @ {fill:.2f} "
                    f"保证金 {margin:.4f} 强平价 {liq:.2f}")
        return self.position

    async def close_position(self, reason: str = "", fill_px: Optional[float] = None) -> dict:
        pos = self.position
        if not pos.is_open:
            return {}
        px = fill_px if fill_px is not None else await self._fill_price(
            "sell" if pos.side == "long" else "buy", pos.size_eth)
        pnl = okx_math.unrealized_pnl(pos.side, pos.entry, px, pos.size_eth)
        fee = okx_math.notional(pos.size_eth, px) * self.taker_fee
        self.wallet.unlock_margin(pos.margin)
        self.wallet.pay_fee(fee)
        self.wallet.add_pnl(pnl)
        self._record_trade(pos.side, pos.size_eth, pos.entry, px, pnl, fee, 0.0, reason)
        logger.info(f"纸盘平仓 {pos.side} {pos.size_eth:.4f} ETH @ {px:.2f} "
                    f"盈亏 {pnl:+.4f} 原因: {reason or '手动'}")
        result = {"side": pos.side, "size_eth": pos.size_eth, "entry": pos.entry,
                  "exit": px, "pnl": pnl, "fee": fee, "reason": reason}
        self.position = Position()
        return result

    async def check_liquidation(self, mark: float) -> Optional[dict]:
        """带缓冲的强平检查：接近 OKX 强平价即提前离场。"""
        pos = self.position
        if not pos.is_open or pos.liq_px <= 0:
            return None
        stop = okx_math.buffered_liq_price(pos.side, pos.entry, pos.liq_px, self.risk.liquidation_buffer)
        hit = mark <= stop if pos.side == "long" else mark >= stop
        if hit:
            logger.warning(f"触发强平保护 {pos.side} @ {mark:.2f} (缓冲强平价 {stop:.2f})")
            return await self.close_position(reason="liquidation", fill_px=stop)
        return None

    def equity(self, mark: float) -> float:
        return self.wallet.equity(self.position.unrealized(mark))

    def buyable_usdt(self) -> float:
        return min(self.wallet.balance * self.risk.leverage, self.risk.max_notional)

    def wallet_view(self) -> dict:
        return {
            "initial_balance": round(self.wallet.initial_balance, 4),
            "balance": round(self.wallet.balance, 4),
            "margin_locked": round(self.wallet.margin_locked, 4),
            "fees_paid": round(self.wallet.fees_paid, 4),
            "funding_paid": round(self.wallet.funding_paid, 4),
            "realized_pnl": round(self.wallet.realized_pnl, 4),
        }


class LiveBroker(Broker):
    """实盘：真实下单；持仓/余额/强平价全部以交易所返回为准。"""

    mode = "live"

    def __init__(self, feed: OkxFeed, cfg: ExchangeConfig, risk: RiskConfig):
        super().__init__(feed, cfg, risk)
        self.exchange = feed.exchange
        self.equity_usdt: float = 0.0
        self.available_usdt: float = 0.0

    async def _set_leverage(self) -> None:
        try:
            await self.exchange.set_leverage(self.risk.leverage, self.cfg.symbol,
                                             params={"mgnMode": "isolated"})
        except Exception as e:
            logger.warning(f"设置杠杆失败（可能已设置）: {e}")

    async def open_position(self, side: str, size_eth: float) -> Position:
        await self._set_leverage()
        contracts = okx_math.round_to_lot(
            okx_math.eth_to_contracts(size_eth, self.feed.spec.ct_val), self.feed.spec.lot_sz)
        order = await self.exchange.create_order(self.cfg.symbol, "market", side, contracts)
        logger.info(f"实盘开仓 {side} {contracts} 张 order={order.get('id')}")
        await self.refresh_position()
        return self.position

    async def close_position(self, reason: str = "", fill_px: Optional[float] = None) -> dict:
        pos = self.position
        if not pos.is_open:
            return {}
        contracts = okx_math.round_to_lot(
            okx_math.eth_to_contracts(pos.size_eth, self.feed.spec.ct_val), self.feed.spec.lot_sz)
        side = "sell" if pos.side == "long" else "buy"
        order = await self.exchange.create_order(self.cfg.symbol, "market", side, contracts,
                                                 params={"reduceOnly": True})
        logger.info(f"实盘平仓 {contracts} 张 order={order.get('id')} 原因: {reason or '手动'}")
        await self.refresh_position()
        self._record_trade(pos.side, pos.size_eth, pos.entry, self.feed.price, 0.0, 0.0, 0.0, reason)
        return {"side": pos.side, "reason": reason}

    async def refresh_position(self) -> None:
        """以交易所数据为准刷新持仓与资金。"""
        positions = await self.exchange.fetch_positions([self.cfg.symbol])
        pos = positions[0] if positions else {}
        if pos and float(pos.get("contracts") or 0) > 0:
            side = pos["side"]
            size = float(pos["contracts"]) * self.feed.spec.ct_val
            entry = float(pos.get("entryPrice") or 0)
            margin = float(pos.get("margin") or 0)
            self.position = Position(
                side=side, size_eth=size, entry=entry, margin=margin,
                liq_px=float(pos.get("liquidationPrice") or 0),
                cost_usdt=float(pos.get("notional") or 0),
                cost_price=entry,
                open_ts=time.time(),
            )
        else:
            self.position = Position()
        bal = await self.exchange.fetch_balance()
        self.equity_usdt = float(bal["USDT"].get("total") or 0)
        self.available_usdt = float(bal["USDT"].get("free") or 0)

    def equity(self, mark: float) -> float:
        return self.equity_usdt

    def buyable_usdt(self) -> float:
        return min(self.available_usdt * self.risk.leverage, self.risk.max_notional)
