"""Docket — Hermes plugin registration.

register() stays import-light: heavy libraries (reportlab, Pillow, telegram) are imported
inside handlers so loading never risks the plugin load deadline.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

PLUGIN_ID = "docket"
VERSION = "0.1.1"
GATED_TOOLS = {"docket_send_quote", "docket_pricebook_save"}            # ask the owner first; never from cron
CRON_BLOCKED_TOOLS = GATED_TOOLS | {"docket_close_job"}                  # never from scheduled runs
OBOL_NEXT = ("Docket has NOT created anything in Obol. Tell the owner the invoice is ready and offer to create it "
             "in Obol. Only if they want that, call obol_create_invoice with obol_payload exactly as given (do not "
             "change any amount); Obol asks the owner to approve it.")

NEVER_PRICE = ("Never state, estimate or change a price yourself: show the owner exactly the text this tool returns, "
               "and say plainly when a line needs a price.")

PRICE_PREVIEW_SCHEMA = {
    "name": "docket_price_preview",
    "description": (
        "Price trade work from the owner's Docket price book WITHOUT saving a quote. Use it for 'what would X cost' "
        "questions. Pass price-book codes with quantities, the job ZIP, and flags (after_hours, weekend, emergency). "
        "Put work that has no code in 'unpriced'. " + NEVER_PRICE),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {"type": "array", "description": "Price-book codes with quantities (LABOR takes hours).",
                      "items": {"type": "object", "properties": {
                          "code": {"type": "string"}, "qty": {"type": "number"}, "note": {"type": "string"}},
                          "required": ["code", "qty"]}},
            "zip": {"type": "string"},
            "flags": {"type": "array", "items": {"type": "string", "enum": ["after_hours", "weekend", "emergency"]}},
            "customer_tier": {"type": "string"},
            "unpriced": {"type": "array", "items": {"type": "string"}},
            "include_service_call": {"type": "boolean"},
        },
        "required": ["items"],
    },
}

QUOTE_SCHEMA = {
    "name": "docket_quote",
    "description": (
        "Create a saved, numbered quote (status draft) from the owner's description of a job: customer name, address "
        "or ZIP, and the work. Call it ONCE per job and pass the owner's words as they wrote them. Docket maps the words to the owner's price book and prices every line in code. "
        "To change an existing quote pass quote_id and describe the change. Does NOT send anything; the owner sends "
        "with the ✅ button, /quote send, or docket_send_quote. " + NEVER_PRICE),
    "parameters": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "The job in the owner's words, or the change to make"},
            "quote_id": {"type": "string", "description": "Existing quote to revise, e.g. Q-1001"},
        },
        "required": ["description"],
    },
}

SEND_SCHEMA = {
    "name": "docket_send_quote",
    "description": ("Send a draft quote to its customer (writes the email and accept link). Requires the owner's "
                    "approval and never runs from scheduled jobs. Only call it when the owner explicitly asks to send."),
    "parameters": {"type": "object", "properties": {"quote_id": {"type": "string", "description": "e.g. Q-1001"}},
                   "required": ["quote_id"]},
}

CLOSE_SCHEMA = {
    "name": "docket_close_job",
    "description": ("Finish a booked job and create its invoice from the quote (plus any extra work the owner "
                    "describes, priced from the price book), saved as an invoice PDF in the outbox. Never takes "
                    "payment and never contacts Obol itself: when the owner's invoicing plugin (Obol) is installed "
                    "the result includes obol_payload, for obol_create_invoice if the owner wants it there. For a job "
                    "that is already invoiced it returns the same invoice (and obol_payload), so use it when the "
                    "owner asks to send an existing invoice to Obol. Blocked from scheduled runs."),
    "parameters": {"type": "object", "properties": {
        "quote_id": {"type": "string", "description": "e.g. Q-1004, or an invoice number like INV-1001 for an "
                                                       "already invoiced job"},
        "extra_work": {"type": "string", "description": "Optional extra work done on site, in the owner's words"}},
        "required": ["quote_id"]},
}

JOBS_SCHEMA = {
    "name": "docket_jobs",
    "description": "List the owner's quotes and jobs grouped by status (draft, sent, accepted, scheduled, ...).",
    "parameters": {"type": "object", "properties": {
        "status": {"type": "string", "description": "Optional comma-separated statuses to include"}}},
}


PRICEBOOK_EDIT_SCHEMA = {
    "name": "docket_pricebook_edit",
    "description": ("Preview changes to the owner's price book from their own words or a pasted price list / CSV: "
                    "labor rate, tax, service call, item prices, new items, a job's labor hours. Never saves; shows "
                    "exactly what would change. Pass the owner's words unchanged; never make up prices."),
    "parameters": {"type": "object", "properties": {
        "changes": {"type": "string", "description": "The owner's words or pasted list, word for word"},
        "remove": {"type": "array", "items": {"type": "string"}, "description": "Item codes to remove"}}},
}

PRICEBOOK_SAVE_SCHEMA = {
    "name": "docket_pricebook_save",
    "description": ("Save the price book change previewed by docket_pricebook_edit. Requires the owner's approval. "
                    "Only call it after the owner has seen the preview and said to save."),
    "parameters": {"type": "object", "properties": {
        "preview_id": {"type": "string", "description": "The preview_id docket_pricebook_edit returned"}},
        "required": ["preview_id"]},
}


def _make_pricebook_edit_tool(ctx):
    def handler(args: dict, **kwargs) -> str:
        try:
            from . import runtime
            from .pbedit import EditError
            args = args or {}
            words = str(args.get("changes") or "").strip()
            removals = tuple(str(c) for c in (args.get("remove") or []) if str(c).strip())
            try:
                plan = runtime.pricebook_propose(words, llm=runtime.llm(ctx), removals=removals)
            except EditError as exc:
                return json.dumps({"ok": False, "error": str(exc)})
            out = {"ok": True, "preview": plan.preview(), "changes": len(plan.changes)}
            if not plan.changes:
                out["next"] = "Nothing to save. Tell the owner what Docket couldn't read, from the preview."
                return json.dumps(out)
            out["preview_id"] = plan.token
            surface = ""
            try:
                surface = ctx.session_surface() or ""
            except Exception:
                pass
            if runtime.push_pricebook_card(plan, surface):
                out["next"] = ("The preview was sent to the owner with Save / Cancel buttons. Say it is waiting for "
                               "them; do not call docket_pricebook_save.")
            else:
                out["next"] = ("Show the owner the preview exactly as written and ask whether to save it. Only if they "
                               "say yes, call docket_pricebook_save with this preview_id.")
            return json.dumps(out)
        except Exception as exc:
            logger.exception("docket_pricebook_edit failed")
            return _err(exc)
    return handler


def _pricebook_save_tool(args: dict, **kwargs) -> str:
    try:
        from . import runtime
        from .pbedit import EditError
        try:
            return json.dumps({"ok": True, "message": runtime.pricebook_save(str((args or {}).get("preview_id") or ""))})
        except EditError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
    except Exception as exc:
        logger.exception("docket_pricebook_save failed")
        return _err(exc)


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}" if not isinstance(exc, ValueError) else str(exc)})


def _price_preview(args: dict, **kwargs) -> str:
    try:
        from . import runtime
        from .pricing import SpecError, price, render_text, spec_from_args
        book, errors, path, is_demo = runtime.book()
        if errors:
            return json.dumps({"ok": False, "error": "price book has errors; fix them with /pricebook validate",
                               "errors": [str(e) for e in errors[:20]], "path": str(path)})
        try:
            spec = spec_from_args(args or {})
        except SpecError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        quote = price(book, spec)
        return json.dumps({"ok": True, "demo_price_book": is_demo, "quote": quote.to_dict(), "text": render_text(quote)})
    except Exception as exc:  # handlers never raise
        logger.exception("docket_price_preview failed")
        return _err(exc)


def _make_quote_tool(ctx):
    def handler(args: dict, **kwargs) -> str:
        try:
            from . import runtime
            from .service import draft_from_words, owner_text, parse_quote_id, revise_from_words
            store, book = runtime.store(), runtime.require_book()
            words = str((args or {}).get("description") or "").strip()
            owner_words = str(kwargs.get("user_task") or "").strip()[:2000]
            if owner_words and owner_words.lower() not in words.lower():
                # The agent's description can drop details; give intake the owner's own words too.
                words = f"{words}\n\nThe owner's own message: {owner_words}" if words else owner_words
            if (args or {}).get("quote_id"):
                d = revise_from_words(store, book, parse_quote_id(args["quote_id"]), words, llm=runtime.llm(ctx),
                                      actor="agent", surface="tool")
            else:
                session = str(kwargs.get("session_id") or "")
                d = draft_from_words(store, book, words, llm=runtime.llm(ctx), actor="agent", surface="tool",
                                     dedupe_key=f"{session}:{owner_words[:200]}" if session else "")
            rec = d.record
            surface = ""
            try:
                surface = ctx.session_surface() or ""
            except Exception:
                pass
            if surface == "desktop":
                return json.dumps({"ok": True, "quote_id": rec.number, "version": rec.version, "status": rec.status,
                                   "complete": rec.complete, "total": rec.total, "notes": d.intake.notes,
                                   "next": (f"The quote is saved. Do not call any other tool to show it. In your reply "
                                            f"text, write this exact line as its own paragraph: "
                                            f'::docket-quote{{id="{rec.number}"}}  (Desktop turns it into the quote card '
                                            "with a Send button). Then add one short sentence; do not list the prices.")})
            if runtime.push_quote_card(rec, surface):
                return json.dumps({"ok": True, "quote_id": rec.number, "version": rec.version, "status": rec.status,
                                   "card_sent": True, "complete": rec.complete, "notes": d.intake.notes,
                                   "next": _card_sent_line(rec)})
            return json.dumps({"ok": True, "quote_id": rec.number, "version": rec.version, "status": rec.status,
                               "complete": rec.complete, "total": rec.total, "text": owner_text(book, rec),
                               "notes": d.intake.notes, "questions": d.intake.questions,
                               "next": (f"Show the owner the text. They can send with /quote send {rec.number}."
                                        if rec.complete else "Some lines need a price from the owner before sending.")})
        except Exception as exc:
            logger.exception("docket_quote failed")
            return _err(exc)
    return handler


def _send_tool(args: dict, **kwargs) -> str:
    try:
        from . import runtime
        from .commands import _pdf_renderer
        from .service import LOCAL_LINK_NOTE, customer_message, link_is_local, parse_quote_id, send_quote
        store, book = runtime.store(), runtime.require_book()
        qid = parse_quote_id(str((args or {}).get("quote_id")))
        result = send_quote(store, book, qid, outbox_root=runtime.outbox_root(), actor="agent", surface="tool",
                            render_pdf=_pdf_renderer(), base_url=runtime.ensure_accept_server())
        result["customer_message"] = customer_message(book, store.get(qid), result["accept_url"])
        result["next"] = ("Give the owner customer_message exactly as written, as its own paragraph, so they can "
                          "forward it to the customer by text or email.")
        if link_is_local(result["accept_url"]):
            result["next"] += " Then tell them: " + LOCAL_LINK_NOTE
        return json.dumps(result)
    except Exception as exc:
        logger.exception("docket_send_quote failed")
        return _err(exc)


def _make_close_tool(ctx):
    def handler(args: dict, **kwargs) -> str:
        try:
            from . import runtime
            from .commands import _invoice_text
            from .invoices import quote_id_for
            inv = runtime.close_out(quote_id_for(runtime.store(), str((args or {}).get("quote_id"))),
                                    extra_words=str((args or {}).get("extra_work") or ""), actor="agent",
                                    surface="tool", llm=runtime.llm(ctx))
            out = {"ok": True, "invoice": inv["number"], "total": inv["total"], "handoff": inv["handoff"],
                   "text": _invoice_text(inv, owner_hint=False)}
            if inv.get("obol_payload"):
                out["obol_payload"] = inv["obol_payload"]
                out["next"] = OBOL_NEXT
            return json.dumps(out)
        except Exception as exc:
            logger.exception("docket_close_job failed")
            return _err(exc)
    return handler


def _jobs_tool(args: dict, **kwargs) -> str:
    try:
        from .commands import jobs
        return json.dumps({"ok": True, "text": jobs(str((args or {}).get("status") or ""))})
    except Exception as exc:
        return _err(exc)


REPORT_SCHEMAS = {
    "docket_followups": {
        "name": "docket_followups",
        "description": ("List quotes the customer has not answered for N+ days, oldest first, each with a suggested "
                        "nudge for the OWNER to send themselves. Read-only: Docket never contacts customers, so do not "
                        "offer to send the nudges. Deliver its text to the owner as returned."),
        "parameters": {"type": "object", "properties": {
            "stale_days": {"type": "integer", "description": "Minimum days waiting (default 5)"}}},
    },
    "docket_run_sheet": {
        "name": "docket_run_sheet",
        "description": ("Jobs scheduled for a day (default tomorrow) with addresses and the materials to load on the "
                        "truck. Read-only. Deliver its text to the owner as returned."),
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD; default tomorrow"}}},
    },
    "docket_pipeline": {
        "name": "docket_pipeline",
        "description": "Open quote value, quotes accepted this week and win rate. Read-only.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _report_tool(name):
    def handler(args: dict, **kwargs) -> str:
        try:
            from datetime import date as _date
            from . import reports, runtime
            from .service import accept_url
            store = runtime.store()
            if name == "docket_followups":
                from .settings import setting
                days = int((args or {}).get("stale_days") or setting("followup_after_days"))
                base = runtime.link_base()
                result = reports.followups(store, stale_days=max(1, days),
                                           accept_url=lambda rec: accept_url(store, rec, base))
            elif name == "docket_run_sheet":
                raw = str((args or {}).get("date") or "").strip()
                result = reports.run_sheet(store, day=_date.fromisoformat(raw) if raw else None)
            else:
                result = reports.pipeline(store)
            return json.dumps({"ok": True, **result})
        except Exception as exc:
            logger.exception("%s failed", name)
            return _err(exc)
    return handler


BLUEPRINT_SLOTS_COMMON = [
    {"name": "recurrence", "type": "weekdays", "label": "Repeat on", "default": "weekdays",
     "options": ["everyday", "weekdays", "weekends"]},
    {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
     "options": ["origin", "local"], "strict": False},
]
SILENT = " If the tool's text starts with 'Nothing to report', respond with [SILENT]."


def _register_blueprints(ctx):
    specs = [
        ("quote-followups", "Chase unsigned quotes",
         "Every morning, list quotes customers haven't answered, with a ready-to-send nudge for each. Never sends.",
         [{"name": "time", "type": "time", "label": "What time?", "default": "08:00"},
          {"name": "days", "type": "enum", "label": "Quotes unanswered for", "default": "5",
           "options": ["3", "5", "7", "14"]}],
         "Call the docket_followups tool with stale_days={days}. Deliver its text to me exactly as returned, "
         "without changing any number or adding prices." + SILENT),
        ("run-sheet", "Tomorrow's run sheet",
         "Each evening: tomorrow's jobs with addresses and the materials to load on the truck.",
         [{"name": "time", "type": "time", "label": "What time?", "default": "17:00"}],
         "Call the docket_run_sheet tool with no date. Deliver its text to me exactly as returned." + SILENT),
        ("weekly-pipeline", "Weekly pipeline",
         "Monday summary: open quote value, quotes won this week, win rate.",
         [{"name": "time", "type": "time", "label": "What time?", "default": "07:30"}],
         "Call the docket_pipeline tool. Deliver its text to me exactly as returned."),
    ]
    for key, title, description, slots, prompt in specs:
        recurrence = [{"name": "recurrence", "type": "weekdays", "label": "Repeat on",
                       "default": "weekdays" if key != "weekly-pipeline" else "everyday",
                       "options": ["everyday", "weekdays", "weekends"]}]
        schedule = "{minute} {hour} * * {dow}" if key != "weekly-pipeline" else "{minute} {hour} * * 1"
        all_slots = slots + (recurrence if key != "weekly-pipeline" else []) + [BLUEPRINT_SLOTS_COMMON[1]]
        try:
            ctx.register_automation_blueprint(key, title=title, description=description, category="work",
                                              schedule_template=schedule, prompt_template=prompt, slots=all_slots,
                                              tags=["docket", "trades", "quotes"])
        except Exception as exc:
            logger.info("Docket: blueprint %s not registered (%s)", key, exc)


def _pricebook_approval_text(preview_id) -> str:
    """The approval prompt lists Docket's own record of the changes, not the model's summary of them."""
    try:
        from . import runtime
        from .pbedit import pending
        plan = pending(runtime.store())
    except Exception:
        plan = None
    if plan is None or str(preview_id or "") != plan.token:
        return "Save a Docket price book change? (No matching preview: this will be refused.)"
    shown = "\n".join(f"  {c.label}" for c in plan.changes[:10])
    more = f"\n  … and {len(plan.changes) - 10} more" if len(plan.changes) > 10 else ""
    return f"Save {len(plan.changes)} change{'s' if len(plan.changes) != 1 else ''} to your Docket price book?\n{shown}{more}"


def _make_gate(ctx):
    def pre_tool_call(tool_name=None, args=None, **kwargs):
        if tool_name not in CRON_BLOCKED_TOOLS:
            return None
        try:
            if ctx.current_cron_execution() is not None:
                return {"action": "block",
                        "message": ("Docket never saves price book changes from scheduled runs."
                                    if tool_name == "docket_pricebook_save" else
                                    "Docket never closes jobs or makes invoices from scheduled runs."
                                    if tool_name == "docket_close_job" else
                                    "Docket never sends quotes from scheduled runs. Tap ✅ on the quote card.")}
        except Exception:
            return {"action": "block", "message": "Docket could not confirm this is an interactive session."}
        if tool_name not in GATED_TOOLS:
            return None
        if tool_name == "docket_pricebook_save":
            return {"action": "approve", "message": _pricebook_approval_text((args or {}).get("preview_id")),
                    "rule_key": f"docket:{tool_name}"}
        return {"action": "approve",
                "message": f"Send quote {(args or {}).get('quote_id', '?')} to the customer?",
                "rule_key": f"docket:{tool_name}"}
    return pre_tool_call


def _hint_hook(user_message: str = "", **kwargs):
    """Point the model at Docket's tools on quoting messages only (Hermes defers plugin tools)."""
    try:
        from . import runtime
        from .hints import hint_for
        from .intake import _find_items, _tokens
        mentions = False
        book, errors, _, _ = runtime.book()
        if book is not None and not errors:
            aliases = sorted(((tuple(_tokens(p)), c) for p, c in book.alias_index().items()), key=lambda kv: -len(kv[0]))
            mentions = bool(_find_items(user_message or "", aliases, set(book.assemblies)))
        text = hint_for(user_message or "", mentions_work=mentions)
        return {"context": text} if text else None
    except Exception:
        return None


