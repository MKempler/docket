"""Slash commands: /quote, /jobs, /pricebook, /docket. Sync handlers, so ask_choice can show buttons."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from . import runtime
from .money import fmt_money
from .service import (ServiceError, draft_from_words, jobs_text, owner_text, parse_quote_id, revise_from_words,
                      send_quote)
from .invoices import InvoiceError
from .store import StoreError

logger = logging.getLogger(__name__)

QUOTE_USAGE = """Docket quotes
  /quote <customer, address or ZIP, what needs doing>   price a new job
  /quote show Q-1001                                     show a quote with its pricing rules
  /quote revise Q-1001 <change>                          e.g. "add a shutoff valve, saturday"
  /quote send Q-1001                                     send it to the customer
  /quote discard Q-1001
  /quote book Q-1001 2026-11-12                          schedule an accepted job
  /quote done Q-1001 [extra work]                        finish the job and make its invoice
  /quote paid Q-1001                                     mark the invoice paid
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
        except (ServiceError, StoreError, InvoiceError) as exc:
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
    if verb in {"done", "paid"}:
        number, _, extra = rest.strip().partition(" ")
        qid = parse_quote_id(number)
        if verb == "paid":
            from .invoices import mark_paid
            inv = mark_paid(store, qid, surface=surface)
            return f"💵 {inv['number']} ({inv['quote_number']}) marked paid."
        return _invoice_text(runtime.close_out(qid, extra_words=extra, surface=surface, llm=runtime.llm(ctx)))
    if verb in {"show", "send", "discard", "revise"}:
        number, _, change = rest.strip().partition(" ")
        qid = parse_quote_id(number)
        if verb == "show":
            return owner_text(book, store.get(qid)) + f"\nStatus: {store.get(qid).status}"
        if verb == "send":
            result = send_quote(store, book, qid, outbox_root=runtime.outbox_root(), actor="owner", surface=surface,
                                render_pdf=_pdf_renderer(), base_url=runtime.ensure_accept_server())
            return _send_text(result, book, store.get(qid))
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
        return text + "\n\n" + _send_text(result, book, store.get(rec.id))
    if choice == "discard":
        store.transition(rec.id, "discarded", actor="owner", surface=surface)
        return text + f"\n\n{rec.number} discarded."
    if choice == "edit":
        return text + f"\n\nTell me the change: /quote revise {rec.number} <what to change>"
    return text + f"\n\nSaved as a draft. Send it with /quote send {rec.number}"


def _send_text(result: dict, book=None, rec=None) -> str:
    from .service import LOCAL_LINK_NOTE, customer_message, link_is_local
    url = result["accept_url"]
    if result.get("already_sent"):
        head = f"{result['number']} v{result['version']} was already sent ({result['status']})."
    else:
        head = f"✅ Sent {result['number']} v{result['version']}. Email and PDF saved to {result['outbox']}"
    if book is None or rec is None:
        return f"{head}\nCustomer link: {url}"
    out = (f"{head}\n\nMessage for {rec.customer['name']} (copy it into a text, WhatsApp or email):\n\n"
           f"{customer_message(book, rec, url)}")
    return out + (f"\n\n{LOCAL_LINK_NOTE}" if link_is_local(url) else "")


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
  /pricebook          show your price book; /pricebook add <prices> changes it (preview first)
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
                _save_public_link("")
                return "Customer links will use the local accept page."
            if not value.startswith(("https://", "http://")):
                return "Give a full URL, e.g. https://quotes.example.com"
            _save_public_link(value.rstrip("/"))
            return f"Customer links will use {value.rstrip('/')}. Make sure it forwards to this machine's accept page."
        return DOCKET_HELP
    except Exception as exc:
        logger.exception("/docket failed")
        return f"Docket hit an error: {type(exc).__name__}: {exc}"


def _save_public_link(value: str) -> None:
    """One source of truth: the 'Public link for customers' setting (Desktop shows it). Older meta is cleared."""
    try:
        runtime.CTX.set_config("public_link", value)
    except Exception:
        runtime.store().set_meta("public_base_url", value)
        return
    runtime.store().set_meta("public_base_url", "")


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


def _invoice_text(inv: dict) -> str:
    if inv.get("already"):
        return f"{inv['quote_number']} already has invoice {inv['number']} ({fmt_money(Decimal(inv['total']))}, {inv['status']})."
    where = ("handed to your invoicing plugin (Obol) as a draft" if inv["handoff"] == "obol"
             else f"saved to {inv['pdf']}" if inv.get("pdf") else "saved")
    out = f"🧾 {inv['number']} for {inv['quote_number']} {inv['customer']['name']}: {fmt_money(Decimal(inv['total']))}, due {inv['due']}. Invoice {where}."
    return out + (f"\n{inv['note']}" if inv.get("note") else "")


PRICEBOOK_USAGE = """Docket price book
  /pricebook                         show it
  /pricebook validate                check it for mistakes
  /pricebook add <prices>            add or change prices in your own words, or paste a whole list / CSV:
                                       labor 145/hr · tax 8.25% · service call $99
                                       3/4" PEX $2.20/ft · Sump pump $289 each · Backflow test $95 flat
                                       TOILET-REPLACE 2 hours
  /pricebook import <file.csv|.txt>  the same, from a file
  /pricebook remove <CODE>           take an item out
  /pricebook alias <CODE> <words>    teach Docket another name for an item
  /pricebook save | cancel           keep or drop the change waiting for review
  /pricebook undo                    put back the price book from before the last save
  /pricebook new <business>, labor <rate>/hr[, tax 7%]   start your own (empty) price book
Every change is shown first and saved only when you confirm."""

