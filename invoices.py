"""Close-out: a finished job becomes an invoice, priced by the same engine, kept as a local invoice PDF in the
outbox. When the owner's invoicing plugin (Obol) is installed, the invoice also comes back as a prepared Obol draft
(obol_payload) for the agent to create through obol_create_invoice, so Obol's own approval and checks run.

Docket never calls Obol itself and never takes payment.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .money import fmt_money, fmt_qty
from .pricebook import PriceBook
from .pricing import price, spec_from_args
from .store import Store, StoreError, now_iso

FIRST_INVOICE_NUMBER = 1001
DUE_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY, quote_id INTEGER NOT NULL UNIQUE, doc_json TEXT NOT NULL, total TEXT NOT NULL,
  issued TEXT NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL, handoff TEXT NOT NULL DEFAULT 'local',
  ref TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
"""


class InvoiceError(ValueError):
    pass


def _ensure(store: Store) -> None:
    with store.lock, store.conn:
        store.conn.executescript(SCHEMA)


def get_invoice(store: Store, quote_id: int) -> dict[str, Any] | None:
    _ensure(store)
    row = store.conn.execute("SELECT id, quote_id, doc_json, total, issued, due, status, handoff, ref FROM invoices "
                             "WHERE quote_id = ?", (quote_id,)).fetchone()
    if row is None:
        return None
    rec = store.get(quote_id)
    return {"id": row[0], "number": f"INV-{row[0]}", "quote_id": row[1], "quote_number": rec.number,
            "doc": json.loads(row[2]), "total": row[3], "issued": row[4], "due": row[5], "status": row[6],
            "handoff": row[7], "ref": row[8], "customer": rec.customer, "site": rec.site, "zip": rec.zip}


def close_out(store: Store, book: PriceBook, quote_id: int, *, outbox_root: Path, extra_words: str = "",
              llm: Any = None, obol: bool = False, actor: str = "owner",
              surface: str = "", today: date | None = None, render: Callable | None = None) -> dict[str, Any]:
    """Finish a job and make its invoice. Safe to repeat: a job is invoiced once.

    obol: the owner's invoicing plugin is installed. Nothing is sent to it from here; the result carries
    obol_payload (when the customer has an email) for the agent to pass to obol_create_invoice."""
    _ensure(store)
    existing = get_invoice(store, quote_id)
    if existing:
        return {**existing, "already": True, **_obol_fields(existing, obol)}
    rec = store.get(quote_id)
    if rec.status == "scheduled":
        rec = store.transition(quote_id, "in_progress", actor=actor, surface=surface)
    if rec.status == "in_progress":
        rec = store.transition(quote_id, "done", actor=actor, surface=surface)
    if rec.status != "done":
        raise InvoiceError(f"{rec.number} is {rec.status}; only a booked or finished job can be invoiced")

    spec = dict(rec.spec)
    extra_note = ""
    if extra_words.strip():
        from .intake import intake
        from .service import _merge
        added = intake(book, extra_words, llm)
        unused = [n.split("Not used: ", 1)[1] for n in added.notes if n.startswith("Not used: ")]
        if not added.args["items"] and not added.args["unpriced"]:
            raise InvoiceError(f"Couldn't price the extra work '{extra_words.strip()}'. Use price-book jobs or "
                               "'N hours labor'.")
        if added.args["unpriced"] or unused:
            raise InvoiceError("Some extra work needs a price before invoicing: "
                               + "; ".join(added.args["unpriced"] + unused))
        merged = _merge(spec, added)
        spec, extra_note = merged.args, extra_words.strip()
    doc = price(book, spec_from_args(spec))
    if not doc.complete:
        raise InvoiceError("Some work needs a price before invoicing: " + "; ".join(doc.needs_price))

    issued = today or date.today()
    from .settings import setting
    due = issued + timedelta(days=setting("invoice_due_days"))
    with store.lock, store.conn:
        next_id = (store.conn.execute("SELECT MAX(id) FROM invoices").fetchone()[0] or FIRST_INVOICE_NUMBER - 1) + 1
        store.conn.execute(
            "INSERT INTO invoices (id, quote_id, doc_json, total, issued, due, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
            (next_id, quote_id, json.dumps(doc.to_dict(), sort_keys=True), str(doc.total), issued.isoformat(),
             due.isoformat(), now_iso()))
    invoice = get_invoice(store, quote_id)

    # Always a local PDF invoice in the outbox. With Obol installed it is also marked ready for Obol: the agent
    # creates the draft through obol_create_invoice, never Docket, so Obol's own approval gate runs.
    handed, ref, problem = "local", "", ""
    if obol:
        if rec.customer.get("email"):
            handed = "obol_ready"
        else:
            problem = "Add the customer's email to create this invoice in Obol."
    pdf_path = ""
    if render is not None:
        from .outbox import write_outbox
        body = (f"Hi {rec.customer['name'].split()[0]},\n\nThanks for having us out. Your invoice {invoice['number']} "
                f"for {fmt_money(doc.total, doc.currency)} is attached, due {due:%b %d, %Y}.\n\n"
                f"{book.business.get('name', '')}\n{book.business.get('phone', '')}")
        folder = write_outbox(outbox_root, number=invoice["number"], version=1, business=book.business,
                              customer=rec.customer, subject=f"Invoice {invoice['number']} from {book.business.get('name', '')}",
                              body=body, attachments={f"{invoice['number']}.pdf": render(book, invoice)})
        pdf_path = str(folder / f"{invoice['number']}.pdf")
    with store.lock, store.conn:
        store.conn.execute("UPDATE invoices SET handoff = ?, ref = ? WHERE id = ?", (handed, ref, invoice["id"]))
    store.transition(quote_id, "invoiced", actor=actor, surface=surface,
                     detail=f"{invoice['number']}" + (f" (+ {extra_note})" if extra_note else ""))
    made = get_invoice(store, quote_id)
    return {**made, "pdf": pdf_path, "note": problem, "already": False, **_obol_fields(made, obol)}


