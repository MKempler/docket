"""Docket backend for Hermes Desktop / dashboard: /api/plugins/docket/...

Runs inside the gateway process next to the loaded Docket plugin and reuses its ledger and service functions,
so the Jobs board, Telegram buttons and slash commands always agree.
"""

from __future__ import annotations

import importlib
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

COLUMNS = [("draft", "Draft"), ("sent", "Sent"), ("accepted", "Accepted"), ("scheduled", "Booked"),
           ("in_progress", "In progress"), ("done", "Done")]
CLOSED = {"declined", "expired", "discarded", "cancelled", "paid"}
MOVES = {"in_progress": "Start job", "done": "Mark done", "cancelled": "Cancel job"}
DONE_TEXT = {"in_progress": "started", "done": "marked done", "cancelled": "cancelled"}


def _module(name: str):
    """The Docket package Hermes already loaded in this process (found by its file, not a guessed name)."""
    init = (Path(__file__).resolve().parents[1] / "__init__.py")
    for mod_name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        try:
            if f and Path(f).resolve() == init:
                return importlib.import_module(f"{mod_name}.{name}")
        except Exception:
            continue
    raise HTTPException(503, "Docket is not loaded in this Hermes process. Enable it and restart the gateway.")


def _card(rec, store) -> dict[str, Any]:
    opened = rec.status == "viewed"
    return {"id": rec.id, "number": rec.number, "version": rec.version, "status": rec.status,
            "column": ("sent" if rec.status in {"sent", "viewed"} else "done" if rec.status == "invoiced"
                       else rec.status), "opened": opened, "invoiced": rec.status == "invoiced",
            "customer": rec.customer["name"], "site": rec.site, "zip": rec.zip, "total": rec.total,
            "complete": rec.complete, "scheduled_for": rec.scheduled_for, "updated_at": rec.updated_at,
            "lines": len(rec.quote.get("lines", []))}


@router.get("/board")
def board(closed: bool = False) -> dict[str, Any]:
    runtime = _module("runtime")
    store, book = runtime.store(), runtime.require_book()
    recs = store.list()
    cards = [_card(r, store) for r in recs]
    columns = []
    for key, label in COLUMNS:
        items = [c for c in cards if c["column"] == key]
        columns.append({"key": key, "label": label, "count": len(items),
                        "total": str(sum((Decimal(c["total"]) for c in items), Decimal(0))), "items": items})
    open_items = [c for c in cards if c["status"] in {"draft", "sent", "viewed"}]
    result = {"currency": book.currency, "business": book.business.get("name", ""), "columns": columns,
              "open_count": len(open_items),
              "open_value": str(sum((Decimal(c["total"]) for c in open_items), Decimal(0)))}
    if closed:
        result["closed"] = [c for c in cards if c["status"] in CLOSED]
    return result


@router.get("/quote/{quote_id}")
def quote(quote_id: int) -> dict[str, Any]:
    runtime, service = _module("runtime"), _module("service")
    store = runtime.store()
    try:
        rec = store.get(quote_id)
    except Exception:
        raise HTTPException(404, f"No quote Q-{quote_id}")
    link = None
    if rec.status in {"sent", "viewed", "accepted", "scheduled", "in_progress", "done"}:
        base = store.get_meta("public_base_url") or None
        link = service.accept_url(store, rec, base)
    return {**_card(rec, store), "customer_info": rec.customer, "quote": rec.quote, "accept_url": link,
            "events": [{"to": e["to_status"], "from": e["from_status"], "actor": e["actor"], "surface": e["surface"],
                        "detail": e["detail"], "at": e["at"]} for e in store.events(quote_id)],
            "actions": _actions(rec)}


def _actions(rec) -> list[dict[str, str]]:
    acts = []
    if rec.status == "draft":
        if rec.complete:
            acts.append({"id": "send", "label": "Send to customer"})
        acts.append({"id": "discard", "label": "Discard"})
    if rec.status == "accepted":
        acts.append({"id": "book", "label": "Book a day"})
    if rec.status == "scheduled":
        acts += [{"id": "book", "label": "Reschedule"}, {"id": "in_progress", "label": MOVES["in_progress"]}]
    if rec.status == "in_progress":
        acts.append({"id": "done", "label": MOVES["done"]})
    if rec.status == "done":
        acts.append({"id": "invoice", "label": "Create invoice"})
    if rec.status == "invoiced":
        acts.append({"id": "paid", "label": "Mark paid"})
    return acts


@router.post("/quote/{quote_id}/{action}")
def act(quote_id: int, action: str, body: dict | None = None) -> dict[str, Any]:
    runtime, service = _module("runtime"), _module("service")
    store, book = runtime.store(), runtime.require_book()
    body = body or {}
    try:
        if action == "send":
            result = service.send_quote(store, book, quote_id, outbox_root=runtime.outbox_root(), actor="owner",
                                        surface="desktop", base_url=runtime.ensure_accept_server(),
                                        render_pdf=runtime.render_pdf_fn())
            return {"ok": True, "message": f"Sent {result['number']}", "accept_url": result["accept_url"]}
        if action == "discard":
            store.transition(quote_id, "discarded", actor="owner", surface="desktop")
            return {"ok": True, "message": f"Q-{quote_id} discarded"}
        if action == "book":
            day = date.fromisoformat(str(body.get("date", "")))
            store.transition(quote_id, "scheduled", actor="owner", surface="desktop", scheduled_for=day.isoformat())
            return {"ok": True, "message": f"Q-{quote_id} booked for {day:%a %b} {day.day}"}
        if action == "invoice":
            inv = runtime.close_out(quote_id, extra_words=str(body.get("extra") or ""), surface="desktop")
            where = "handed to Obol" if inv["handoff"] == "obol" else "PDF saved"
            return {"ok": True, "message": f"{inv['number']} created ({where})" + (f". {inv['note']}" if inv.get("note") else "")}
        if action == "paid":
            inv = _module("invoices").mark_paid(store, quote_id, surface="desktop")
            return {"ok": True, "message": f"{inv['number']} marked paid"}
        if action in MOVES:
            rec = store.transition(quote_id, action, actor="owner", surface="desktop")
            return {"ok": True, "message": f"{rec.number} {rec.customer['name']} {DONE_TEXT[action]}"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(409, str(exc))
    raise HTTPException(404, f"Unknown action {action!r}")
