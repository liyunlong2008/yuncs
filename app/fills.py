"""成交价估算：纸盘与回测共用。

- 纸盘市价单：按真实盘口深度加权计算成交均价（深度不足以覆盖时用最后一档 + 滑点）
- 回测市价单：按 K 线 open ± 滑点
"""
from __future__ import annotations


def depth_fill_price(order_book: dict, side: str, size_eth: float, slippage_bps: float = 0.0) -> float:
    """按盘口深度计算市价成交均价。side: buy(吃卖一) / sell(吃买一)。"""
    levels = order_book["asks"] if side == "buy" else order_book["bids"]
    if not levels:
        return 0.0
    filled = 0.0
    cost = 0.0
    for price, amount in levels:
        take = min(size_eth, float(amount))
        cost += take * float(price)
        filled += take
        if filled >= size_eth - 1e-12:
            break
    avg = cost / filled if filled > 0 else float(levels[0][0])
    slip = avg * slippage_bps / 10000.0
    return avg + slip if side == "buy" else avg - slip


def candle_fill_price(bar: dict, side: str, slippage_bps: float = 0.0) -> float:
    """回测：按 K 线开盘价 ± 滑点成交。"""
    px = bar["o"]
    slip = px * slippage_bps / 10000.0
    return px + slip if side == "buy" else px - slip
