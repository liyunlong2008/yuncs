"""长周期稳健性回测：1年@5m + 3年@15m，纸盘/实盘(连续)两种语义对比，防过拟合。

用法: uv run python scripts/long_backtest.py
K 线按天增量缓存复用；结果打印到 stdout。
"""
import sys
import asyncio
sys.path.insert(0, ".")

from app import data
from app.backtest import Backtest
from app.config import load_config
from app.okx_feed import OkxFeed

WINDOWS = [
    ("1年@5m", "2025-09-01", "2026-09-01", "5m"),
    ("3年@15m", "2023-09-01", "2026-09-01", "15m"),
]


async def ensure_bars(feed, cfg, tf: str, start: str, end: str) -> list[dict]:
    bars: list[dict] = []
    for day in data.iter_days(start, end):
        path = data.day_cache_path(tf, day)
        day_bars = data.load_candles_csv(path)
        if day_bars is None:
            day_bars = await data.download_candles(
                feed.exchange, cfg.exchange.symbol, tf,
                data.date_to_ms(day), data.date_to_ms(day) + data.DAY_MS)
            data.save_candles_csv(path, day_bars)
            print(f"  下载 {day} {tf}: {len(day_bars)} 根", flush=True)
        bars.extend(day_bars)
    return sorted({b["ts"]: b for b in bars}.values(), key=lambda b: b["ts"])


async def main() -> None:
    cfg = load_config("config.toml")
    cfg.strategy.name = "donchian"  # 固定当前默认策略
    feed = OkxFeed(cfg.exchange, cfg.secrets)
    await feed.exchange.load_markets()
    try:
        for label, start, end, tf in WINDOWS:
            print(f"\n===== {label} {start}~{end} =====", flush=True)
            bars = await ensure_bars(feed, cfg, tf, start, end)
            print(f"  K线 {len(bars)} 根", flush=True)
            for comp_name, comp in (("纸盘(重置20U)", False), ("实盘(连续复利)", True)):
                bt = Backtest(cfg, bars, [], compounding=comp)
                r = bt.run()
                print(
                    f"  [{comp_name}] 期末 {r['end_equity']}U 复合 {r['compounded_multiple']}x "
                    f"峰值 {r['max_equity_seen']}U 周期{r['rounds_total']} "
                    f"正{r['rounds_positive']}/{r['rounds_completed']} 胜率{r['win_rate']}% "
                    f"交易{r['trades']} 强平{r['liquidation_count']}", flush=True)
    finally:
        await feed.exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
