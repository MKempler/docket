from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import demo  # noqa: E402
from docket import service as svc  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.store import Store  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    return Store(sqlite3.connect(":memory:", check_same_thread=False))


def test_seed_covers_every_state_with_complete_quotes(store, tmp_path):
    made = demo.seed(store, BOOK, tmp_path, now=NOW)
    assert len(made) == 6 and made[3] == "Q-1004 Pat Jones: scheduled"
    by_status = {q.status: q for q in store.list()}
    assert set(by_status) == {"sent", "viewed", "accepted", "scheduled", "declined", "done"}
    assert all(q.complete for q in store.list())                       # every seed parses fully
    assert by_status["scheduled"].scheduled_for == "2026-11-09"            # Sun Nov 8 moves to Monday
    assert by_status["sent"].customer["name"] == "Ray Ortiz"
    assert by_status["sent"].created_at.startswith("2026-10-31")      # drafted 6 days ago
    assert by_status["sent"].updated_at.startswith("2026-11-01")      # sent the next day: 5+ days unsigned


def test_events_keep_order_after_backdating(store, tmp_path):
    demo.seed(store, BOOK, tmp_path, now=NOW)
    done = next(q for q in store.list() if q.status == "done")
    ev = list(reversed(store.events(done.id)))
    assert [e["to_status"] for e in ev] == ["draft", "sent", "viewed", "accepted", "scheduled", "in_progress", "done"]
    assert [e["at"] for e in ev] == sorted(e["at"] for e in ev)


def test_refuses_to_overwrite_real_data_without_reset(store, tmp_path):
    svc.draft_from_words(store, BOOK, "Real Customer, 62701, water heater")
    with pytest.raises(demo.DemoError):
        demo.seed(store, BOOK, tmp_path, now=NOW)
    demo.seed(store, BOOK, tmp_path, now=NOW, force=True)
    assert "Real Customer" not in {q.customer["name"] for q in store.list()}
    assert store.list()[-1].number == "Q-1001"                         # numbering restarts after reset


def test_no_event_is_in_the_future_and_no_sunday_bookings(store, tmp_path):
    demo.seed(store, BOOK, tmp_path, now=NOW)
    assert all(e["at"] <= NOW.isoformat() for e in store.events(limit=500))
    from datetime import date
    assert all(date.fromisoformat(q.scheduled_for).weekday() != 6 for q in store.list() if q.scheduled_for)
