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

PRICE_PREVIEW_SCHEMA = {
    "name": "docket_price_preview",
    "description": (
        "Price trade work from the owner's Docket price book without saving anything. "
        "Use it whenever the user asks what work would cost. Pass price-book item codes with quantities, "
        "the job's ZIP code, and flags (after_hours, weekend, emergency) when they apply. Put anything the "
        "user mentioned that has no code in 'unpriced'. Never state or estimate a price yourself: report "
        "exactly the lines and totals this tool returns, and say plainly when a line needs a price."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Price-book item codes with quantities (LABOR takes hours).",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Price-book code, e.g. WH50-REPLACE"},
                        "qty": {"type": "number", "description": "Quantity in the item's unit"},
                        "note": {"type": "string", "description": "Optional short description"},
                    },
                    "required": ["code", "qty"],
                },
            },
            "zip": {"type": "string", "description": "Job site ZIP code, for travel zones"},
            "flags": {"type": "array", "items": {"type": "string", "enum": ["after_hours", "weekend", "emergency"]}},
            "customer_tier": {"type": "string", "description": "Discount tier from the price book, e.g. member"},
            "unpriced": {"type": "array", "items": {"type": "string"},
                         "description": "Work mentioned that has no price-book code; the owner must price it"},
            "include_service_call": {"type": "boolean", "description": "Add the service call fee (default true)"},
        },
        "required": ["items"],
    },
}


def _load_book():
    from .pricebook import load_pricebook
    from .store_paths import pricebook_path
    path, is_demo = pricebook_path()
    book, errors = load_pricebook(path)
    return book, errors, path, is_demo


def _price_preview(args: dict, **kwargs) -> str:
    try:
        from .pricing import SpecError, price, render_text, spec_from_args
        book, errors, path, is_demo = _load_book()
        if errors:
            return json.dumps({"ok": False, "error": "price book has errors; fix them with /pricebook validate",
                               "errors": [str(e) for e in errors[:20]], "path": str(path)})
        try:
            spec = spec_from_args(args or {})
        except SpecError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        quote = price(book, spec)
        return json.dumps({"ok": True, "demo_price_book": is_demo, "quote": quote.to_dict(),
                           "text": render_text(quote)})
    except Exception as exc:  # handlers never raise
        logger.exception("docket_price_preview failed")
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _pricebook_command(raw_args: str = "", **kwargs) -> str:
    try:
        from .money import fmt_money
        book, errors, path, is_demo = _load_book()
        sub = (raw_args or "").strip().lower() or "show"
        label = " (demo price book)" if is_demo else ""
        if errors:
            return f"Price book {path}{label} has {len(errors)} error(s):\n" + "\n".join(f"  • {e}" for e in errors[:25])
        if sub == "validate":
            return (f"Price book OK{label}: {len(book.assemblies)} assemblies, {len(book.materials)} materials, "
                    f"{len(book.fees)} fees.\n{path}")
        lines = [f"{book.business.get('name', 'Price book')}{label} · {book.currency} · labor "
                 f"{fmt_money(book.labor_rate, book.currency)}/h · {book.tax_name} {book.tax_rate * 100:g}% on "
                 f"{', '.join(sorted(book.tax_applies_to)) or 'nothing'}"]
        lines.append("Assemblies:")
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
    return f"Docket {VERSION}. Try /pricebook to see the price book. More commands are coming: /quote, /jobs."


def register(ctx):
    ctx.register_tool(
        name="docket_price_preview",
        toolset="docket",
        schema=PRICE_PREVIEW_SCHEMA,
        handler=_price_preview,
        description="Price work from your price book",
        emoji="🧾",
    )
    ctx.register_hook("pre_tool_call", _make_gate(ctx))
    ctx.register_command("docket", _docket_command, description="Docket status and setup",
                         args_hint="[setup|demo|doctor]")
    ctx.register_command("pricebook", _pricebook_command, description="Show or validate your Docket price book",
                         args_hint="[show|validate]")
