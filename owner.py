"""Owner channel: quote cards with one-tap actions, shared by Telegram, Discord and Slack.

Platform adapters only draw buttons and relay taps. Everything a button does happens here, through the
same service functions the slash commands use, so the three platforms behave identically and are tested
without any network. Button data is "docket:<verb>:<quote>:<arg>" (well under Telegram's 64-byte limit).

Only the registered owner of a platform (set with /docket setup) can act on Docket buttons.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from .money import fmt_money

logger = logging.getLogger(__name__)

PREFIX = "docket"
PLATFORMS = ("telegram", "discord", "slack")


@dataclass
class Button:
    label: str
    data: str
    style: str = "secondary"      # primary | secondary | danger


@dataclass
class Card:
    caption: str                   # short line: shown under an image, or as the message preview
    text: str                      # full plain-text body for platforms without the image
    buttons: list[list[Button]] = field(default_factory=list)
    png: bytes | None = None
    quote_id: int | None = None


@dataclass
class ActionResult:
    ok: bool
    toast: str                     # brief acknowledgement shown on the tap
    card: Card | None = None       # replacement for the tapped message, if it changes
    followup: str = ""             # an extra message for the owner
    forward: str = ""              # a message on its own, ready to forward to the customer


# ---------------------------------------------------------------- owner registry

def set_owner(store, platform: str, *, chat_id: str, user_id: str, thread_id: str = "") -> None:
    store.set_meta(f"owner:{platform}", json.dumps({"chat_id": str(chat_id), "user_id": str(user_id),
                                                     "thread_id": str(thread_id or "")}))


def get_owner(store, platform: str) -> dict | None:
    raw = store.get_meta(f"owner:{platform}")
    return json.loads(raw) if raw else None


def owners(store) -> dict[str, dict]:
    return {p: o for p in PLATFORMS if (o := get_owner(store, p))}


# ---------------------------------------------------------------- cards

def _data(verb: str, quote_id: int, arg: str | int = "") -> str:
    return f"{PREFIX}:{verb}:{quote_id}:{arg}"


def quote_card(book, rec, *, with_png: bool = True) -> Card:
    from .service import quote_text
    total = fmt_money(Decimal(rec.total), book.currency)
    caption = f"{rec.number} v{rec.version} · {rec.customer['name']} · {total} · {rec.status.replace('_', ' ')}"
    buttons: list[list[Button]] = []
    if rec.status == "draft" and rec.complete:
        buttons = [[Button("✅ Send", _data("send", rec.id, rec.version), "primary"),
                    Button("✏️ Edit", _data("edit", rec.id, rec.version)),
                    Button("🗑 Discard", _data("discard", rec.id, rec.version), "danger")]]
    elif rec.status == "draft":
        buttons = [[Button("✏️ Add prices", _data("edit", rec.id, rec.version)),
                    Button("🗑 Discard", _data("discard", rec.id, rec.version), "danger")]]
    png = None
    if with_png:
        try:
            from .render import render_card_png
            png = render_card_png(book, rec)
        except Exception:
            logger.exception("Docket card image failed; sending text")
    return Card(caption=caption, text=quote_text(book, rec), buttons=buttons, png=png, quote_id=rec.id)


def booking_days(today: date, count: int = 2) -> list[date]:
    days, d = [], today
    while len(days) < count:
        d += timedelta(days=1)
        if d.weekday() != 6:          # skip Sundays
            days.append(d)
    return days


def event_card(book, rec, event: str, *, store=None, today: date | None = None) -> Card | None:
    total = fmt_money(Decimal(rec.total), book.currency)
    who = rec.customer["name"]
    if event == "accepted":
        today = today or datetime.now(timezone.utc).date()
        row = [Button(f"📅 {d:%a %b} {d.day}", _data("book", rec.id, d.strftime("%Y%m%d")), "primary")
               for d in booking_days(today)]
        row.append(Button("Pick a day", _data("pick", rec.id)))
        text = f"✅ {who} accepted {rec.number} ({total}). Book it:"
        return Card(caption=text, text=text, buttons=[row], quote_id=rec.id)
    if event == "declined":
        reason = ""
        for e in (store.events(rec.id) if store is not None else []):
            if e.get("to_status") == "declined" and e.get("detail"):
                reason = f" Reason: \"{e['detail']}\""
                break
        text = f"✖️ {who} declined {rec.number} ({total}).{reason} Revise with /quote revise {rec.number} <change>."
        return Card(caption=text, text=text, quote_id=rec.id)
    if event == "viewed":
        text = f"👀 {who} opened {rec.number} ({total})."
        return Card(caption=text, text=text, quote_id=rec.id)
    return None


# ---------------------------------------------------------------- actions

def parse(data: str) -> tuple[str, int, str]:
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != PREFIX or not parts[2].isdigit():
        raise ValueError("not a Docket action")
    return parts[1], int(parts[2]), (parts[3] if len(parts) > 3 else "")


def pricebook_card(plan) -> Card:
    """A price-book preview with Save / Cancel. Button data carries the preview's token, so an old card
    can never save a newer (or different) change."""
    n = len(plan.changes)
    caption = f"📒 Price book: {n} change{'s' if n != 1 else ''} to review"
    return Card(caption=caption, text=plan.preview(),
                buttons=[[Button("✅ Save", f"{PREFIX}:pbsave:0:{plan.token}", "primary"),
                          Button("Cancel", f"{PREFIX}:pbcancel:0:{plan.token}")]])


def perform(data: str, *, platform: str, user_id: str, store, book, outbox_root, base_url: Callable[[], str],
            render_pdf: Callable | None = None, pricebook: tuple[Callable, Callable] | None = None) -> ActionResult:
    """Run one button tap. Pure business logic; adapters handle drawing and network."""
    from .pbedit import EditError
    from .service import LOCAL_LINK_NOTE, ServiceError, customer_message, link_is_local, send_quote
    from .store import StoreError
    owner = get_owner(store, platform)
    if not owner:
        return ActionResult(False, "Run /docket setup in your chat with Hermes first.")
    if str(user_id) != owner["user_id"]:
        return ActionResult(False, "Only the business owner can use these buttons.")
    try:
        verb, quote_id, arg = parse(data)
        if verb in {"pbsave", "pbcancel"}:
            if pricebook is None:
                return ActionResult(False, "Price book edits aren't available here.")
            save, cancel = pricebook
            text = save(arg) if verb == "pbsave" else cancel(arg)
            done = Card(caption=("📒 " + text), text=("📒 " + text))
            return ActionResult(True, "Saved" if verb == "pbsave" else "Cancelled", card=done)
        rec = store.get(quote_id)
        if verb in {"send", "edit", "discard"} and arg and arg.isdigit() and int(arg) != rec.version:
            return ActionResult(False, f"That card is for v{arg}; {rec.number} is now v{rec.version}.",
                                card=quote_card(book, rec, with_png=False))
        if verb == "send":
            result = send_quote(store, book, quote_id, outbox_root=outbox_root, actor="owner", surface=platform,
                                base_url=base_url(), render_pdf=render_pdf)
            rec = store.get(quote_id)
            note = "Already sent." if result.get("already_sent") else "Sent."
            url = result["accept_url"]
            owner_note = (f"📨 {rec.number} is ready for {rec.customer['name']}. Forward the next message to them "
                          f"(text, WhatsApp or email); the PDF is in Docket's outbox.")
            if link_is_local(url):
                owner_note += "\n" + LOCAL_LINK_NOTE
            return ActionResult(True, f"{note} {rec.number}", card=_without_buttons(quote_card(book, rec)),
                                followup=owner_note, forward=customer_message(book, rec, url))
        if verb == "edit":
            return ActionResult(True, "Tell me the change",
                                followup=f"What should change on {rec.number}? Reply in your own words, e.g. "
                                         f"\"make it Saturday and add a shutoff valve\", or use "
                                         f"/quote revise {rec.number} <change>.")
        if verb == "discard":
            rec = store.transition(quote_id, "discarded", actor="owner", surface=platform)
            return ActionResult(True, f"{rec.number} discarded", card=_without_buttons(quote_card(book, rec)))
        if verb == "book":
            day = datetime.strptime(arg, "%Y%m%d").date()
            rec = store.transition(quote_id, "scheduled", actor="owner", surface=platform,
                                   scheduled_for=day.isoformat())
            text = f"📅 {rec.number} {rec.customer['name']} booked for {day:%A %b} {day.day}."
            return ActionResult(True, "Booked", card=Card(caption=text, text=text, quote_id=rec.id))
        if verb == "pick":
            return ActionResult(True, "Pick a day",
                                followup=f"Reply with the day, e.g. /quote book {rec.number} {booking_days(date.today(), 3)[-1]:%Y-%m-%d}")
        return ActionResult(False, "Unknown action")
    except (ServiceError, StoreError, EditError, ValueError) as exc:
        return ActionResult(False, str(exc)[:180])


def _without_buttons(card: Card) -> Card:
    card.buttons = []
    return card


# ---------------------------------------------------------------- delivery to owners

class Channel:
    """Base for platform adapters. send() is safe to call from any thread."""

    platform = ""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def live(self) -> bool:
        return self.loop is not None and self.loop.is_running()

    async def _send(self, owner: dict, card: Card) -> None:  # pragma: no cover - adapters implement
        raise NotImplementedError

    def send(self, owner: dict, card: Card, *, timeout: float = 20.0) -> bool:
        if not self.live:
            return False
        coro = self._send(owner, card)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:                       # already on the platform loop: don't block it
            self.loop.create_task(coro)
            return True
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)
            return True
        except Exception:
            logger.exception("Docket: sending a card on %s failed", self.platform)
            return False


CHANNELS: dict[str, Channel] = {}
_lock = threading.Lock()


def register_channel(channel: Channel) -> None:
    with _lock:
        CHANNELS[channel.platform] = channel


def push(store, card: Card, *, platforms: tuple[str, ...] | None = None) -> list[str]:
    """Send a card to every registered owner whose platform is connected. Returns where it went."""
    sent = []
    for platform, owner in owners(store).items():
        if platforms and platform not in platforms:
            continue
        channel = CHANNELS.get(platform)
        if channel and channel.send(owner, card):
            sent.append(platform)
    return sent
