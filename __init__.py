"""Docket — Hermes plugin registration.

register() stays import-light: heavy libraries (reportlab, Pillow, telegram) are imported
inside handlers so loading never risks the plugin load deadline.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

PLUGIN_ID = "docket"
VERSION = "0.1.0"
GATED_TOOLS = {"docket_send_quote"}

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
        "or ZIP, and the work. Docket maps the words to the owner's price book and prices every line in code. "
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

JOBS_SCHEMA = {
    "name": "docket_jobs",
    "description": "List the owner's quotes and jobs grouped by status (draft, sent, accepted, scheduled, ...).",
    "parameters": {"type": "object", "properties": {
        "status": {"type": "string", "description": "Optional comma-separated statuses to include"}}},
}


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
            words = str((args or {}).get("description") or "")
            if (args or {}).get("quote_id"):
                d = revise_from_words(store, book, parse_quote_id(args["quote_id"]), words, llm=runtime.llm(ctx),
                                      actor="agent", surface="tool")
            else:
                d = draft_from_words(store, book, words, llm=runtime.llm(ctx), actor="agent", surface="tool")
            rec = d.record
            surface = ""
            try:
                surface = ctx.session_surface() or ""
            except Exception:
                pass
            if runtime.push_quote_card(rec, surface):
                return json.dumps({"ok": True, "quote_id": rec.number, "version": rec.version, "status": rec.status,
                                   "card_sent": True, "complete": rec.complete, "notes": d.intake.notes,
                                   "next": (f"The quote card for {rec.number} with Send/Edit/Discard buttons was just "
                                            "sent to this chat. Tell the owner in one short line; do not repeat the "
                                            "prices.")})
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
        from .service import parse_quote_id, send_quote
        result = send_quote(runtime.store(), runtime.require_book(), parse_quote_id(str((args or {}).get("quote_id"))),
                            outbox_root=runtime.outbox_root(), actor="agent", surface="tool", render_pdf=_pdf_renderer(),
                            base_url=runtime.ensure_accept_server())
        return json.dumps(result)
    except Exception as exc:
        logger.exception("docket_send_quote failed")
        return _err(exc)


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
                days = int((args or {}).get("stale_days") or 5)
                base = store.get_meta("public_base_url") or None
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


def _pricebook_command(raw_args: str = "", **kwargs) -> str:
    try:
        from . import runtime
        from .money import fmt_money, fmt_qty
        book, errors, path, is_demo = runtime.book()
        sub = (raw_args or "").strip().lower() or "show"
        label = " (demo price book)" if is_demo else ""
        if errors:
            return f"Price book {path}{label} has {len(errors)} error(s):\n" + "\n".join(f"  • {e}" for e in errors[:25])
        if sub == "validate":
            return (f"Price book OK{label}: {len(book.assemblies)} assemblies, {len(book.materials)} materials, "
                    f"{len(book.fees)} fees.\n{path}")
        lines = [f"{book.business.get('name', 'Price book')}{label} · {book.currency} · labor "
                 f"{fmt_money(book.labor_rate, book.currency)}/h · {book.tax_name} {fmt_qty(book.tax_rate * 100)}% on "
                 f"{', '.join(sorted(book.tax_applies_to)) or 'nothing'}", "Assemblies:"]
        lines += [f"  {a.code:<18} {a.name} (per {a.unit})" for a in book.assemblies.values()]
        lines.append("Fees:")
        lines += [f"  {f.code:<18} {f.name} {fmt_money(f.amount, book.currency)}" for f in book.fees.values()]
        lines.append(f"File: {path}")
        return "\n".join(lines)
    except Exception as exc:
        logger.exception("/pricebook failed")
        return f"Docket could not read the price book: {type(exc).__name__}: {exc}"


def _make_gate(ctx):
    def pre_tool_call(tool_name=None, args=None, **kwargs):
        if tool_name not in GATED_TOOLS:
            return None
        try:
            if ctx.current_cron_execution() is not None:
                return {"action": "block",
                        "message": "Docket never sends quotes from scheduled runs. Tap ✅ on the quote card."}
        except Exception:
            return {"action": "block", "message": "Docket could not confirm this is an interactive session."}
        return {"action": "approve",
                "message": f"Send quote {(args or {}).get('quote_id', '?')} to the customer?",
                "rule_key": f"docket:{tool_name}"}
    return pre_tool_call


def _docket_command(raw_args: str = "", **kwargs) -> str:
    return (f"Docket {VERSION}\n  /quote <job>   price and send a quote\n  /jobs          your pipeline\n"
            f"  /pricebook     show or validate your price book")


def register(ctx):
    from . import runtime
    from .commands import cli_main, cli_setup, docket_admin, jobs, make_quote_command

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
    ctx.register_tool(name="docket_jobs", toolset="docket", schema=JOBS_SCHEMA, handler=_jobs_tool,
                      description="Quotes and jobs by status", emoji="📋")
    for name, schema in REPORT_SCHEMAS.items():
        ctx.register_tool(name=name, toolset="docket", schema=schema, handler=_report_tool(name),
                          description=schema["description"].split(".")[0], emoji="📋")
    ctx.register_hook("pre_tool_call", _make_gate(ctx))
    _register_blueprints(ctx)
    _register_channels(ctx)
    ctx.register_command("quote", make_quote_command(ctx), description="Price, revise or send a quote",
                         args_hint="<customer, ZIP, work> | show|revise|send|discard Q-1001")
    ctx.register_command("jobs", jobs, description="Your Docket quotes and jobs by status", args_hint="[status]")
    ctx.register_command("pricebook", _pricebook_command, description="Show or validate your Docket price book",
                         args_hint="[show|validate]")
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
            ctx.register_platform_handler(channel.platform, channel.factory)
            register_channel(channel)
        except Exception as exc:
            logger.info("Docket: %s buttons unavailable (%s)", channel.platform, exc)
    try:
        ctx.register_slack_action_handler(re.compile(r"^docket:"), channels[2].on_action)
    except Exception as exc:
        logger.info("Docket: Slack button handler unavailable (%s)", exc)
