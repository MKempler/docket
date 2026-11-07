"""Close-out: a finished job becomes an invoice, priced by the same engine, then handed to the owner's
invoicing tool (Obol) when one is installed, or kept as a local invoice PDF in the outbox.

Docket stops where invoicing starts: it never takes payment itself.
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
              llm: Any = None, handoff: Callable[[dict], str] | None = None, actor: str = "owner",
              surface: str = "", today: date | None = None, render: Callable | None = None) -> dict[str, Any]:
    """Finish a job and make its invoice. Safe to repeat: a job is invoiced once."""
    _ensure(store)
    existing = get_invoice(store, quote_id)
    if existing:
        return {**existing, "already": True}
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

    # Hand-off to the owner's invoicing plugin, else a local PDF invoice in the outbox.
    handed, ref, problem = "local", "", ""
    if handoff is not None:
        if not rec.customer.get("email"):
            problem = "Add the customer's email to hand this invoice to your invoicing plugin."
        else:
            try:
                ref = handoff(invoice) or ""
                handed = "obol"
            except Exception as exc:
                problem = f"Invoicing plugin declined: {exc}"
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
    return {**get_invoice(store, quote_id), "pdf": pdf_path, "note": problem, "already": False}


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
    quantities), tax as its own line, created as a DRAFT so Obol's own approval rules apply before sending."""
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