def quote_id_for(store: Store, ref: str) -> int:
    """A quote id from 'Q-1007' or from an invoice number like 'INV-1001' (its job)."""
    from .service import parse_quote_id
    t = (ref or "").strip().upper()
    if t.startswith("INV"):
        n = t.removeprefix("INV").lstrip("-").strip()
        _ensure(store)
        row = store.conn.execute("SELECT quote_id FROM invoices WHERE id = ?", (int(n),)).fetchone() if n.isdigit() else None
        if row is None:
            raise InvoiceError(f"{ref!r} is not one of your invoices")
        return int(row[0])
    return parse_quote_id(ref)


def _obol_fields(invoice: dict, obol: bool) -> dict:
    """{'obol_payload': ...} when this invoice can go to Obol (installed, customer email on file, not paid yet,
    not already handed over by an older Docket), else {}."""
    if (not obol or not invoice["customer"].get("email") or invoice.get("status") == "paid"
            or invoice.get("handoff") == "obol"):
        return {}
    return {"obol_payload": obol_payload(invoice)}


def mark_paid(store: Store, quote_id: int, *, actor: str = "owner", surface: str = "") -> dict[str, Any]:
    _ensure(store)
    inv = get_invoice(store, quote_id)
    if inv is None:
        raise InvoiceError(f"Q-{quote_id} has no invoice yet")
    rec = store.get(quote_id)
    if rec.status != "paid":
        store.transition(quote_id, "paid", actor=actor, surface=surface, detail=inv["number"])
    with store.lock, store.conn:
        store.conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    return get_invoice(store, quote_id)


def obol_payload(invoice: dict) -> dict:
    """obol_create_invoice arguments: each Docket line as one item at its exact total (Obol takes whole-number
    quantities), tax as its own line, as a DRAFT (send: False). Docket never calls Obol with it: the agent passes
    it to obol_create_invoice, so Obol's own approval, risk checks and unattended-run block apply."""
    doc, cur = invoice["doc"], invoice["doc"].get("currency", "USD")
    items = []
    for line in doc["lines"]:
        qty = Decimal(line["qty"]).normalize()
        unit = f" {line['unit']}" if line["unit"] and line["unit"] != "each" else ""
        label = line["description"] + (f" × {format(qty, 'f')}{unit}" if (qty != 1 or unit) and line["kind"] != "discount" else "")
        items.append({"description": label[:200], "amount": float(Decimal(line["amount"])), "quantity": 1})
    if Decimal(doc["tax"]) != 0:
        items.append({"description": f"{doc['tax_name']} {fmt_qty(Decimal(doc['tax_rate']) * 100)}% on materials",
                      "amount": float(Decimal(doc["tax"])), "quantity": 1})
    return {"customer_email": invoice["customer"]["email"], "customer_name": invoice["customer"]["name"],
            "line_items": items, "currency": cur.lower(), "days_until_due": max(0, (date.fromisoformat(invoice["due"]) - date.fromisoformat(invoice["issued"][:10])).days),
            "memo": f"{invoice['number']} for quote {invoice['quote_number']} (Docket)", "send": False}
