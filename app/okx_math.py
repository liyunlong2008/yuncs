"""OKX 计算方法：强平价 / 保证金 / 资金费 / 成本 / 仓位换算。

纸盘与回测按这里计算，保证与交易所口径一致；实盘以交易所返回的 liqPx / 余额为准。

强平价公式（OKX 帮助中心「逐仓交易规则」, USDT 本位线性合约）：
  多仓: liq = (保证金余额 - 面值×|张数|×开仓均价) / (面值×|张数|×(MMR + 费率 - 1))
  空仓: liq = (保证金余额 + 面值×|张数|×开仓均价) / (面值×|张数|×(MMR + 费率 + 1))
  其中 MMR 为仓位档位维持保证金率、费率为用户 taker 费率（含强平手续费）。

资金费（每 8 小时，UTC 00:00/08:00/16:00 结算）：
  funding_fee = 持仓量(ETH) × 标记价 × 资金费率
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 资金费结算时刻（UTC）
FUNDING_HOURS = (0, 8, 16)


def round_to_lot(value: float, lot: float) -> float:
    """向下取整到最小交易步进（张数必须为 lot 的整数倍）。"""
    if lot <= 0:
        return value
    return int(value / lot) * lot


def contracts_to_eth(contracts: float, ct_val: float) -> float:
    return contracts * ct_val


def eth_to_contracts(eth: float, ct_val: float) -> float:
    return eth / ct_val


def notional(size_eth: float, price: float) -> float:
    """名义价值（USDT）。"""
    return size_eth * price


def margin_required(notional_usdt: float, leverage: float) -> float:
    return notional_usdt / leverage


def cost_including_fee(fill_price: float, size_eth: float, fee_rate: float) -> float:
    """含手续费的总成本 USDT（成本 = 名义价值 + 手续费）。"""
    return notional(size_eth, fill_price) * (1.0 + fee_rate)


def cost_price_including_fee(fill_price: float, fee_rate: float) -> float:
    """含手续费后的平均成本价 USDT/ETH。"""
    return fill_price * (1.0 + fee_rate)


def unrealized_pnl(side: str, entry: float, mark: float, size_eth: float) -> float:
    if side == "long":
        return (mark - entry) * size_eth
    return (entry - mark) * size_eth


def funding_fee(size_eth: float, mark_price: float, funding_rate: float) -> float:
    """资金费（USDT），按 OKX：持仓量 × 标记价 × 资金费率。"""
    return size_eth * mark_price * funding_rate


def liquidation_price(
    side: str,
    entry: float,
    size_eth: float,
    margin_usdt: float,
    mmr: float,
    taker_fee: float,
) -> float:
    """逐仓（isolated）线性合约预估强平价，按 OKX 官方公式。

    margin_usdt 为仓位保证金（初始保证金 + 追加保证金，不含未实现盈亏）。
    """
    value = entry * size_eth
    if side == "long":
        denom = size_eth * (mmr + taker_fee - 1.0)
        return (margin_usdt - value) / denom if abs(denom) > 1e-12 else 0.0
    denom = size_eth * (mmr + taker_fee + 1.0)
    return (margin_usdt + value) / denom if abs(denom) > 1e-12 else 0.0


def buffered_liq_price(side: str, entry: float, liq_px: float, buffer: float) -> float:
    """带缓冲的离场价：提前 buffer 比例离场，防本地计算与交易所强平价误差。

    多仓缓冲价高于真实强平价（更靠近开仓价、更安全）；空仓反之。
    """
    distance = abs(entry - liq_px)
    if side == "long":
        return liq_px + distance * buffer
    return liq_px - distance * buffer


def next_funding_time(dt: datetime) -> datetime:
    """下一个资金费结算时刻（UTC 00:00/08:00/16:00）。"""
    dt = dt.astimezone(timezone.utc)
    for h in FUNDING_HOURS:
        if dt.hour < h:
            return dt.replace(hour=h, minute=0, second=0, microsecond=0)
    return (dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
