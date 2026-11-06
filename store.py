"""Docket ledger: customers, versioned quotes, the job state machine and an append-only event log.

SQLite via Hermes's plugin_db() in production; any sqlite3 connection in tests. Every status change
goes through transition(), which enforces the state machine and writes an event row, so the board
and the follow-up blueprint read one consistent history.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

FIRST_QUOTE_NUMBER = 1001

# Quote → job lifecycle. A revision moves a sent/viewed/declined/expired quote back to draft as a new version.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"sent", "discarded"},
    "sent": {"viewed", "accepted", "declined", "expired", "draft"},
    "viewed": {"accepted", "declined", "expired", "draft"},
    "declined": {"draft"},
    "expired": {"draft"},
    "accepted": {"scheduled", "cancelled"},
    "scheduled": {"scheduled", "in_progress", "cancelled"},
    "in_progress": {"done"},
    "done": {"invoiced"},
    "invoiced": {"paid"},
    "discarded": set(),
    "cancelled": set(),
    "paid": set(),
}
OPEN_QUOTE = {"draft", "sent", "viewed"}
ACCEPTABLE = {"sent", "viewed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT DEFAULT '', email TEXT DEFAULT '',
  address TEXT DEFAULT '', zip TEXT DEFAULT '', tier TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS customers_name ON customers (lower(name));
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  site TEXT DEFAULT '', zip TEXT DEFAULT '', status TEXT NOT NULL, version INTEGER NOT NULL,
  scheduled_for TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quote_versions (
  quote_id INTEGER NOT NULL REFERENCES quotes(id), version INTEGER NOT NULL,
  spec_json TEXT NOT NULL, quote_json TEXT NOT NULL, total TEXT NOT NULL, complete INTEGER NOT NULL,
  note TEXT DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY (quote_id, version));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, quote_id INTEGER NOT NULL, version INTEGER NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, surface TEXT DEFAULT '',
  detail TEXT DEFAULT '', at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_quote ON events (quote_id, id);
CREATE TABLE IF NOT EXISTS sends (
  send_key TEXT PRIMARY KEY, quote_id INTEGER NOT NULL, version INTEGER NOT NULL,
  channel TEXT NOT NULL, ref TEXT DEFAULT '', at TEXT NOT NULL);
"""