SAVE_CHOICES = [
    {"value": "save", "label": "✅ Save these changes"},
    {"value": "cancel", "label": "Cancel"},
]


def make_pricebook_command(ctx: Any):
    def pricebook(raw_args: str = "", **kwargs) -> str:
        surface = str(kwargs.get("platform") or "")
        try:
            return _pricebook(ctx, (raw_args or "").strip(), surface)
        except Exception as exc:
            from .pbedit import EditError
            if isinstance(exc, EditError):
                return f"Docket: {exc}"
            logger.exception("/pricebook failed")
            return f"Docket could not edit the price book: {type(exc).__name__}: {exc}"
    return pricebook


def _pricebook(ctx: Any, text: str, surface: str) -> str:
    verb, _, rest = text.partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb in {"", "show", "list"}:
        return pricebook_text()
    if verb == "validate":
        return pricebook_text(validate=True)
    if verb in {"help", "-h", "--help"}:
        return PRICEBOOK_USAGE
    if verb == "save":
        return runtime.pricebook_save()
    if verb == "cancel":
        return runtime.pricebook_cancel()
    if verb == "undo":
        return runtime.pricebook_undo()
    if verb in {"add", "set", "change", "update", "import", "new", "remove", "delete", "alias"} and not rest:
        return PRICEBOOK_USAGE
    if verb == "import":
        from pathlib import Path
        path = Path(rest.strip("'\"")).expanduser()
        if not path.is_file():
            return f"Docket: no file at {path}. Paste the list instead: /pricebook add <your prices>"
        if path.stat().st_size > 512_000:
            return "Docket: that file is over 500 KB. Split it, or paste the part with your prices."
        plan = runtime.pricebook_propose(path.read_text(encoding="utf-8", errors="replace"), llm=runtime.llm(ctx))
    elif verb in {"add", "set", "change", "update"}:
        plan = runtime.pricebook_propose(rest, llm=runtime.llm(ctx))
    elif verb in {"remove", "delete"}:
        plan = runtime.pricebook_propose(removals=tuple(c for c in rest.replace(",", " ").split() if c))
    elif verb == "alias":
        code, _, words = rest.partition(" ")
        if not words.strip():
            return "Give the code and the new name, e.g. /pricebook alias PTRAP p trap"
        plan = runtime.pricebook_propose(aliases=((code, words.strip().strip("'\"")),))
    elif verb == "new":
        plan = runtime.pricebook_propose(rest, fresh=True)
    else:
        # "/pricebook labor 145/hr" with no verb: treat the whole thing as an add
        plan = runtime.pricebook_propose(text, llm=runtime.llm(ctx))
    return _offer_pricebook(ctx, plan, surface)


def _offer_pricebook(ctx: Any, plan, surface: str) -> str:
    preview = plan.preview()
    if not plan.changes:
        return preview
    if runtime.push_pricebook_card(plan, surface):      # Telegram / Discord / Slack: Save / Cancel buttons
        return ""
    try:
        choice = ctx.ask_choice(f"Save {len(plan.changes)} price book change{'s' if len(plan.changes) != 1 else ''}?",
                                SAVE_CHOICES)
    except Exception:
        choice = None
    if choice == "save":
        return preview + "\n\n" + runtime.pricebook_save(plan.token)
    if choice == "cancel":
        return preview + "\n\n" + runtime.pricebook_cancel(plan.token)
    return preview + "\n\nReply /pricebook save to keep it, or /pricebook cancel."


def pricebook_text(*, validate: bool = False) -> str:
    from .money import fmt_qty
    book, errors, path, is_demo = runtime.book()
    label = " (demo price book: /pricebook new starts your own)" if is_demo else ""
    if errors:
        return f"Price book {path} has {len(errors)} error(s):\n" + "\n".join(f"  • {e}" for e in errors[:25])
    if validate:
        return (f"Price book OK{label}: {len(book.assemblies)} jobs, {len(book.fees)} flat-rate items, "
                f"{len(book.materials)} materials.\n{path}")
    cur = book.currency
    head = (f"{book.business.get('name', 'Price book')}{label}\n{cur} · labor {fmt_money(book.labor_rate, cur)}/h "
            f"(min {fmt_qty(book.labor_minimum_hours)} h) · {book.tax_name} {fmt_qty(book.tax_rate * 100)}% on "
            f"{', '.join(sorted(book.tax_applies_to)) or 'nothing'}"
            + (f" · {book.service_call.name} {fmt_money(book.service_call.amount, cur)}" if book.service_call else ""))
    lines = [head]
    if book.assemblies:
        lines.append("Jobs (labor + parts):")
        lines += [f"  {a.code:<18} {a.name}  {fmt_qty(a.labor_hours)} h + {len(a.materials)} part"
                  f"{'s' if len(a.materials) != 1 else ''}  per {a.unit}" for a in book.assemblies.values()]
    if book.fees:
        lines.append("Flat-rate:")
        lines += [f"  {f.code:<18} {f.name}  {fmt_money(f.amount, cur)}" for f in book.fees.values()]
    if book.materials:
        lines.append("Materials:")
        lines += [f"  {m.code:<18} {m.name}  {fmt_money(m.price, cur)}/{m.unit}" for m in book.materials.values()]
    lines.append(f"File: {path}\nChange it: /pricebook add <prices>  (e.g. labor 145/hr, 3/4\" PEX $2.20/ft)")
    return "\n".join(lines)
