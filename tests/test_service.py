from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import service as svc  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.store import Store  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")
WORDS = "Dana Henderson, 14 Maple St 62707, swap the 50 gallon gas water heater, permit and haul away"


@pytest.fixture
def store():
    return Store(sqlite3.connect(":memory:", check_same_thread=False))


def test_words_to_sent_to_accepted(store, tmp_path):
    d = svc.draft_from_words(store, BOOK, WORDS)
    assert d.record.number == "Q-1001" and d.record.total == "1972.36"
    sent = svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    assert sent["status"] == "sent" and not sent["already_sent"]
    outbox = Path(sent["outbox"])
    assert (outbox / "message.eml").exists() and "Total $1,972.36" in (outbox / "message.txt").read_text()
    token = sent["accept_url"].rsplit("/", 1)[1]
    assert svc.customer_action(store, token, "view").status == "viewed"
    assert svc.customer_action(store, token, "accept").status == "accepted"
    assert svc.customer_action(store, token, "accept").status == "accepted"     # double click is harmless
    assert "Accepted (1, $1,972.36)" in svc.jobs_text(store)


def test_double_send_is_a_no_op(store, tmp_path):
    d = svc.draft_from_words(store, BOOK, WORDS)
    first = svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    again = svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    assert again["already_sent"] and again["accept_url"] == first["accept_url"]
    assert len(list(tmp_path.iterdir())) == 1


def test_revision_voids_old_link(store, tmp_path):
    d = svc.draft_from_words(store, BOOK, WORDS)
    old = svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    r = svc.revise_from_words(store, BOOK, d.record.id, "add a shutoff valve, saturday")
    assert r.record.version == 2 and r.record.status == "draft"
    assert {"WH50-REPLACE", "SHUTOFF-REPLACE"} <= {i["code"] for i in r.record.spec["items"]}
    assert r.quote.multiplier_flag == "weekend"
    with pytest.raises(svc.ServiceError, match="updated"):
        svc.customer_action(store, old["accept_url"].rsplit("/", 1)[1], "accept")
    new = svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    assert svc.customer_action(store, new["accept_url"].rsplit("/", 1)[1], "accept").status == "accepted"


def test_needs_price_blocks_send_and_records_nothing(store, tmp_path):
    d = svc.draft_from_words(store, BOOK, "Pat Jones, 62701, install a tankless heater in the attic, clogged drain")
    with pytest.raises(svc.ServiceError, match="need a price"):
        svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    assert not store.has_send(d.record.id, 1, "outbox") and list(tmp_path.iterdir()) == []
    assert store.get(d.record.id).status == "draft"


def test_failed_outbox_write_does_not_mark_sent(store, tmp_path, monkeypatch):
    import docket.outbox as ob
    d = svc.draft_from_words(store, BOOK, WORDS)
    monkeypatch.setattr(ob, "write_outbox", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    assert not store.has_send(d.record.id, 1, "outbox") and store.get(d.record.id).status == "draft"


def test_nothing_recognised_is_an_error(store):
    with pytest.raises(svc.ServiceError):
        svc.draft_from_words(store, BOOK, "")


def test_parse_quote_id():
    assert svc.parse_quote_id("Q-1042") == svc.parse_quote_id("q1042") == svc.parse_quote_id("1042") == 1042
    with pytest.raises(svc.ServiceError):
        svc.parse_quote_id("henderson")


def test_no_priced_work_means_no_quote(store):
    with pytest.raises(svc.ServiceError, match="no quote was made"):
        svc.draft_from_words(store, BOOK, "Nate Cole 62702 wants a tankless water heater installed")
    assert store.list() == []


def test_same_job_twice_in_one_chat_reuses_the_draft(store):
    a = svc.draft_from_words(store, BOOK, WORDS, dedupe_key="s1:x")
    b = svc.draft_from_words(store, BOOK, WORDS, dedupe_key="s1:x")
    c = svc.draft_from_words(store, BOOK, WORDS, dedupe_key="s2:x")
    assert a.record.id == b.record.id != c.record.id and len(store.list()) == 2
