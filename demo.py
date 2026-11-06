"""Demo mode: seed the fictional Copperline Plumbing & Drain with quotes in every state.

Same code paths as real use (intake parser -> pricing -> ledger -> state machine); only the timestamps are
moved into the past so the job board and the follow-up automation have history to show.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .pricebook import PriceBook
from .service import draft_from_words, send_quote
from .store import Store

# (words the owner might type, target status, days ago, scheduled for)
SEEDS = [
    ("Ray Ortiz, 9 Elm Dr 62701, repipe 38 ft, member", "sent", 6, ""),
    ("Kim Lee, 31 Birch Ln 62711, 2 x TOILET-RESET", "viewed", 4, ""),
    ("Mike O'Brien, 220 Oak Ave 62712, kitchen faucet and a new disposal, after hours", "accepted", 2, ""),
    ("Pat Jones, 5 Cedar Ct 62629, clogged drain, camera inspection", "scheduled", 3, "+2"),
    ("Grace Liu, 77 Pine Rd 62703, shutoff valve, emergency", "declined", 8, ""),
    ("Sam Patel, 410 Willow Way 62704, replace toilet, haul away", "done", 12, "-5"),
]
PATH_TO = {
    "sent": ["sent"], "viewed": ["sent", "viewed"], "accepted": ["sent", "viewed", "accepted"],
    "scheduled": ["sent", "viewed", "accepted", "scheduled"], "declined": ["sent", "viewed", "declined"],
    "done": ["sent", "viewed", "accepted", "scheduled", "in_progress", "done"],
}
TABLES = ("events", "sends", "quote_versions", "quotes", "customers")


class DemoError(RuntimeError):
    pass


def has_data(store: Store) -> bool:
    return bool(store.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])


def reset(store: Store) -> None:
    with store.lock, store.conn:
        for table in TABLES:
            store.conn.execute(f"DELETE FROM {table}")
        store.conn.execute("DELETE FROM meta WHERE key IN ('demo_seeded')")


def seed(store: Store, book: PriceBook, outbox_root: Path, *, force: bool = False, now: datetime | None = None) -> list[str]:
    if has_data(store) and not force:
        raise DemoError("There are already quotes here. Run with --reset to wipe them and load the demo.")
    if force:
        reset(store)
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    made = []
    for words, target, days_ago, sched in SEEDS:
        d = draft_from_words(store, book, words, actor="owner", surface="demo")
        rec = d.record
        for step in PATH_TO[target]:
            if step == "sent":
                send_quote(store, book, rec.id, outbox_root=outbox_root, actor="owner", surface="demo")
                continue
            when = ""
            if step == "scheduled" and sched:
                booked = (now + timedelta(days=int(sched))).date()
                while booked.weekday() == 6:           # no Sunday bookings, same rule as the booking buttons
                    booked += timedelta(days=1 if int(sched) > 0 else -1)
                when = booked.isoformat()
            actor = "customer" if step in {"viewed", "accepted", "declined"} else "owner"
            store.transition(rec.id, step, actor=actor, surface="demo",
                             scheduled_for=when if step == "scheduled" else None)
        _backdate(store, rec.id, now - timedelta(days=days_ago), now)
        made.append(f"{rec.number} {store.get(rec.id).customer['name']}: {target}")
    store.set_meta("demo_seeded", now.isoformat())
    return made


def _backdate(store: Store, quote_id: int, start: datetime, now: datetime) -> None:
    """Spread this quote's events from `start` towards `now`, in order, never past `now` (at most ~20 h apart)."""
    with store.lock, store.conn:
        ids = [r[0] for r in store.conn.execute("SELECT id FROM events WHERE quote_id = ? ORDER BY id", (quote_id,))]
        span = (now - start) * 0.8                    # leave the newest step comfortably in the past
        step = min(timedelta(hours=20), span / max(len(ids) - 1, 1))
        for i, eid in enumerate(ids):
            ts = (start + step * i).isoformat()
            store.conn.execute("UPDATE events SET at = ? WHERE id = ?", (ts, eid))
        first = (start).isoformat()
        last = (start + step * max(len(ids) - 1, 0)).isoformat()
        store.conn.execute("UPDATE quotes SET created_at = ?, updated_at = ? WHERE id = ?", (first, last, quote_id))
        store.conn.execute("UPDATE quote_versions SET created_at = ? WHERE quote_id = ?", (first, quote_id))
