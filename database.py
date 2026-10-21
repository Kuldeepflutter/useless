"""Small SQLite journal used to make order submission and restart recovery auditable."""
from __future__ import annotations

import json
import sqlite3
from threading import RLock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str = "grid_bot.sqlite3"):
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS bot_orders (
          exchange_order_id TEXT PRIMARY KEY, pair TEXT NOT NULL, level_index INTEGER NOT NULL,
          side TEXT NOT NULL, price TEXT NOT NULL, quantity TEXT NOT NULL, status TEXT NOT NULL,
          filled_quantity TEXT NOT NULL DEFAULT '0', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, severity TEXT NOT NULL,
          message TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS grid_cycles (
          close_order_id TEXT PRIMARY KEY, pair TEXT NOT NULL, entry_order_id TEXT,
          entry_side TEXT NOT NULL, entry_price TEXT NOT NULL, close_price TEXT NOT NULL,
          quantity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
          realized_profit_inr TEXT NOT NULL DEFAULT '0', created_at TEXT NOT NULL,
          completed_at TEXT
        );
            """)
            self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def log(self, severity: str, message: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.connection.execute("INSERT INTO events(created_at,severity,message,payload_json) VALUES(?,?,?,?)",
                                    (self._now(), severity, message, json.dumps(payload or {}, default=str)))
            self.connection.commit()

    def save_order(self, *, exchange_order_id: str, pair: str, level_index: int, side: str, price: object,
                   quantity: object, status: str, filled_quantity: object = 0, raw: dict[str, Any] | None = None) -> None:
        now = self._now()
        with self._lock:
            self.connection.execute("""INSERT INTO bot_orders(exchange_order_id,pair,level_index,side,price,quantity,status,filled_quantity,created_at,updated_at,raw_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(exchange_order_id) DO UPDATE SET status=excluded.status, filled_quantity=excluded.filled_quantity, updated_at=excluded.updated_at, raw_json=excluded.raw_json""",
            (exchange_order_id, pair, level_index, side, str(price), str(quantity), status, str(filled_quantity), now, now, json.dumps(raw or {}, default=str)))
            self.connection.commit()

    def known_open_at_level(self, pair: str, level_index: int, side: str) -> bool:
        with self._lock:
            row = self.connection.execute("SELECT 1 FROM bot_orders WHERE pair=? AND level_index=? AND side=? AND status IN ('open','partially_filled')",
                                          (pair, level_index, side)).fetchone()
        return row is not None

    def open_order_at_level(self, pair: str, level_index: int, side: str) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute("SELECT * FROM bot_orders WHERE pair=? AND level_index=? AND side=? AND status IN ('open','partially_filled') ORDER BY updated_at DESC LIMIT 1",
                                           (pair, level_index, side)).fetchone()

    def order(self, exchange_order_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute("SELECT * FROM bot_orders WHERE exchange_order_id=?", (exchange_order_id,)).fetchone()

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def orders(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM bot_orders ORDER BY updated_at DESC")]

    def snapshot(self, kind: str, payload: Any) -> None:
        with self._lock:
            self.connection.execute("INSERT INTO snapshots(created_at,kind,payload_json) VALUES(?,?,?)", (self._now(), kind, json.dumps(payload, default=str)))
            self.connection.commit()

    def save_cycle(self, *, close_order_id: str, pair: str, entry_order_id: str | None, entry_side: str,
                   entry_price: object, close_price: object, quantity: object) -> None:
        with self._lock:
            self.connection.execute("""INSERT INTO grid_cycles(close_order_id,pair,entry_order_id,entry_side,entry_price,close_price,quantity,created_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(close_order_id) DO NOTHING""",
            (close_order_id, pair, entry_order_id, entry_side, str(entry_price), str(close_price), str(quantity), self._now()))
            self.connection.commit()

    def cycle_for_close_order(self, close_order_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute("SELECT * FROM grid_cycles WHERE close_order_id=?", (close_order_id,)).fetchone()

    def complete_cycle(self, close_order_id: str, realized_profit_inr: object) -> bool:
        with self._lock:
            cursor = self.connection.execute("""UPDATE grid_cycles SET status='completed', realized_profit_inr=?, completed_at=?
            WHERE close_order_id=? AND status='open'""", (str(realized_profit_inr), self._now(), close_order_id))
            self.connection.commit()
            return cursor.rowcount == 1

    def cycle_summary(self, pair: str) -> dict[str, str | int]:
        with self._lock:
            row = self.connection.execute("""SELECT COUNT(*) AS completed, COALESCE(SUM(CAST(realized_profit_inr AS REAL)), 0) AS profit
            FROM grid_cycles WHERE pair=? AND status='completed'""", (pair,)).fetchone()
            return {"completed": int(row["completed"]), "realized_profit_inr": str(row["profit"])}

    def cycle_summary_since(self, pair: str, since_iso: str) -> dict[str, str | int]:
        """Completed grid cycles for `pair` finished at/after `since_iso` (scopes profit to one bot run)."""
        with self._lock:
            row = self.connection.execute("""SELECT COUNT(*) AS completed, COALESCE(SUM(CAST(realized_profit_inr AS REAL)), 0) AS profit
            FROM grid_cycles WHERE pair=? AND status='completed' AND completed_at>=?""", (pair, since_iso)).fetchone()
            return {"completed": int(row["completed"]), "realized_profit_inr": str(row["profit"])}

    def close(self) -> None:
        with self._lock:
            self.connection.close()
