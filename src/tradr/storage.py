from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Intent, Order, dec


class Journal:
    def __init__(self, directory: str, mode: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / f"{mode}.sqlite", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, intent TEXT NOT NULL, state TEXT NOT NULL,
                filled TEXT NOT NULL DEFAULT '0', average TEXT NOT NULL DEFAULT '0',
                server_id TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (time REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
        """)
        self.db.commit()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, default=str)))
        self.db.commit()

    def event(self, kind: str, detail):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), kind, json.dumps(detail, default=str)))
        self.db.commit()

    def next_id(self) -> str:
        # uint48 for Lighter; same durable ID space for both venues, never reused.
        value = max(int(time.time() * 1000) * 10, self.get("last_id", 0) + 1)
        if value >= 2**48:
            raise RuntimeError("Client ID space exhausted")
        self.put("last_id", value)
        return str(value)

    def timestamp_ns(self) -> int:
        value = max(time.time_ns(), self.get("last_timestamp_ns", 0) + 1)
        self.put("last_timestamp_ns", value)
        return value

    def prepare(self, intent: Intent):
        # Write-ahead record precedes any network send, even when the process dies during send.
        self.db.execute("INSERT INTO orders(id,intent,state,updated) VALUES (?,?,?,?)",
                        (intent.id, json.dumps(asdict(intent), default=str), "prepared", time.time()))
        self.db.commit()

    def update(self, order: Order):
        row = self.db.execute("SELECT filled FROM orders WHERE id=?", (order.id,)).fetchone()
        if row and dec(row[0]) > order.filled:
            raise RuntimeError("Order fill quantity regressed")
        self.db.execute("UPDATE orders SET state=?,filled=?,average=?,server_id=COALESCE(?,server_id),"
                        "updated=? WHERE id=?", (order.state, str(order.filled), str(order.average),
                                                order.server_id, time.time(), order.id))
        self.db.commit()

    def rows(self):
        output = []
        for row in self.db.execute("SELECT id,intent,state,filled,average,server_id FROM orders ORDER BY updated"):
            values = json.loads(row[1])
            values["quantity"], values["price"] = dec(values["quantity"]), dec(values["price"])
            output.append((Intent(**values), Order(row[0], row[2], dec(row[3]), dec(row[4]), row[5])))
        return output

    def paper_commit(self, venue, state, order):
        """Paper cash/positions and fill acknowledgement are one crash-consistent transaction."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",
                            ("paper:" + venue, json.dumps(state, default=str)))
            self.db.execute("UPDATE orders SET state=?,filled=?,average=?,server_id=?,updated=? WHERE id=?",
                            (order.state, str(order.filled), str(order.average), order.server_id,
                             time.time(), order.id))

    def daily_loss(self, equity, cash_flow=None, now=None):
        day = (now or datetime.now(ZoneInfo("Europe/Istanbul"))).astimezone(
            ZoneInfo("Europe/Istanbul")).date().isoformat()
        key = f"day:{day}"
        baseline = self.get(key)
        if baseline is None:
            baseline = {"equity": str(equity), "cash_flow": str(cash_flow) if cash_flow is not None else None}
            self.put(key, baseline)
        delta = 0
        if cash_flow is not None and baseline["cash_flow"] is not None:
            delta = cash_flow - dec(baseline["cash_flow"])
        return max(dec(0), dec(baseline["equity"]) + delta - equity)

    def report(self):
        states = dict(self.db.execute("SELECT state,COUNT(*) FROM orders GROUP BY state"))
        return {"order_states": states, "hedge": self.get("hedge"), "health": self.get("health"),
                "recent_events": [{"time": t, "kind": k, "detail": json.loads(d)} for t, k, d in
                                  self.db.execute("SELECT time,kind,detail FROM events ORDER BY rowid DESC LIMIT 20")]}

    def close(self):
        self.db.close()
