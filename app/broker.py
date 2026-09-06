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
from .okx_feed import InstrumentSpec, OkxFeed
from .wallet import Wallet


def fraction_eth_for_spec(spec: InstrumentSpec, plan_eth: float, frac: float) -> float:
    """按计划量切分批仓位：张数取整到 lot、不足最小单量返回 0（三模式同口径）。"""
    if plan_eth <= 0 or frac <= 0:
        return 0.0
    cts = okx_math.round_to_lot(
        okx_math.eth_to_contracts(plan_eth, spec.ct_val) * frac, spec.lot_sz)
    if cts < spec.min_sz:
        return 0.0
    return okx_math.contracts_to_eth(cts, spec.ct_val)


@dataclass
class Position:
    side: str = ""            # long / short / ""
    size_eth: float = 0.0
    entry: float = 0.0
    margin: float = 0.0       # 占用保证金 USDT（分批加仓 = 各批初始保证金之和）
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


# ---------- 纸盘/回测共享记账核心（与 OKX 线性口径一致，禁止别处自创公式） ----------

def open_position_math(wallet: Wallet, side: str, size_eth: float, fill: float,
                       fee_rate: float, leverage: float, mmr: float,
                       now_ts: Optional[float] = None) -> Optional[Position]:
    """新开仓记账：锁保证金 + 付开仓费 + 按 OKX 公式算强平价。

    保证金不足返回 None（账不动），由调用方按各自模式提示。
    """
    notional_usdt = okx_math.notional(size_eth, fill)
    fee = notional_usdt * fee_rate
    margin = okx_math.margin_required(notional_usdt, leverage)
    if margin > wallet.balance:
        return None
    wallet.lock_margin(margin)
    wallet.pay_fee(fee)
    liq = okx_math.liquidation_price(side, fill, size_eth, margin, mmr, fee_rate)
    return Position(
        side=side, size_eth=size_eth, entry=fill, margin=margin, liq_px=liq,
        cost_usdt=okx_math.cost_including_fee(fill, size_eth, fee_rate),
        cost_price=okx_math.cost_price_including_fee(fill, fee_rate),
        fee=fee, open_ts=now_ts if now_ts is not None else time.time(),
    )


def add_position_math(wallet: Wallet, pos: Position, size_eth: float, fill: float,
                      fee_rate: float, leverage: float, mmr: float) -> Optional[Position]:
    """同向分批加仓记账：加权均价、累计保证金、按累计口径重算强平价。

    只允许同向；保证金不足返回 None（原仓不动）。open_ts 保留首笔时间。
    """
    if not pos.is_open or size_eth <= 0:
        return None
    add_notional = okx_math.notional(size_eth, fill)
    fee = add_notional * fee_rate
    margin = okx_math.margin_required(add_notional, leverage)
    if margin > wallet.balance:
        return None
    wallet.lock_margin(margin)
    wallet.pay_fee(fee)
    new_size = pos.size_eth + size_eth
    entry = (pos.entry * pos.size_eth + fill * size_eth) / new_size
    total_margin = pos.margin + margin
    liq = okx_math.liquidation_price(pos.side, entry, new_size, total_margin, mmr, fee_rate)
    return Position(
        side=pos.side, size_eth=new_size, entry=entry, margin=total_margin,
        liq_px=liq,
        cost_usdt=okx_math.cost_including_fee(entry, new_size, fee_rate),
        cost_price=okx_math.cost_price_including_fee(entry, fee_rate),
        fee=pos.fee + fee, open_ts=pos.open_ts,
    )