def _docket_command(raw_args: str = "", **kwargs) -> str:
    return (f"Docket {VERSION}\n  /quote <job>   price and send a quote\n  /jobs          your pipeline\n"
            f"  /pricebook     show your price book, or change it in your own words")


def _card_sent_line(rec) -> str:
    """What the agent says after the card lands: the card went to the OWNER, nothing has gone to the customer yet."""
    name = (rec.customer or {}).get("name") or "the customer"
    if rec.complete:
        line = f"{rec.number} for {name} is ready to review. Tap ✅ Send when it looks right."
    else:
        line = f"{rec.number} for {name} has lines that need a price before it can be sent."
    return (f"The quote card is in this chat, waiting for the owner; nothing has gone to the customer yet. "
            f"Reply with exactly this line and nothing else: {line}")


def _start_accept_page() -> None:
    """Customer links sent before a gateway restart must keep working, so the accept page starts as soon as a
    messaging platform connects. Hermes runs platform factories only inside the gateway; starting is idempotent."""
    import threading
    from . import runtime
    threading.Thread(target=runtime.ensure_accept_server, name="docket-accept-start", daemon=True).start()


def _on_connect(channel):
    """The platform factory Hermes calls at connect time: wire the channel's buttons, then start the accept page."""
    def factory(native, adapter=None):
        try:
            channel.factory(native, adapter)
        finally:
            _start_accept_page()
    return factory


