"""入口：python -m app.backtest --start 2026-08-01 --end 2026-08-31 [--timeframe 1m] [--force]

玩法适配回测：同一套策略/挑战/撮合/资金费逻辑，K 线 CSV 缓存到 data/。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from . import data
from .backtest import Backtest
from .config import load_config
from .log import setup_logging
from .okx_feed import OkxFeed
from .store import Store


def to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


async def main() -> None:
    p = argparse.ArgumentParser(description="yuncs 玩法适配回测")
    p.add_argument("--start", required=True, help="如 2026-08-01")
    p.add_argument("--end", required=True, help="如 2026-08-31")
    p.add_argument("--timeframe", default=None)
    p.add_argument("--config", default="config.toml")
    p.add_argument("--force", action="store_true", help="忽略 CSV 缓存重新下载")
    args = p.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.log.level)
    tf = args.timeframe or cfg.challenge.timeframe
    since, until = to_ms(args.start), to_ms(args.end)
    if until <= since:
        raise SystemExit("--end 需晚于 --start")

    feed = OkxFeed(cfg.exchange, cfg.secrets)
    await feed.exchange.load_markets()
    await feed.load_spec_and_fees()
    try:
        cache = Path(f"data/candles_{tf}_{args.start}_{args.end}.csv")
        bars = data.load_candles_csv(cache) if cache.exists() and not args.force else None
        if bars is None:
            logger.info(f"下载 K 线 {tf} {args.start} -> {args.end}")
            bars = await data.download_candles(feed.exchange, cfg.exchange.symbol, tf, since, until)
            data.save_candles_csv(cache, bars)
        funding = await feed.fetch_funding_rate_history(since, until)
        logger.info(f"K 线 {len(bars)} 根, 资金费记录 {len(funding)} 条")

        bt = Backtest(cfg, bars, funding, feed.spec)
        report = bt.run()

        store = Store(cfg.storage.db_path)
        await store.init()
        run_id = await store.start_run(
            "backtest", bt.strategy.name, cfg.challenge.initial_balance,
            cfg.model_dump(exclude={"secrets"}),
        )
        for t in bt.trades:
            await store.add_trade(run_id, t)
        for s in bt.equity_curve:
            await store.add_equity(run_id, {
                "ts": s["ts"] / 1000, "equity": s["equity"], "balance": s["equity"],
                "margin": 0.0, "unrealized": 0.0,
                "drawdown_pct": s["drawdown_pct"], "challenge_status": s["status"],
            })
        await store.finish_run(run_id, report["challenge_status"],
                               report["challenge_result"] or "数据结束",
                               report["peak_equity"], report["final_equity"])
        await store.close()

        print("\n===== 回测报告 =====")
        for k, v in report.items():
            print(f"  {k}: {v}")
    finally:
        await feed.exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
