"""历史数据下载（回测用）：K 线 CSV 缓存。"""
from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from typing import Optional


async def download_candles(exchange, symbol: str, timeframe: str,
                           since_ms: int, until_ms: int) -> list[dict]:
    """按 OKX 原生接口反向翻页下载历史 K 线（ccxt 的 since 分页对 OKX 有兼容问题）。

    /market/history-candles 的 after 参数返回该时间戳之前的记录（newest-first），
    从 until 逐批往回翻，直到覆盖 since。
    """
    market = exchange.market(symbol)
    inst_id = market["id"]
    bar = (exchange.timeframes or {}).get(timeframe, timeframe)
    raw: list[list] = []
    cursor = until_ms
    while cursor > since_ms:
        resp = await exchange.publicGetMarketHistoryCandles(
            {"instId": inst_id, "bar": bar, "after": cursor, "limit": 300})
        batch = resp.get("data", [])
        if not batch:
            break
        raw.extend(batch)
        oldest = int(batch[-1][0])  # OKX 的 after 参数必须是整数时间戳（ms）
        if oldest >= cursor:
            break  # 没有前进，防止死循环
        cursor = oldest
        await asyncio.sleep(0.2)

    bars = [
        {"ts": float(c[0]), "o": float(c[1]), "h": float(c[2]),
         "l": float(c[3]), "c": float(c[4]), "v": float(c[5])}
        for c in raw
    ]
    bars = [b for b in bars if since_ms <= b["ts"] <= until_ms]
    bars.sort(key=lambda b: b["ts"])
    return bars


def save_candles_csv(path: Path, bars: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "o", "h", "l", "c", "v"])
        w.writeheader()
        for b in bars:
            w.writerow({k: b[k] for k in w.fieldnames})


def load_candles_csv(path: Path) -> Optional[list[dict]]:
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    return [{k: float(r[k]) for k in r} for r in rows]
