from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket.links import LinkError, make_token, read_token  # noqa: E402
from docket.store import Store, StoreError  # noqa: E402

COMPLETE = {"total": "654.19", "complete": True}
INCOMPLETE = {"total": "89.00", "complete": False}


@pytest.fixture
def store():
    return Store(sqlite3.connect(":memory:", check_same_thread=False))


def new_quote(store, quote=COMPLETE):
    c = store.upsert_customer("Dana Henderson", zip="62701", phone="555-0100")
    return store.create_quote(c["id"], "14 Maple St", "62701", {"items": []}, quote, actor="owner", surface="cli")


def test_numbering_starts_at_1001_and_increments(store):
    a, b = new_quote(store), new_quote(store)
    assert (a.number, b.number) == ("Q-1001", "Q-1002")
    assert a.status == "draft" and a.version == 1


def test_customer_upsert_is_case_insensitive(store):
    a = store.upsert_customer("Dana Henderson")
    b = store.upsert_customer("dana henderson", email="dana@example.com")
    assert a["id"] == b["id"] and b["email"] == "dana@example.com"


def test_full_lifecycle_writes_events(store):
    q = new_quote(store)
    for s in ("sent", "viewed", "accepted", "scheduled", "in_progress", "done", "invoiced", "paid"):
        q = store.transition(q.id, s, actor="test")
    assert q.status == "paid"
    assert [e["to_status"] for e in reversed(store.events(q.id))] == [
        "draft", "sent", "viewed", "accepted", "scheduled", "in_progress", "done", "invoiced", "paid"]


def test_illegal_transitions_are_refused(store):
    q = new_quote(store)
    with pytest.raises(StoreError):
        store.transition(q.id, "accepted", actor="test")       # draft cannot jump to accepted
    store.transition(q.id, "discarded", actor="test")
    with pytest.raises(StoreError):
        store.transition(q.id, "sent", actor="test")           # discarded is terminal


def test_incomplete_quote_cannot_be_sent(store):
    q = new_quote(store, INCOMPLETE)
    with pytest.raises(StoreError, match="need a price"):
        store.transition(q.id, "sent", actor="test")


def test_revision_bumps_version_and_invalidates_old_version(store):
    q = new_quote(store)
    store.transition(q.id, "sent", actor="test")
    q2 = store.revise(q.id, {"items": ["x"]}, {"total": "700.00", "complete": True}, note="add 3 ft", actor="owner")
    assert (q2.version, q2.status, q2.total) == (2, "draft", "700.00")
    assert store.version(q.id, 1)["total"] == "654.19"
    with pytest.raises(StoreError, match="replaced"):
        store.transition(q.id, "accepted", actor="customer", expect_version=1)


def test_sends_are_idempotent(store):
    q = new_quote(store)
    assert store.record_send(q.id, 1, "outbox") is True
    assert store.record_send(q.id, 1, "outbox") is False
    assert store.record_send(q.id, 2, "outbox") is True


def test_link_tokens_roundtrip_and_reject_tampering_and_expiry(store):
    secret = store.link_secret()
    assert store.link_secret() == secret                      # stable across calls
    token = make_token(secret, 1042, 2, now=1_000_000)
    assert read_token(secret, token, now=1_000_100) == (1042, 2)
    body, sig = token.split(".")
    with pytest.raises(LinkError):
        read_token(secret, body + "." + sig[::-1], now=1_000_100)
    with pytest.raises(LinkError):
        read_token(b"other-secret", token, now=1_000_100)
    with pytest.raises(LinkError, match="expired"):
        read_token(secret, token, now=1_000_000 + 31 * 86400)