def close_position_math(wallet: Wallet, pos: Position, fill_px: float, fee_rate: float,
                        mmr: float, frac: float = 1.0,
                        reason: str = "") -> tuple[dict, Position]:
    """平掉持仓的 frac 比例（0<frac<=1，1=全平）。

    按加权均价计已实现盈亏，解锁 frac 对应保证金（保证金与名义线性，
    按比例精确），手续费 = 平仓费 + 已平部分的开仓费；返回
    (成交记录 dict 同 trades 口径, 剩余仓位按累计口径重算强平价或空仓)。
    """
    if not pos.is_open or frac <= 0:
        return {}, pos
    frac = min(frac, 1.0)
    closed = pos.size_eth * frac
    pnl = okx_math.unrealized_pnl(pos.side, pos.entry, fill_px, closed)
    fee = okx_math.notional(closed, fill_px) * fee_rate + pos.fee * frac
    wallet.unlock_margin(pos.margin * frac)
    wallet.pay_fee(fee)
    wallet.add_pnl(pnl)
    rec = {
        "ts": pos.open_ts, "side": pos.side, "size_eth": closed,
        "entry": pos.entry, "exit": fill_px, "pnl": pnl,
        "fee": fee, "funding": 0.0, "reason": reason,
    }
    remain = pos.size_eth - closed
    if remain <= 1e-12:
        return rec, Position()
    rem_margin = pos.margin * (1.0 - frac)
    liq = okx_math.liquidation_price(pos.side, pos.entry, remain, rem_margin, mmr, fee_rate)
    return rec, Position(
        side=pos.side, size_eth=remain, entry=pos.entry, margin=rem_margin,
        liq_px=liq, cost_usdt=pos.cost_usdt * (1.0 - frac),
        cost_price=pos.cost_price, fee=pos.fee * (1.0 - frac), open_ts=pos.open_ts,
    )


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

    async def add_position(self, side: str, size_eth: float) -> Position:
        """同向分批加仓（只允许与持仓同向；由调用方保证语义）。"""
        raise NotImplementedError

    async def close_position(self, reason: str = "", fill_px: Optional[float] = None,
                             frac: float = 1.0) -> dict:
        """平仓：frac=1.0 全平；0<frac<1 部分平仓（剩余仓位继续管理）。"""
        raise NotImplementedError

    def equity(self, mark: float) -> float:
        raise NotImplementedError

    # ---- 公共 ----
    def _margin_budget(self) -> float:
        """保证金预算：margin_frac>0 按可用资金比例；否则固定 margin_per_trade（上限余额）。"""
        avail = self.available_for_margin()
        if self.risk.margin_frac > 0:
            return avail * self.risk.margin_frac
        return min(self.risk.margin_per_trade, avail)

    def available_for_margin(self) -> float:
        """可用于保证金的资金（paper=钱包余额，live=交易所可用）。"""
        return 0.0

    def compute_size(self, price: float) -> tuple[float, float]:
        """按保证金×杠杆计算开仓量，上限 max_notional，取整到 lot。

        返回 (张数, ETH 数量)；不满足最小下单量时返回 (0, 0)。
        """
        spec = self.feed.spec
        budget = self._margin_budget()
        if budget <= 0:
            return 0, 0
        notional = min(budget * self.risk.leverage, self.risk.max_notional)
        eth = notional / price if price > 0 else 0.0
        contracts = okx_math.round_to_lot(okx_math.eth_to_contracts(eth, spec.ct_val), spec.lot_sz)
        if contracts < spec.min_sz:
            logger.warning(f"下单量 {contracts} 张 < 最小 {spec.min_sz} 张，跳过开仓")
            return 0, 0
        return contracts, okx_math.contracts_to_eth(contracts, spec.ct_val)

    def buyable_usdt(self) -> float:
        """可买（USDT）：可用资金 × 杠杆，受 max_notional 约束。"""
        return 0.0

    def fraction_eth(self, plan_eth: float, frac: float) -> float:
        """按计划量切分批仓位：张数取整到 lot、不足最小单量返回 0。

        136 分仓的首笔/补批都从这里出大小，三模式同一口径。
        """
        return fraction_eth_for_spec(self.feed.spec, plan_eth, frac)

    def snapshot(self, mark: float) -> dict:
        return {
            "mode": self.mode,
            "leverage": self.risk.leverage,
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

    def available_for_margin(self) -> float:
        return self.wallet.balance

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
        fill = await self._fill_price(side, size_eth)
        pos = open_position_math(self.wallet, side, size_eth, fill,
                                 self.taker_fee, self.risk.leverage,
                                 self.feed.spec.mmr)
        if pos is None:
            logger.warning(f"纸盘开仓保证金不足: {size_eth:.4f} ETH @ {fill:.2f}")
            return self.position
        logger.info(f"纸盘开仓 {side} {size_eth:.4f} ETH @ {fill:.2f} "
                    f"保证金 {pos.margin:.4f} 强平价 {pos.liq_px:.2f}")
        return pos

    async def add_position(self, side: str, size_eth: float) -> Position:
        """分批加仓：同向累加，加权均价，累计保证金与强平价按 OKX 口径。"""
        pos = self.position
        if not pos.is_open or side != pos.side or size_eth <= 0:
            logger.warning(f"加仓条件不符: 当前 {pos.side} {pos.size_eth:.4f}, "
                           f"请求 {side} {size_eth:.4f}")
            return pos
        fill = await self._fill_price(side, size_eth)
        merged = add_position_math(self.wallet, pos, size_eth, fill,
                                   self.taker_fee, self.risk.leverage,
                                   self.feed.spec.mmr)
        if merged is None:
            logger.warning(f"纸盘加仓保证金不足: {size_eth:.4f} ETH @ {fill:.2f}")
            return pos
        logger.info(f"纸盘加仓 {side} +{size_eth:.4f} ETH @ {fill:.2f} "
                    f"-> 总 {merged.size_eth:.4f} ETH 均价 {merged.entry:.2f} "
                    f"强平价 {merged.liq_px:.2f}")
        return merged

    async def close_position(self, reason: str = "", fill_px: Optional[float] = None,
                             frac: float = 1.0) -> dict:
        pos = self.position
        if not pos.is_open:
            return {}
        if frac <= 0 or frac >= 1.0:
            # 全平走原路径（可用 fill_px 覆盖成交价，如止损/强平价）
            frac = 1.0
            px = fill_px if fill_px is not None else await self._fill_price(
                "sell" if pos.side == "long" else "buy", pos.size_eth)
        else:
            px = fill_px if fill_px is not None else await self._fill_price(
                "sell" if pos.side == "long" else "buy", pos.size_eth * frac)
        rec, rest = close_position_math(self.wallet, pos, px, self.taker_fee,
                                        self.feed.spec.mmr, frac=frac, reason=reason)
        if rec:
            self._record_trade(pos.side, rec["size_eth"], rec["entry"], px,
                               rec["pnl"], rec["fee"], 0.0, reason)
        self.position = rest
        verb = "纸盘平仓" if frac >= 1.0 else f"纸盘部分平仓 {frac * 100:.0f}%"
        logger.info(f"{verb} {pos.side} {rec.get('size_eth', 0):.4f} ETH @ {px:.2f} "
                    f"盈亏 {rec.get('pnl', 0):+.4f} 原因: {reason or '手动'}"
                    + (f" 剩余 {rest.size_eth:.4f} ETH" if rest.is_open else ""))
        result = {"side": pos.side, "size_eth": rec.get("size_eth", 0.0),
                  "entry": rec.get("entry", pos.entry), "exit": px,
                  "pnl": rec.get("pnl", 0.0), "fee": rec.get("fee", 0.0),
                  "reason": reason, "frac": frac, "remaining": rest.is_open}
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

    def available_for_margin(self) -> float:
        return self.available_usdt

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

    async def add_position(self, side: str, size_eth: float) -> Position:
        """实盘加仓 = 同向市价单，总量/均价由交易所合并后 refresh 对账。"""
        return await self.open_position(side, size_eth)

    async def close_position(self, reason: str = "", fill_px: Optional[float] = None,
                             frac: float = 1.0) -> dict:
        pos = self.position
        if not pos.is_open:
            return {}
        size = pos.size_eth if frac >= 1.0 else pos.size_eth * frac
        contracts = okx_math.round_to_lot(
            okx_math.eth_to_contracts(size, self.feed.spec.ct_val), self.feed.spec.lot_sz)
        if contracts <= 0:
            logger.warning(f"部分平仓量不足 1 张，跳过: frac={frac} size={size:.6f}")
            return {}
        side = "sell" if pos.side == "long" else "buy"
        order = await self.exchange.create_order(self.cfg.symbol, "market", side, contracts,
                                                 params={"reduceOnly": True})
        logger.info(f"实盘{'部分' if frac < 1.0 else ''}平仓 {contracts} 张 "
                    f"order={order.get('id')} 原因: {reason or '手动'}")
        await self.refresh_position()
        if frac >= 1.0:
            self._record_trade(pos.side, pos.size_eth, pos.entry, self.feed.price,
                               0.0, 0.0, 0.0, reason)
        return {"side": pos.side, "reason": reason, "frac": frac,
                "remaining": self.position.is_open}

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