def register(ctx):
    from . import runtime
    from .commands import cli_main, cli_setup, docket_admin, jobs, make_pricebook_command, make_quote_command
    runtime.CTX = ctx

    # Docket's own model slot: job intake only maps words to price-book codes, so thinking is switched off
    # (reasoning models otherwise spend minutes before answering) and the owner can pin a faster model for it.
    try:
        ctx.register_auxiliary_task(
            "docket_intake", display_name="Docket job intake",
            description="Maps a job description to price-book codes. Never prices. A fast model works best.",
            defaults={"reasoning_effort": "none", "timeout": 90})
        runtime.INTAKE_TASK = "docket_intake"
    except Exception as exc:  # older hosts: use the main model with default settings
        logger.info("Docket: auxiliary task unavailable (%s); intake uses the main model", exc)

    ctx.register_tool(name="docket_price_preview", toolset="docket", schema=PRICE_PREVIEW_SCHEMA,
                      handler=_price_preview, description="Price work from your price book", emoji="🧾")
    ctx.register_tool(name="docket_quote", toolset="docket", schema=QUOTE_SCHEMA, handler=_make_quote_tool(ctx),
                      description="Create or revise a quote", emoji="🧾")
    ctx.register_tool(name="docket_send_quote", toolset="docket", schema=SEND_SCHEMA, handler=_send_tool,
                      description="Send a quote to the customer", emoji="📨")
    ctx.register_tool(name="docket_close_job", toolset="docket", schema=CLOSE_SCHEMA, handler=_make_close_tool(ctx),
                      description="Finish a job and make its invoice", emoji="🧾")
    ctx.register_tool(name="docket_pricebook_edit", toolset="docket", schema=PRICEBOOK_EDIT_SCHEMA,
                      handler=_make_pricebook_edit_tool(ctx), description="Preview price book changes", emoji="📒")
    ctx.register_tool(name="docket_pricebook_save", toolset="docket", schema=PRICEBOOK_SAVE_SCHEMA,
                      handler=_pricebook_save_tool, description="Save a previewed price book change", emoji="📒")
    ctx.register_tool(name="docket_jobs", toolset="docket", schema=JOBS_SCHEMA, handler=_jobs_tool,
                      description="Quotes and jobs by status", emoji="📋")
    for name, schema in REPORT_SCHEMAS.items():
        ctx.register_tool(name=name, toolset="docket", schema=schema, handler=_report_tool(name),
                          description=schema["description"].split(".")[0], emoji="📋")
    ctx.register_hook("pre_tool_call", _make_gate(ctx))
    ctx.register_hook("pre_llm_call", _hint_hook)
    try:                                   # voice notes: spell the owner's trade words the way the price book does
        from .voice import make_pre_transcription_hook
        ctx.register_hook("pre_transcription", make_pre_transcription_hook(runtime.book))
    except Exception as exc:               # hosts without the hook still transcribe; just without the hint
        logger.info("Docket: pre_transcription hook unavailable (%s)", exc)
    _register_blueprints(ctx)
    _register_channels(ctx)
    ctx.register_command("quote", make_quote_command(ctx), description="Price, revise or send a quote",
                         args_hint="<customer, ZIP, work> | show|revise|send|discard Q-1001")
    ctx.register_command("jobs", jobs, description="Your Docket quotes and jobs by status", args_hint="[status]")
    ctx.register_command("pricebook", make_pricebook_command(ctx),
                         description="Show your Docket price book, or change prices in your own words",
                         args_hint="[add <prices> | import <file> | remove | alias | save | cancel | undo | new]")
    ctx.register_command("docket", docket_admin, description="Docket help, accept page and link settings",
                         args_hint="[serve|link <url>]")
    ctx.register_cli_command(name="docket", help="Docket: quotes for trades businesses", setup_fn=cli_setup,
                             handler_fn=cli_main, description="Run Docket's customer accept page and demo tools")


def _register_channels(ctx):
    """Owner cards with buttons on Telegram, Discord and Slack. Refused under plugins.isolation: host, in
    which case Docket keeps working through slash commands and Hermes's own choice prompts."""
    import re
    from . import runtime
    from .channels import DiscordChannel, SlackChannel, TelegramChannel
    from .owner import register_channel
    channels = [TelegramChannel(runtime.tap), DiscordChannel(runtime.tap), SlackChannel(runtime.tap)]
    for channel in channels:
        try:
            ctx.register_platform_handler(channel.platform, _on_connect(channel))
            register_channel(channel)
        except Exception as exc:
            logger.info("Docket: %s buttons unavailable (%s)", channel.platform, exc)
    try:
        ctx.register_slack_action_handler(re.compile(r"^docket:"), channels[2].on_action)
    except Exception as exc:
        logger.info("Docket: Slack button handler unavailable (%s)", exc)
