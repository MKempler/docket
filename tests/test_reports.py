from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import demo, reports  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.store import Store  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(sqlite3.connect(":memory:", check_same_thread=False))
    demo.seed(s, BOOK, tmp_path, now=NOW)
    return s


def test_followups_lists_only_stale_open_quotes(store):
    r = reports.followups(store, stale_days=5, now=NOW)
    assert r["quotes"] == ["Q-1001"]                           # Ray: sent 5+ days ago; Kim viewed 2-3 days ago
    assert "Suggested nudge" in r["text"] and "Hi Ray" in r["text"] and "Nothing was sent" in r["text"]
    r3 = reports.followups(store, stale_days=2, now=NOW)
    assert r3["quotes"] == ["Q-1001", "Q-1002"]
    assert reports.followups(store, stale_days=30, now=NOW)["count"] == 0


def test_run_sheet_lists_jobs_and_truck_load(store):
    r = reports.run_sheet(store, day=date(2026, 11, 8))
    assert r["count"] == 1 and "Pat Jones" in r["text"] and "5 Cedar Ct" in r["text"]
    assert reports.run_sheet(store, day=date(2026, 11, 9))["count"] == 0


def test_pipeline_numbers(store):
    r = reports.pipeline(store, now=NOW)
    assert r["open"] == 2                                       # sent + viewed
    assert r["win_rate"] == "75%"                               # 3 won (accepted, scheduled, done), 1 declined
