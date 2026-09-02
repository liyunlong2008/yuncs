"""SQLite 持久化：挑战轮次 / 成交 / 权益采样。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT, strategy TEXT, initial_balance REAL, config TEXT,
                status TEXT, result TEXT, started REAL, ended REAL,
                peak_equity REAL, final_equity REAL
            );
            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER, ts REAL, side TEXT, size_eth REAL, entry REAL,
                exit_px REAL, pnl REAL, fee REAL, funding REAL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS equity_samples(
                run_id INTEGER, ts REAL, equity REAL, balance REAL, margin REAL,
                unrealized REAL, drawdown_pct REAL, challenge_status TEXT
            );
            """
        )
        await self.conn.commit()

    async def start_run(self, mode: str, strategy: str, initial_balance: float,
                        config: dict) -> int:
        cur = await self.conn.execute(
            "INSERT INTO runs(mode, strategy, initial_balance, config, status, started)"
            " VALUES(?,?,?,?,?,?)",
            (mode, strategy, initial_balance, json.dumps(config, ensure_ascii=False),
             "running", time.time()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def finish_run(self, run_id: int, status: str, result: str,
                         peak_equity: float, final_equity: float) -> None:
        await self.conn.execute(
            "UPDATE runs SET status=?, result=?, ended=?, peak_equity=?, final_equity=? WHERE id=?",
            (status, result, time.time(), peak_equity, final_equity, run_id),
        )
        await self.conn.commit()

    async def add_trade(self, run_id: int, t: dict) -> None:
        await self.conn.execute(
            "INSERT INTO trades(run_id, ts, side, size_eth, entry, exit_px, pnl, fee, funding, reason)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, t["ts"], t["side"], t["size_eth"], t["entry"], t["exit"],
             t["pnl"], t["fee"], t.get("funding", 0.0), t["reason"]),
        )
        await self.conn.commit()

    async def add_equity(self, run_id: int, sample: dict) -> None:
        await self.conn.execute(
            "INSERT INTO equity_samples(run_id, ts, equity, balance, margin, unrealized,"
            " drawdown_pct, challenge_status) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, sample["ts"], sample["equity"], sample["balance"],
             sample["margin"], sample["unrealized"], sample["drawdown_pct"],
             sample["challenge_status"]),
        )
        await self.conn.commit()

    async def fetch_trades(self, run_id: int, limit: int = 200) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT ts, side, size_eth, entry, exit_px, pnl, fee, funding, reason"
            " FROM trades WHERE run_id=? ORDER BY ts DESC LIMIT ?", (run_id, limit))
        rows = await cur.fetchall()
        cols = ["ts", "side", "size_eth", "entry", "exit", "pnl", "fee", "funding", "reason"]
        return [dict(zip(cols, r)) for r in rows]

    async def fetch_equity(self, run_id: int, limit: int = 5000) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT ts, equity, drawdown_pct, challenge_status FROM equity_samples"
            " WHERE run_id=? ORDER BY ts LIMIT ?", (run_id, limit))
        rows = await cur.fetchall()
        cols = ["ts", "equity", "drawdown_pct", "challenge_status"]
        return [dict(zip(cols, r)) for r in rows]

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
