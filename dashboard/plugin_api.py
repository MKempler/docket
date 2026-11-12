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
        link = service.accept_url(store, rec, runtime.link_base())
    message = service.customer_message(runtime.require_book(), rec, link) if link else None
    return {**_card(rec, store), "customer_info": rec.customer, "quote": rec.quote, "accept_url": link,
            "customer_message": message, "link_is_local": bool(link) and service.link_is_local(link),
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
            rec = store.get(quote_id)
            return {"ok": True, "message": f"Sent {result['number']}", "accept_url": result["accept_url"],
                    "customer_message": service.customer_message(book, rec, result["accept_url"]),
                    "link_is_local": service.link_is_local(result["accept_url"])}
        if action == "discard":
            store.transition(quote_id, "discarded", actor="owner", surface="desktop")
            return {"ok": True, "message": f"Q-{quote_id} discarded"}
        if action == "book":
            day = date.fromisoformat(str(body.get("date", "")))
            store.transition(quote_id, "scheduled", actor="owner", surface="desktop", scheduled_for=day.isoformat())
            return {"ok": True, "message": f"Q-{quote_id} booked for {day:%a %b} {day.day}"}
        if action == "invoice":
            inv = runtime.close_out(quote_id, extra_words=str(body.get("extra") or ""), surface="desktop")
            where = "handed to Obol" if inv["handoff"] == "obol" else "PDF saved"      # "obol": older Docket only
            obol = (f". To create it in Obol, ask your agent: send invoice {inv['number']} ({inv['quote_number']}) to Obol"
                    if inv.get("obol_payload") else "")
            return {"ok": True, "message": f"{inv['number']} created ({where})" + (f". {inv['note']}" if inv.get("note") else "") + obol}
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


# ---------------------------------------------------------------- price book page

def _pricebook_payload() -> dict[str, Any]:
    runtime, pbedit = _module("runtime"), _module("pbedit")
    book, errors, path, is_demo = runtime.book()
    history = runtime.own_pricebook_path().parent / "pricebook-history"
    can_undo = history.exists() and any(p.suffix in {".yaml", ".none"} for p in history.iterdir())
    pending = pbedit.pending(runtime.store())
    out: dict[str, Any] = {"path": str(path), "is_demo": is_demo, "errors": [str(e) for e in errors[:25]],
                           "can_undo": can_undo, "pending": _plan_json(pending) if pending else None}
    if book is None:
        return out
    parts_cost = lambda a: sum((book.materials[c.code].price * c.qty for c in a.materials  # noqa: E731
                                if c.code in book.materials), Decimal(0))
    out.update({
        "business": book.business.get("name", ""), "currency": book.currency,
        "labor_rate": str(book.labor_rate), "labor_minimum": str(book.labor_minimum_hours),
        "tax_name": book.tax_name, "tax_rate": str(book.tax_rate), "tax_applies_to": sorted(book.tax_applies_to),
        "service_call": ({"name": book.service_call.name, "amount": str(book.service_call.amount)}
                         if book.service_call else None),
        "multipliers": {k: str(v) for k, v in book.multipliers.items()},
        "zones": [{"name": z.name, "amount": str(z.amount), "zips": sorted(z.zips)} for z in book.zones],
        "assemblies": [{"code": a.code, "name": a.name, "unit": a.unit, "hours": str(a.labor_hours),
                        "parts": [{"code": c.code, "qty": str(c.qty),
                                   "name": book.materials[c.code].name if c.code in book.materials else c.code}
                                  for c in a.materials],
                        "price": str((book.labor_rate * a.labor_hours + parts_cost(a)).quantize(Decimal("0.01"))),
                        "aliases": list(a.aliases), "exclude": list(a.exclude)} for a in book.assemblies.values()],
        "fees": [{"code": f.code, "name": f.name, "unit": f.unit, "amount": str(f.amount), "aliases": list(f.aliases)}
                 for f in book.fees.values()],
        "materials": [{"code": m.code, "name": m.name, "unit": m.unit, "price": str(m.price), "cost": str(m.cost),
                       "markup": str(m.markup), "aliases": list(m.aliases)} for m in book.materials.values()],
    })
    return out


def _plan_json(plan) -> dict[str, Any]:
    return {"token": plan.token, "changes": [c.__dict__ for c in plan.changes], "skipped": plan.skipped,
            "summary": plan.summary, "preview": plan.preview()}


def _pb_call(fn):
    pbedit = _module("pbedit")
    try:
        return fn()
    except pbedit.EditError as exc:
        raise HTTPException(400, str(exc))


@router.get("/pricebook")
def pricebook() -> dict[str, Any]:
    return _pricebook_payload()


@router.post("/pricebook/preview")
def pricebook_preview(body: dict | None = None) -> dict[str, Any]:
    runtime = _module("runtime")
    body = body or {}
    text = str(body.get("text") or "")[:200_000]
    removals = tuple(str(c) for c in (body.get("remove") or []) if str(c).strip())
    fresh = bool(body.get("new"))
    plan = _pb_call(lambda: runtime.pricebook_propose(text, llm=runtime.llm(runtime.CTX), removals=removals,
                                                      fresh=fresh))
    return {"ok": True, "plan": _plan_json(plan)}


@router.post("/pricebook/{action}")
def pricebook_action(action: str, body: dict | None = None) -> dict[str, Any]:
    runtime = _module("runtime")
    token = str((body or {}).get("token") or "")
    if action == "save":
        return {"ok": True, "message": _pb_call(lambda: runtime.pricebook_save(token))}
    if action == "cancel":
        return {"ok": True, "message": runtime.pricebook_cancel(token)}
    if action == "undo":
        return {"ok": True, "message": runtime.pricebook_undo()}
    raise HTTPException(404, f"Unknown action {action!r}")
