"""Slash commands: /quote, /jobs, /pricebook, /docket. Sync handlers, so ask_choice can show buttons."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from . import runtime
from .money import fmt_money
from .service import (ServiceError, draft_from_words, jobs_text, owner_text, parse_quote_id, revise_from_words,
                      send_quote)
from .store import StoreError

logger = logging.getLogger(__name__)

QUOTE_USAGE = """Docket quotes
  /quote <customer, address or ZIP, what needs doing>   price a new job
  /quote show Q-1001                                     show a quote with its pricing rules
  /quote revise Q-1001 <change>                          e.g. "add a shutoff valve, saturday"
  /quote send Q-1001                                     send it to the customer
  /quote discard Q-1001
Example: /quote Dana Henderson, 14 Maple St 62707, swap the 50 gallon gas water heater, permit and haul away"""

ACTIONS = [
    {"value": "send", "label": "✅ Send to customer", "description": "Writes the quote and accept link to the outbox"},
    {"value": "edit", "label": "✏️ Edit", "description": "Tell me what to change"},
    {"value": "keep", "label": "Keep as draft"},
    {"value": "discard", "label": "🗑 Discard"},
]


def make_quote_command(ctx: Any):
    def quote(raw_args: str = "", **kwargs) -> str:
        surface = str(kwargs.get("platform") or "")
        try:
            return _quote(ctx, (raw_args or "").strip(), surface)
        except (ServiceError, StoreError) as exc:
            return f"Docket: {exc}"
        except Exception as exc:
            logger.exception("/quote failed")
            return f"Docket hit an error: {type(exc).__name__}: {exc}"
    return quote


def _quote(ctx: Any, text: str, surface: str) -> str:
    if not text or text.lower() in {"help", "-h", "--help"}:
        return QUOTE_USAGE
    store, book = runtime.store(), runtime.require_book()
    verb, _, rest = text.partition(" ")
    verb = verb.lower()
    if verb in {"show", "send", "discard", "revise"}:
        number, _, change = rest.strip().partition(" ")
        qid = parse_quote_id(number)
        if verb == "show":
            return owner_text(book, store.get(qid)) + f"\nStatus: {store.get(qid).status}"
        if verb == "send":
            return _send_text(send_quote(store, book, qid, outbox_root=runtime.outbox_root(), actor="owner",
                                         surface=surface, render_pdf=_pdf_renderer()))
        if verb == "discard":
            rec = store.transition(qid, "discarded", actor="owner", surface=surface)
            return f"{rec.number} discarded."
        if not change.strip():
            return f"What should change on {number}? e.g. /quote revise {number} add a shutoff valve, saturday"
        d = revise_from_words(store, book, qid, change, llm=runtime.llm(ctx), actor="owner", surface=surface)
        return _offer(ctx, store, book, d, surface, header=f"Revised to v{d.record.version}.")
    d = draft_from_words(store, book, text, llm=runtime.llm(ctx), actor="owner", surface=surface)
    return _offer(ctx, store, book, d, surface)


def _offer(ctx: Any, store, book, d, surface: str, header: str = "") -> str:
    rec = d.record
    body = owner_text(book, rec)
    notes = d.intake.notes + [f"Question: {q}" for q in d.intake.questions]
    text = "\n".join(x for x in [header, body, *notes] if x)
    if not rec.complete:
        return text + f"\n\nAdd prices or codes with /quote revise {rec.number} <change>."
    total = fmt_money(Decimal(rec.total), book.currency)
    try:
        choice = ctx.ask_choice(f"{rec.number} · {rec.customer['name']} · {total}. Send it?", ACTIONS)
    except Exception:
        choice = None
    if choice == "send":
        result = send_quote(store, book, rec.id, outbox_root=runtime.outbox_root(), actor="owner", surface=surface,
                            render_pdf=_pdf_renderer())
        return text + "\n\n" + _send_text(result)
    if choice == "discard":
        store.transition(rec.id, "discarded", actor="owner", surface=surface)
        return text + f"\n\n{rec.number} discarded."
    if choice == "edit":
        return text + f"\n\nTell me the change: /quote revise {rec.number} <what to change>"
    return text + f"\n\nSaved as a draft. Send it with /quote send {rec.number}"


def _send_text(result: dict) -> str:
    if result.get("already_sent"):
        return f"{result['number']} v{result['version']} was already sent ({result['status']}). Link: {result['accept_url']}"
    return (f"✅ Sent {result['number']} v{result['version']}.\nCustomer link: {result['accept_url']}\n"
            f"Saved to {result['outbox']}")


def _pdf_renderer():
    try:
        from .render import render_pdf
        return render_pdf
    except Exception:
        return None


def jobs(raw_args: str = "", **kwargs) -> str:
    try:
        wanted = {s.strip().lower().replace(" ", "_") for s in (raw_args or "").split(",") if s.strip()}
        return jobs_text(runtime.store(), wanted or None)
    except Exception as exc:
        logger.exception("/jobs failed")
        return f"Docket hit an error: {type(exc).__name__}: {exc}"
