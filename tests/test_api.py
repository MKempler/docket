from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from docket import demo, runtime  # noqa: E402
from docket.dashboard import plugin_api  # noqa: E402
from docket.store import Store  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    runtime.reset_for_tests(store)
    monkeypatch.setattr(runtime, "outbox_root", lambda: tmp_path)
    monkeypatch.setattr(runtime, "ensure_accept_server", lambda: "http://127.0.0.1:8787")
    demo.seed(store, runtime.require_book(), tmp_path)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/docket")
    yield TestClient(app), store
    runtime.reset_for_tests(None)


def test_board_groups_quotes_into_columns(client):
    c, _ = client
    b = c.get("/api/plugins/docket/board").json()
    counts = {col["key"]: col["count"] for col in b["columns"]}
    assert counts == {"draft": 0, "sent": 2, "accepted": 1, "scheduled": 1, "in_progress": 0, "done": 1}
    assert b["open_count"] == 2 and b["open_value"] == "990.49"          # Ray 561.85 + Kim 428.64
    sent = next(col for col in b["columns"] if col["key"] == "sent")
    assert any(item["opened"] for item in sent["items"])                  # Kim viewed it
    assert "closed" not in b and len(c.get("/api/plugins/docket/board?closed=true").json()["closed"]) == 1


def test_quote_detail_and_actions(client):
    c, store = client
    accepted = next(q for q in store.list() if q.status == "accepted")
    d = c.get(f"/api/plugins/docket/quote/{accepted.id}").json()
    assert [a["id"] for a in d["actions"]] == ["book"] and d["accept_url"].startswith("http://127.0.0.1:8787/q/")
    r = c.post(f"/api/plugins/docket/quote/{accepted.id}/book", json={"date": "2026-11-10"})
    assert r.status_code == 200 and store.get(accepted.id).scheduled_for == "2026-11-10"
    for step in ("in_progress", "done"):
        assert c.post(f"/api/plugins/docket/quote/{accepted.id}/{step}", json={}).status_code == 200
    assert store.get(accepted.id).status == "done"


def test_illegal_action_is_a_409_not_a_crash(client):
    c, store = client
    done = next(q for q in store.list() if q.status == "done")
    assert c.post(f"/api/plugins/docket/quote/{done.id}/send", json={}).json()["ok"]    # already sent: no-op
    r = c.post(f"/api/plugins/docket/quote/{done.id}/discard", json={})               # done -> discarded: illegal
    assert r.status_code == 409 and "cannot go from done" in r.json()["detail"]
    assert c.get("/api/plugins/docket/quote/99999").status_code == 404


def test_changes_notify_listeners(client):
    c, store = client
    seen = []
    store.listeners.append(lambda qid, status: seen.append(status))
    q = next(q for q in store.list() if q.status == "accepted")
    c.post(f"/api/plugins/docket/quote/{q.id}/book", json={"date": "2026-11-10"})
    assert seen == ["scheduled"]
