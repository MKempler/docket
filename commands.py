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
  /quote book Q-1001 2026-11-12                          schedule an accepted job
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
    if verb == "book":
        number, _, when = rest.strip().partition(" ")
        from datetime import date as _date
        try:
            day = _date.fromisoformat(when.strip())
        except ValueError:
            return f"Give the date as YYYY-MM-DD, e.g. /quote book {number or 'Q-1001'} 2026-11-12"
        rec = store.transition(parse_quote_id(number), "scheduled", actor="owner", surface=surface,
                               scheduled_for=day.isoformat())
        return f"📅 {rec.number} {rec.customer['name']} booked for {day:%A %b} {day.day}."
    if verb in {"show", "send", "discard", "revise"}:
        number, _, change = rest.strip().partition(" ")
        qid = parse_quote_id(number)
        if verb == "show":
            return owner_text(book, store.get(qid)) + f"\nStatus: {store.get(qid).status}"
        if verb == "send":
            return _send_text(send_quote(store, book, qid, outbox_root=runtime.outbox_root(), actor="owner",
                                         surface=surface, render_pdf=_pdf_renderer(),
                                         base_url=runtime.ensure_accept_server()))
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
    notes = d.intake.notes + [f"Question: {q}" for q in d.intake.questions]
    if runtime.push_quote_card(rec, surface):          # Telegram / Discord / Slack: card with buttons
        return "\n".join(x for x in [header, *notes] if x)
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
                            render_pdf=_pdf_renderer(), base_url=runtime.ensure_accept_server())
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


DOCKET_HELP = """Docket
  /quote <job>        price a job from your price book, then send it
  /jobs [status]      your quotes and jobs by status
  /pricebook          show or validate your price book
  /docket demo        load the demo company (Copperline Plumbing & Drain); 'demo reset' wipes existing quotes first
  /docket setup       run in Telegram, Discord or Slack: quote cards and customer pings come to this chat
  /docket serve       start the customer accept page in this session
  /docket link <url>  public base URL for customer links (e.g. your tunnel); 'local' to reset"""


def docket_admin(raw_args: str = "", **kwargs) -> str:
    try:
        verb, _, rest = (raw_args or "").strip().partition(" ")
        verb = verb.lower()
        if verb == "serve":
            return f"Customer accept page: {runtime.ensure_accept_server()}  (runs while this Hermes process runs)"
        if verb == "setup":
            return setup_owner(kwargs)
        if verb == "demo":
            return run_demo(force=rest.strip().lower() == "reset")
        if verb == "link":
            value = rest.strip()
            if not value:
                return f"Customer links use: {runtime.ensure_accept_server()}"
            if value.lower() == "local":
                runtime.store().set_meta("public_base_url", "")
                return "Customer links will use the local accept page."
            if not value.startswith(("https://", "http://")):
                return "Give a full URL, e.g. https://quotes.example.com"
            runtime.store().set_meta("public_base_url", value.rstrip("/"))
            return f"Customer links will use {value.rstrip('/')}. Make sure it forwards to this machine's accept page."
        return DOCKET_HELP
    except Exception as exc:
        logger.exception("/docket failed")
        return f"Docket hit an error: {type(exc).__name__}: {exc}"


def cli_setup(subparser) -> None:
    subs = subparser.add_subparsers(dest="docket_command")
    demo_p = subs.add_parser("demo", help="Load the demo company with quotes in every state")
    demo_p.add_argument("--reset", action="store_true", help="Wipe existing Docket quotes first")
    serve = subs.add_parser("serve", help="Run the customer accept page in the foreground")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    subparser.set_defaults(func=cli_main)


def cli_main(args) -> None:
    sub = getattr(args, "docket_command", None)
    if sub == "demo":
        print(run_demo(force=bool(getattr(args, "reset", False))))
        return
    if sub == "serve":
        from .accept_server import serve_forever
        serve_forever(runtime.accept_app(), host=args.host, port=args.port)
        return
    print(DOCKET_HELP.replace("/docket", "hermes docket"))


def run_demo(*, force: bool = False) -> str:
    from .demo import DemoError, seed
    try:
        made = seed(runtime.store(), runtime.require_book(), runtime.outbox_root(), force=force)
    except DemoError as exc:
        return f"Docket: {exc}"
    return ("Loaded the demo company, Copperline Plumbing & Drain (fictional):\n  " + "\n  ".join(made) +
            "\n\nTry it (start a new session first so the tools load):\n"
            "  /quote Dana Henderson, 14 Maple St 62707, swap the 50 gallon gas water heater, permit and haul away\n"
            "  /jobs\n  /pricebook")


def setup_owner(kwargs: dict) -> str:
    from .owner import PLATFORMS, set_owner
    platform = str(kwargs.get("platform") or "")
    source = kwargs.get("source")
    if platform not in PLATFORMS or source is None:
        return ("Run /docket setup from your Telegram, Discord or Slack chat with Hermes. "
                "Docket will send quote cards with buttons, and customer accept pings, to that chat.")
    chat_id, user_id = str(getattr(source, "chat_id", "") or ""), str(getattr(source, "user_id", "") or "")
    if not chat_id or not user_id:
        return "Docket couldn't read this chat's id. Try again from a direct message with the bot."
    set_owner(runtime.store(), platform, chat_id=chat_id, user_id=user_id,
              thread_id=str(getattr(source, "thread_id", "") or ""))
    return (f"Docket is set up on {platform.title()}. Quote cards and customer updates will come here, and only you "
            f"can press their buttons. Try: /quote Dana Henderson, 14 Maple St 62707, water heater, permit")
