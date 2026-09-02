"""入口：python -m app.run [--mode paper|live] [--config config.toml]

启动机器人（纸盘/实盘）+ FastAPI 看板，同一进程。
"""
from __future__ import annotations

import argparse
import asyncio

from .config import load_config
from .engine import Engine
from .log import setup_logging
from .store import Store


async def main(mode: str | None, config_path: str) -> None:
    cfg = load_config(config_path)
    if mode:
        cfg.exchange.mode = mode
    if cfg.exchange.mode == "live" and not cfg.secrets.api_key:
        raise SystemExit("实盘模式需要 secrets.toml 配置 OKX API key（开启交易权限）")
    setup_logging(cfg.log.level)

    store = Store(cfg.storage.db_path)
    engine = Engine(cfg, store)

    from .api import create_app

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        create_app(engine), host=cfg.api.host, port=cfg.api.port, log_level="warning"))
    api_task = asyncio.create_task(server.serve())
    try:
        await engine.run()
    finally:
        server.should_exit = True
        api_task.cancel()
        await store.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="yuncs 挑战赛机器人")
    p.add_argument("--mode", choices=["paper", "live"], default=None, help="覆盖 config.toml 的 mode")
    p.add_argument("--config", default="config.toml")
    args = p.parse_args()
    asyncio.run(main(args.mode, args.config))
