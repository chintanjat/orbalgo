from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Position, TradingViewEvent


class Store:
    def __init__(self, path: Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    underlying TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    entry_event_id TEXT NOT NULL UNIQUE,
                    side TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    strike REAL NOT NULL,
                    security_id TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    lot_size INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    option_stop_price REAL NOT NULL,
                    status TEXT NOT NULL,
                    entry_order_id TEXT NOT NULL DEFAULT '',
                    exit_order_id TEXT NOT NULL DEFAULT '',
                    exit_price REAL,
                    exit_reason TEXT NOT NULL DEFAULT '',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    last_option_price REAL,
                    spot_entry REAL,
                    spot_stop REAL,
                    spot_target REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_underlying
                ON positions(underlying) WHERE status='OPEN';
                """
            )

    def claim_event(self, event: TradingViewEvent) -> bool:
        payload = event.model_dump_json(by_alias=True)
        try:
            with self._lock, self._connect() as c:
                c.execute(
                    "INSERT INTO events(event_id,event_type,underlying,payload,created_at) VALUES(?,?,?,?,?)",
                    (event.event_id, event.event.value, event.underlying, payload, datetime.now(timezone.utc).isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_event_outcome(self, event_id: str, outcome: dict):
        with self._lock, self._connect() as c:
            c.execute("UPDATE events SET outcome=? WHERE event_id=?", (json.dumps(outcome, default=str), event_id))

    def get_open_position(self, underlying: str) -> Position | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE underlying=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (underlying,)
            ).fetchone()
        return self._row_to_position(row) if row else None

    def list_open_positions(self) -> list[Position]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY id").fetchall()
        return [self._row_to_position(r) for r in rows]

    def insert_position(self, p: Position) -> Position:
        data = p.model_dump()
        cols = [k for k in data.keys() if k != "id"]
        values = [self._serialize(data[k]) for k in cols]
        with self._lock, self._connect() as c:
            cur = c.execute(
                f"INSERT INTO positions({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                values,
            )
            pid = cur.lastrowid
        return p.model_copy(update={"id": pid})

    def close_position(self, position_id: int, *, exit_price: float, reason: str, exit_order_id: str = ""):
        with self._lock, self._connect() as c:
            c.execute(
                """UPDATE positions SET status='CLOSED', exit_price=?, exit_reason=?, exit_order_id=?, closed_at=?
                   WHERE id=? AND status='OPEN'""",
                (exit_price, reason, exit_order_id, datetime.now(timezone.utc).isoformat(), position_id),
            )

    def update_last_price(self, position_id: int, last_price: float):
        with self._lock, self._connect() as c:
            c.execute("UPDATE positions SET last_option_price=? WHERE id=?", (last_price, position_id))

    @staticmethod
    def _serialize(value):
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        d = dict(row)
        d["expiry"] = d["expiry"]
        d["opened_at"] = d["opened_at"]
        if d["closed_at"]:
            d["closed_at"] = d["closed_at"]
        return Position.model_validate(d)
