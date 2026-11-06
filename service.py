"""Docket business logic shared by slash commands, tools, Telegram buttons and the accept page.

Every function takes explicit dependencies (store, price book, data dir, model) so it runs the same in
Hermes and in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .intake import IntakeResult, intake
from .links import LinkError, make_token, read_token
from .money import fmt_money
from .pricebook import PriceBook
from .pricing import Quote, price, render_text, spec_from_args
from .store import ACCEPTABLE, QuoteRecord, Store, StoreError

DEFAULT_BASE_URL = "http://127.0.0.1:8787"


class ServiceError(ValueError):
    pass


@dataclass
class Drafted:
    record: QuoteRecord
    quote: Quote
    intake: IntakeResult


def parse_quote_id(text: str) -> int:
    t = (text or "").strip().upper().removeprefix("Q-").removeprefix("Q")
    if not t.isdigit():
        raise ServiceError(f"{text!r} is not a quote number (try Q-1001)")
    return int(t)


def draft_from_words(store: Store, book: PriceBook, words: str, *, llm: Any = None, actor: str = "owner",
                     surface: str = "", dedupe_key: str = "") -> Drafted:
    result = intake(book, words, llm)
    if not result.args["items"]:
        missing = "; ".join(result.args["unpriced"][:5])
        raise ServiceError("No work from the price book was found, so no quote was made."
                           + (f" Not in the price book: {missing}." if missing else "")
                           + " Name the job (e.g. 'water heater swap') or add it to the price book.")
    if dedupe_key:                                 # same chat asked for the same job again: reuse the draft
        import hashlib
        import json as _json
        digest = hashlib.sha256(_json.dumps(result.args, sort_keys=True).encode()).hexdigest()[:16]
        prev = store.get_meta(f"dedupe:{dedupe_key}")
        if prev and prev.split(":")[0] == digest:
            try:
                rec = store.get(int(prev.split(":")[1]))
                if rec.status == "draft":
                    result.notes.append(f"Same job as {rec.number}; reused that draft instead of making a duplicate.")
                    return Drafted(rec, price(book, spec_from_args(rec.spec)), result)
            except (StoreError, ValueError):
                pass
    name = result.customer_name or "New customer"
    customer = store.upsert_customer(name, zip=result.args.get("zip", ""), address=result.site, email=result.email)
    if customer.get("tier") and not result.args.get("customer_tier"):
        result.args["customer_tier"] = customer["tier"]
    quote = price(book, spec_from_args(result.args))
    rec = store.create_quote(customer["id"], result.site, result.args.get("zip", ""), result.args, quote.to_dict(),
                             actor=actor, surface=surface)
    if dedupe_key:
        store.set_meta(f"dedupe:{dedupe_key}", f"{digest}:{rec.id}")
    return Drafted(rec, quote, result)


def revise_from_words(store: Store, book: PriceBook, quote_id: int, change: str, *, llm: Any = None,
                      actor: str = "owner", surface: str = "") -> Drafted:
    current = store.get(quote_id)
    if llm is not None:
        result = intake(book, change, llm, previous=current.spec)
        if result.source == "parser":           # model failed: apply the change additively
            result = _merge(current.spec, intake(book, change, None))
    else:
        result = _merge(current.spec, intake(book, change, None))
    quote = price(book, spec_from_args(result.args))
    rec = store.revise(quote_id, result.args, quote.to_dict(), note=change.strip(), actor=actor, surface=surface)
    return Drafted(rec, quote, result)


def _merge(previous: dict[str, Any], addition: IntakeResult) -> IntakeResult:
    merged = {k: (list(v) if isinstance(v, list) else v) for k, v in previous.items()}
    qty: dict[str, Decimal] = {}
    for it in merged.get("items", []):
        qty[it["code"]] = qty.get(it["code"], Decimal(0)) + Decimal(str(it["qty"]))
    for it in addition.args["items"]:
        qty[it["code"]] = qty.get(it["code"], Decimal(0)) + Decimal(str(it["qty"]))
    merged["items"] = [{"code": c, "qty": str(q)} for c, q in qty.items()]
    merged["flags"] = sorted(set(merged.get("flags", [])) | set(addition.args["flags"]))
    if addition.args.get("zip"):
        merged["zip"] = addition.args["zip"]
    if addition.args.get("customer_tier"):
        merged["customer_tier"] = addition.args["customer_tier"]
    merged["unpriced"] = list(merged.get("unpriced", [])) + list(addition.args["unpriced"])
    return IntakeResult(args=merged, source="parser", notes=["Change applied as an addition to the quote."])


def accept_url(store: Store, rec: QuoteRecord, base_url: str | None = None) -> str:
    token = make_token(store.link_secret(), rec.id, rec.version)
    base = (base_url or store.get_meta("public_base_url") or DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/q/{token}"


def send_quote(store: Store, book: PriceBook, quote_id: int, *, outbox_root: Path, actor: str, surface: str = "",
               base_url: str | None = None, render_pdf: Any = None) -> dict[str, Any]:
    """Send the current version once. Repeats (double taps) return the earlier send without re-sending."""
    with store.lock:   # one send at a time: a double tap waits, then sees the first send
        return _send_locked(store, book, quote_id, outbox_root=outbox_root, actor=actor, surface=surface,
                            base_url=base_url, render_pdf=render_pdf)


def _send_locked(store: Store, book: PriceBook, quote_id: int, *, outbox_root: Path, actor: str, surface: str,
                 base_url: str | None, render_pdf: Any) -> dict[str, Any]:
    rec = store.get(quote_id)
    if store.has_send(rec.id, rec.version, "outbox"):
        return {"ok": True, "already_sent": True, "number": rec.number, "version": rec.version,
                "status": rec.status, "accept_url": accept_url(store, rec, base_url)}
    if rec.status != "draft":
        raise ServiceError(f"{rec.number} is {rec.status}; only a draft can be sent")
    if not rec.complete:
        raise ServiceError(f"{rec.number} has lines that need a price. Fix them with /quote revise {rec.number} ...")
    url = accept_url(store, rec, base_url)
    text = quote_text(book, rec)
    attachments = {}
    if render_pdf is not None:
        try:
            attachments[f"{rec.number}-v{rec.version}.pdf"] = render_pdf(book, rec, url)
        except Exception as exc:  # the email still goes; the owner sees why the PDF is missing
            text += f"\n\n(PDF could not be generated: {type(exc).__name__})"
    body = (f"Hi {rec.customer['name'].split()[0]},\n\nHere is your quote {rec.number} from "
            f"{book.business.get('name', 'us')}.\n\n{text}\n\nTo accept, open: {url}\n\n"
            f"{book.business.get('name', '')}\n{book.business.get('phone', '')}")
    from .outbox import write_outbox
    folder = write_outbox(outbox_root, number=rec.number, version=rec.version, business=book.business,
                          customer=rec.customer, subject=f"Your quote {rec.number} from {book.business.get('name', '')}",
                          body=body, attachments=attachments)
    store.record_send(rec.id, rec.version, "outbox", ref=str(folder))
    rec = store.transition(rec.id, "sent", actor=actor, surface=surface, detail=f"outbox {folder.name}")
    return {"ok": True, "already_sent": False, "number": rec.number, "version": rec.version, "status": rec.status,
            "accept_url": url, "outbox": str(folder)}


def customer_action(store: Store, token: str, action: str, *, surface: str = "accept-page", note: str = "") -> QuoteRecord:
    """Accept / decline / mark viewed from a customer link. The token must name the CURRENT version."""
    try:
        quote_id, version = read_token(store.link_secret(), token)
    except LinkError as exc:
        raise ServiceError(str(exc)) from None
    rec = store.get(quote_id)
    if rec.version != version:
        raise ServiceError(f"This quote was updated. Please use the link for version {rec.version}.")
    if action == "view":
        return store.transition(quote_id, "viewed", actor="customer", surface=surface, expect_version=version) \
            if rec.status == "sent" else rec
    if action in {"accept", "decline"}:
        target = "accepted" if action == "accept" else "declined"
        if rec.status == target:
            return rec
        if rec.status not in ACCEPTABLE:
            raise ServiceError(f"This quote is {rec.status} and can no longer be {target}.")
        return store.transition(quote_id, target, actor="customer", surface=surface, detail=note, expect_version=version)
    raise ServiceError(f"unknown action {action!r}")


def quote_text(book: PriceBook, rec: QuoteRecord) -> str:
    q = rec.quote
    cur = q.get("currency", book.currency)
    lines = [f"{rec.number} v{rec.version} · {rec.customer['name']}" + (f" · {rec.site}" if rec.site else "")]
    for l in q["lines"]:
        amount = "NEEDS PRICE" if l["kind"] == "needs_price" else fmt_money(Decimal(l["amount"]), cur)
        qty = Decimal(l["qty"]).normalize()
        unit = f" {l['unit']}" if l["unit"] and l["unit"] != "each" else ""
        count = f" × {format(qty, 'f')}{unit}" if (qty != 1 or unit) else ""
        lines.append(f"• {l['description']}{count} — {amount}")
    lines.append(f"Subtotal {fmt_money(Decimal(q['subtotal']), cur)} · {q['tax_name']} "
                 f"{fmt_money(Decimal(q['tax']), cur)} · Total {fmt_money(Decimal(q['total']), cur)}")
    if q.get("needs_price"):
        lines.append("Needs a price before sending: " + "; ".join(q["needs_price"]))
    return "\n".join(lines)


def owner_text(book: PriceBook, rec: QuoteRecord) -> str:
    """Quote with the rule behind every line, for the owner's eyes."""
    from .pricing import Quote as _Q  # noqa: F401  (type only)
    out = [quote_text(book, rec), ""]
    for l in rec.quote["lines"]:
        out.append(f"  {l['code']}: {l['rule']}")
    return "\n".join(out)


def jobs_text(store: Store, statuses: set[str] | None = None) -> str:
    recs = store.list(statuses)
    if not recs:
        return "No quotes yet. Start one with /quote <customer, ZIP, what needs doing>."
    order = ["draft", "sent", "viewed", "accepted", "scheduled", "in_progress", "done", "invoiced", "paid",
             "declined", "expired", "discarded", "cancelled"]
    out = []
    for status in order:
        group = [r for r in recs if r.status == status]
        if not group:
            continue
        total = sum((Decimal(r.total) for r in group), Decimal(0))
        out.append(f"{status.replace('_', ' ').title()} ({len(group)}, {fmt_money(total)})")
        for r in group:
            when = f" · {r.scheduled_for}" if r.scheduled_for else ""
            out.append(f"  {r.number} v{r.version}  {r.customer['name']:<20} {fmt_money(Decimal(r.total)):>11}{when}")
    return "\n".join(out)


__all__ = ["ServiceError", "StoreError", "draft_from_words", "revise_from_words", "send_quote", "customer_action",
           "quote_text", "owner_text", "jobs_text", "parse_quote_id", "accept_url", "render_text"]
