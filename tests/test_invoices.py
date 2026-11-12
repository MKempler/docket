from __future__ import annotations

import io
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import service as svc  # noqa: E402
from docket.intake import parse_words  # noqa: E402
from docket.invoices import InvoiceError, close_out, get_invoice, mark_paid, obol_payload  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.render import render_invoice_pdf  # noqa: E402
from docket.store import Store  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")
DAY = date(2026, 11, 9)


def booked(store, tmp_path, words="Dana Henderson dana@example.com, 14 Maple St 62707, water heater, permit, haul away"):
    d = svc.draft_from_words(store, BOOK, words)
    svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    for s in ("accepted", "scheduled"):
        store.transition(d.record.id, s, actor="test")
    return d.record.id


@pytest.fixture
def store():
    return Store(sqlite3.connect(":memory:", check_same_thread=False))


def test_close_out_makes_one_invoice_and_pdf(store, tmp_path):
    qid = booked(store, tmp_path)
    inv = close_out(store, BOOK, qid, outbox_root=tmp_path, today=DAY, render=render_invoice_pdf)
    assert inv["number"] == "INV-1001" and inv["total"] == "1972.36" and inv["due"] == "2026-11-23"
    assert inv["handoff"] == "local" and Path(inv["pdf"]).exists()
    assert store.get(qid).status == "invoiced"
    steps = [e["to_status"] for e in reversed(store.events(qid))][-3:]
    assert steps == ["in_progress", "done", "invoiced"]
    again = close_out(store, BOOK, qid, outbox_root=tmp_path, today=DAY)
    assert again["already"] and again["number"] == "INV-1001"
    from pypdf import PdfReader
    text = PdfReader(io.BytesIO(Path(inv["pdf"]).read_bytes())).pages[0].extract_text()
    assert "INVOICE" in text and "INV-1001" in text and "Amount due: $1,972.36" in text and "dana@example.com" in text
    assert mark_paid(store, qid)["status"] == "paid" and store.get(qid).status == "paid"


def test_extra_work_is_priced_by_the_engine(store, tmp_path):
    qid = booked(store, tmp_path)
    inv = close_out(store, BOOK, qid, outbox_root=tmp_path, extra_words="plus 1 hour labor", today=DAY)
    assert Decimal(inv["total"]) == Decimal("1972.36") + Decimal("135.00")          # labor isn't taxed
    assert "plus 1 hour labor" in store.events(qid)[0]["detail"]


def test_unpriced_extra_blocks_the_invoice(store, tmp_path):
    qid = booked(store, tmp_path)
    with pytest.raises(InvoiceError, match="needs a price"):
        close_out(store, BOOK, qid, outbox_root=tmp_path, extra_words="also re-tiled the floor", today=DAY)
    assert get_invoice(store, qid) is None


def test_only_finished_or_booked_jobs(store, tmp_path):
    d = svc.draft_from_words(store, BOOK, "Ray Ortiz, 62701, clogged drain")
    with pytest.raises(InvoiceError, match="draft"):
        close_out(store, BOOK, d.record.id, outbox_root=tmp_path)


def test_obol_draft_is_prepared_never_sent(store, tmp_path):
    """With Obol installed the invoice comes back with obol_payload for the agent; Docket itself calls nothing."""
    qid = booked(store, tmp_path)
    inv = close_out(store, BOOK, qid, outbox_root=tmp_path, today=DAY, obol=True, render=render_invoice_pdf)
    assert inv["handoff"] == "obol_ready" and inv["ref"] == "" and Path(inv["pdf"]).exists()   # local PDF still made
    p = inv["obol_payload"]
    assert p == obol_payload(get_invoice(store, qid))
    assert p["send"] is False and p["customer_email"] == "dana@example.com" and p["currency"] == "usd"
    assert all(item["quantity"] == 1 for item in p["line_items"])
    assert round(sum(Decimal(str(i["amount"])) for i in p["line_items"]), 2) == Decimal(inv["total"])   # adds up exactly
    again = close_out(store, BOOK, qid, outbox_root=tmp_path, today=DAY, obol=True)   # "send INV-1001 to Obol" later
    assert again["already"] and again["number"] == inv["number"] and again["obol_payload"] == p


def test_obol_needs_an_email_and_an_installed_plugin(store, tmp_path):
    q2 = booked(store, tmp_path, "Kim Lee, 62711, 2 x TOILET-RESET")                  # no email on file
    inv2 = close_out(store, BOOK, q2, outbox_root=tmp_path, today=DAY, obol=True)
    assert inv2["handoff"] == "local" and "email" in inv2["note"] and "obol_payload" not in inv2
    q3 = booked(store, tmp_path)
    inv3 = close_out(store, BOOK, q3, outbox_root=tmp_path, today=DAY)                # Obol not installed
    assert inv3["handoff"] == "local" and "obol_payload" not in inv3 and inv3["note"] == ""


def test_email_is_picked_up_from_the_words():
    r = parse_words(BOOK, "Dana Henderson dana.h@example.com, 62707, water heater")
    assert r.email == "dana.h@example.com" and r.customer_name == "Dana Henderson"


def test_parser_reads_stated_hours_as_labor():
    assert {i["code"]: i["qty"] for i in parse_words(BOOK, "plus 1.5 hours labor").args["items"]} == {"LABOR": "1.5"}
