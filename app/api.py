"""FastAPI：状态查询 REST + WebSocket 推送 + 静态看板。"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .engine import Engine

STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="yuncs", docs_url="/api/docs")

    @app.get("/api/status")
    async def status():
        snap = engine.latest_snapshot()
        return {"ok": True, "running": bool(snap), "ts": time.time(), **snap}

    @app.get("/api/challenge")
    async def challenge():
        return engine.latest_snapshot().get("challenge", {})

    @app.get("/api/position")
    async def position():
        return engine.latest_snapshot().get("position", {})

    @app.get("/api/trades")
    async def trades(limit: int = 100):
        if engine.run_id and engine.store.conn:
            return await engine.store.fetch_trades(engine.run_id, limit)
        return []

    @app.get("/api/equity")
    async def equity(limit: int = 2000):
        if engine.run_id and engine.store.conn:
            return await engine.store.fetch_equity(engine.run_id, limit)
        return []

    @app.get("/api/runs")
    async def runs(limit: int = 20):
        if engine.store.conn:
            return await engine.store.fetch_runs(limit)
        return []

    @app.get("/api/config")
    async def config():
        return engine.cfg.model_dump(exclude={"secrets"})

    @app.post("/api/kill")
    async def kill():
        await engine.request_stop()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            while True:
                await engine.wait_snapshot()
                await sock.send_json(engine.latest_snapshot())
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
