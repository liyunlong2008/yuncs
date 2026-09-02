"""入口：python -m app.backtest_cli --start 2026-08-25 --end 2026-09-01 [--timeframe 5m]

玩法适配回测：按轮次回放 10u 战神玩法（动态出局线）。
K 线按天增量缓存到 data/candles_{周期}_{日期}.csv：同区间/重叠区间直接复用，
滚动延长只下载新日子。资金费历史每次拉取（量小价廉）。
"""
from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from . import data
from .backtest import Backtest
from .config import load_config
from .log import setup_logging
from .okx_feed import OkxFeed
from .store import Store


async def main() -> None:
    p = argparse.ArgumentParser(description="yuncs 玩法适配回测")
    p.add_argument("--start", required=True, help="如 2026-08-25")
    p.add_argument("--end", required=True, help="如 2026-09-01")
    p.add_argument("--timeframe", default=None)
    p.add_argument("--config", default="config.toml")
    p.add_argument("--force", action="store_true", help="忽略缓存重新下载")
    args = p.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.log.level)
    if cfg.challenge.initial_balance <= 0:
        raise SystemExit("回测 [challenge].initial_balance 必须 > 0")
    tf = args.timeframe or cfg.challenge.timeframe
    days = data.iter_days(args.start, args.end)

    feed = OkxFeed(cfg.exchange, cfg.secrets)
    await feed.exchange.load_markets()
    await feed.load_spec_and_fees()
    try:
        bars: list[dict] = []
        for day in days:
            path = data.day_cache_path(tf, day)
            day_bars = data.load_candles_csv(path) if path.exists() and not args.force else None
            if day_bars is None:
                day_bars = await data.download_candles(
                    feed.exchange, cfg.exchange.symbol, tf,
                    data.date_to_ms(day), data.date_to_ms(day) + data.DAY_MS)
                data.save_candles_csv(path, day_bars)
                logger.info(f"下载 {day} {tf}: {len(day_bars)} 根")
            bars.extend(day_bars)
        bars = sorted({b["ts"]: b for b in bars}.values(), key=lambda b: b["ts"])
        if not bars:
            raise SystemExit("无 K 线数据")

        since_ms = data.date_to_ms(args.start)
        funding = await feed.fetch_funding_rate_history(
            since_ms, data.date_to_ms(args.end) + data.DAY_MS)
        logger.info(f"K 线 {len(bars)} 根（{len(days)} 天）, 资金费记录 {len(funding)} 条")

        bt = Backtest(cfg, bars, funding, feed.spec)
        report = bt.run()

        store = Store(cfg.storage.db_path)
        await store.init()
        for r in bt.rounds:
            run_id = await store.start_run(
                "backtest", bt.strategy.name, cfg.challenge.initial_balance,
                cfg.model_dump(exclude={"secrets"}),
            )
            await store.finish_run(run_id, r["status"], r["result"] or "",
                                   r["peak"], r["final"])
        await store.close()

        print("\n===== 回测报告（轮次分布）=====")
        for k, v in report.items():
            print(f"  {k}: {v}")
        print("\n===== 每轮明细 =====")
        for r in bt.rounds:
            print(f"  轮{r['round']:>3} [{r['status']}] 结束 {r['multiple']:>7.3f}x "
                  f"(初始{r['initial']:.2f} -> {r['final']:.2f}) 峰值{r['peak']:.2f} {r['result'] or ''}")
    finally:
        await feed.exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