class StoreError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class QuoteRecord:
    id: int
    customer: dict[str, Any]
    site: str
    zip: str
    status: str
    version: int
    scheduled_for: str
    created_at: str
    updated_at: str
    spec: dict[str, Any]
    quote: dict[str, Any]
    total: str
    complete: bool

    @property
    def number(self) -> str:
        return f"Q-{self.id}"


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lock = threading.RLock()
        self.listeners: list = []          # callables(quote_id, to_status), called after commit
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)

    # ---- meta / settings -------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def link_secret(self) -> bytes:
        with self.lock:
            value = self.get_meta("link_secret")
            if value is None:
                value = secrets.token_hex(32)
                self.set_meta("link_secret", value)
            return bytes.fromhex(value)

    # ---- customers ---------------------------------------------------------
    def upsert_customer(self, name: str, **fields: str) -> dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            raise StoreError("customer name is required")
        allowed = {k: str(v).strip() for k, v in fields.items() if k in {"phone", "email", "address", "zip", "tier"} and v}
        with self.lock, self.conn:
            row = self.conn.execute("SELECT id FROM customers WHERE lower(name) = lower(?)", (clean,)).fetchone()
            if row is None:
                cur = self.conn.execute(
                    "INSERT INTO customers (name, phone, email, address, zip, tier, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (clean, allowed.get("phone", ""), allowed.get("email", ""), allowed.get("address", ""),
                     allowed.get("zip", ""), allowed.get("tier", ""), now_iso()))
                cid = cur.lastrowid
            else:
                cid = row[0]
                for k, v in allowed.items():
                    self.conn.execute(f"UPDATE customers SET {k} = ? WHERE id = ?", (v, cid))
        return self.customer(cid)

    def customer(self, cid: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT id, name, phone, email, address, zip, tier FROM customers WHERE id = ?",
                                (cid,)).fetchone()
        if row is None:
            raise StoreError(f"no customer {cid}")
        return dict(zip(("id", "name", "phone", "email", "address", "zip", "tier"), row))

    def find_customer(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT id FROM customers WHERE lower(name) = lower(?)", ((name or "").strip(),)).fetchone()
        return self.customer(row[0]) if row else None

    # ---- quotes ------------------------------------------------------------
    def create_quote(self, customer_id: int, site: str, zip_code: str, spec: dict, quote: dict, *,
                     actor: str, surface: str = "") -> QuoteRecord:
        ts = now_iso()
        with self.lock, self.conn:
            next_id = (self.conn.execute("SELECT MAX(id) FROM quotes").fetchone()[0] or FIRST_QUOTE_NUMBER - 1) + 1
            self.conn.execute(
                "INSERT INTO quotes (id, customer_id, site, zip, status, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'draft', 1, ?, ?)", (next_id, customer_id, site, zip_code, ts, ts))
            self._insert_version(next_id, 1, spec, quote, "", ts)
            self._event(next_id, 1, None, "draft", actor, surface, "created", ts)
        self._changed(next_id, "draft")
        return self.get(next_id)

    def revise(self, quote_id: int, spec: dict, quote: dict, *, note: str, actor: str, surface: str = "") -> QuoteRecord:
        with self.lock, self.conn:
            rec = self.get(quote_id)
            if rec.status not in OPEN_QUOTE | {"declined", "expired"}:
                raise StoreError(f"{rec.number} is {rec.status} and can no longer be revised")
            version = rec.version + 1
            ts = now_iso()
            self._insert_version(quote_id, version, spec, quote, note, ts)
            self.conn.execute("UPDATE quotes SET version = ?, status = 'draft', updated_at = ? WHERE id = ?",
                              (version, ts, quote_id))
            self._event(quote_id, version, rec.status, "draft", actor, surface, f"revised to v{version}: {note}", ts)
        self._changed(quote_id, "draft")
        return self.get(quote_id)

    def transition(self, quote_id: int, to_status: str, *, actor: str, surface: str = "", detail: str = "",
                   expect_version: int | None = None, scheduled_for: str | None = None) -> QuoteRecord:
        with self.lock, self.conn:
            rec = self.get(quote_id)
            if expect_version is not None and expect_version != rec.version:
                raise StoreError(f"{rec.number} v{expect_version} was replaced by v{rec.version}")
            if to_status not in TRANSITIONS.get(rec.status, set()):
                raise StoreError(f"{rec.number} cannot go from {rec.status} to {to_status}")
            if to_status == "sent" and not rec.complete:
                raise StoreError(f"{rec.number} has lines that need a price; it cannot be sent")
            ts = now_iso()
            if scheduled_for is not None:
                self.conn.execute("UPDATE quotes SET scheduled_for = ? WHERE id = ?", (scheduled_for, quote_id))
            self.conn.execute("UPDATE quotes SET status = ?, updated_at = ? WHERE id = ?", (to_status, ts, quote_id))
            self._event(quote_id, rec.version, rec.status, to_status, actor, surface, detail, ts)
        self._changed(quote_id, to_status)
        return self.get(quote_id)

    def record_send(self, quote_id: int, version: int, channel: str, ref: str = "") -> bool:
        """Idempotency: True the first time a (quote, version, channel) is sent, False on repeats."""
        key = f"{quote_id}:{version}:{channel}"
        with self.lock, self.conn:
            try:
                self.conn.execute("INSERT INTO sends (send_key, quote_id, version, channel, ref, at) VALUES (?, ?, ?, ?, ?, ?)",
                                  (key, quote_id, version, channel, ref, now_iso()))
                return True
            except sqlite3.IntegrityError:
                return False

    def has_send(self, quote_id: int, version: int, channel: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sends WHERE send_key = ?",
                                 (f"{quote_id}:{version}:{channel}",)).fetchone() is not None

    def get(self, quote_id: int) -> QuoteRecord:
        row = self.conn.execute(
            "SELECT q.id, q.customer_id, q.site, q.zip, q.status, q.version, q.scheduled_for, q.created_at, q.updated_at, "
            "v.spec_json, v.quote_json, v.total, v.complete FROM quotes q "
            "JOIN quote_versions v ON v.quote_id = q.id AND v.version = q.version WHERE q.id = ?", (quote_id,)).fetchone()
        if row is None:
            raise StoreError(f"no quote Q-{quote_id}")
        return QuoteRecord(id=row[0], customer=self.customer(row[1]), site=row[2], zip=row[3], status=row[4], version=row[5],
                           scheduled_for=row[6], created_at=row[7], updated_at=row[8], spec=json.loads(row[9]),
                           quote=json.loads(row[10]), total=row[11], complete=bool(row[12]))

    def version(self, quote_id: int, version: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT spec_json, quote_json, total, complete, note, created_at FROM quote_versions "
                                "WHERE quote_id = ? AND version = ?", (quote_id, version)).fetchone()
        if row is None:
            raise StoreError(f"no Q-{quote_id} v{version}")
        return {"spec": json.loads(row[0]), "quote": json.loads(row[1]), "total": row[2], "complete": bool(row[3]),
                "note": row[4], "created_at": row[5]}

    def list(self, statuses: set[str] | None = None, customer: str = "") -> list[QuoteRecord]:
        ids = [r[0] for r in self.conn.execute("SELECT id FROM quotes ORDER BY id DESC").fetchall()]
        out = [self.get(i) for i in ids]
        if statuses:
            out = [q for q in out if q.status in statuses]
        if customer:
            out = [q for q in out if q.customer["name"].lower() == customer.strip().lower()]
        return out

    def events(self, quote_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT id, quote_id, version, from_status, to_status, actor, surface, detail, at FROM events"
        params: tuple = ()
        if quote_id is not None:
            sql += " WHERE quote_id = ?"
            params = (quote_id,)
        rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", params + (limit,)).fetchall()
        keys = ("id", "quote_id", "version", "from_status", "to_status", "actor", "surface", "detail", "at")
        return [dict(zip(keys, r)) for r in rows]

    def last_status_change(self, quote_id: int) -> str:
        row = self.conn.execute("SELECT at FROM events WHERE quote_id = ? ORDER BY id DESC LIMIT 1", (quote_id,)).fetchone()
        return row[0] if row else ""

    # ---- internals ---------------------------------------------------------
    def _changed(self, quote_id: int, status: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(quote_id, status)
            except Exception:
                pass

    def _insert_version(self, quote_id: int, version: int, spec: dict, quote: dict, note: str, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO quote_versions (quote_id, version, spec_json, quote_json, total, complete, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (quote_id, version, json.dumps(spec, sort_keys=True), json.dumps(quote, sort_keys=True),
             str(quote.get("total", "0")), 1 if quote.get("complete") else 0, note, ts))

    def _event(self, quote_id: int, version: int, from_status: str | None, to_status: str, actor: str,
               surface: str, detail: str, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO events (quote_id, version, from_status, to_status, actor, surface, detail, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (quote_id, version, from_status, to_status, actor, surface, detail, ts))


def open_store() -> Store:
    """The production store, in Hermes's per-plugin data directory (survives updates, follows the profile)."""
    from plugins.plugin_storage import plugin_db
    return Store(plugin_db("docket"))
