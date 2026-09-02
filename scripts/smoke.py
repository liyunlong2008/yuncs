"""冒烟验证：代理连 OKX，拉 spec/费率/历史，订阅 20 秒行情。"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.config import load_config
from app.okx_feed import OkxFeed


async def main():
    cfg = load_config("config.toml")
    feed = OkxFeed(cfg.exchange, cfg.secrets)
    print("proxy:", cfg.exchange.proxy)
    await feed.load_spec_and_fees()
    print("spec:", feed.spec)
    print("fees: taker", feed.taker_fee, "maker", feed.maker_fee)

    got = {"trades": 0, "bars": 0, "books": 0}

    async def on_trade(t):
        got["trades"] += 1
    async def on_bar(b):
        got["bars"] += 1
        got["last_bar"] = b
    async def on_book(b):
        got["books"] += 1

    feed.subscribe("trade", on_trade)
    feed.subscribe("bar", on_bar)
    feed.subscribe("book", on_book)
    await feed.start()
    await asyncio.sleep(20)
    print(f"20s 行情: trades={got['trades']} bars={got['bars']} books={got['books']} "
          f"price={feed.price} bid={feed.bid} ask={feed.ask} funding={feed.funding_rate}")

    hist = await feed.fetch_ohlcv_history("1m", 5)
    print("warmup candles:", len(hist), "last ts:", hist[-1]["ts"] if hist else None)

    import time
    until = int(time.time() * 1000)
    fr = await feed.fetch_funding_rate_history(until - 7 * 86400_000, until)
    print("funding history (7d):", len(fr), "last:", fr[-1] if fr else None)

    await feed.close()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
