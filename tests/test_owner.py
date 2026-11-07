from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import owner, service as svc  # noqa: E402
from docket.channels import DiscordChannel, SlackChannel, TelegramChannel  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.store import Store  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")
WORDS = "Dana Henderson, 14 Maple St 62707, water heater, permit, haul away"


@pytest.fixture
def env(tmp_path):
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    owner.set_owner(store, "telegram", chat_id="555", user_id="42")
    rec = svc.draft_from_words(store, BOOK, WORDS).record

    def tap(data, platform="telegram", user="42"):
        return owner.perform(data, platform=platform, user_id=user, store=store, book=BOOK, outbox_root=tmp_path,
                             base_url=lambda: "http://127.0.0.1:8787")
    return store, rec, tap


def test_card_buttons_fit_platform_limits(env):
    store, rec, _ = env
    card = owner.quote_card(BOOK, rec, with_png=False)
    labels = [b.label for row in card.buttons for b in row]
    assert labels == ["✅ Send", "✏️ Edit", "🗑 Discard"]
    assert all(len(b.data.encode()) <= 64 for row in card.buttons for b in row)     # Telegram callback limit
    assert "$1,972.36" in card.caption and "Dana Henderson" in card.text


def test_only_the_owner_can_press(env):
    store, rec, tap = env
    data = owner.quote_card(BOOK, rec, with_png=False).buttons[0][0].data
    assert not tap(data, user="999").ok and store.get(rec.id).status == "draft"
    assert not tap(data, platform="slack").ok                                         # no Slack owner yet
    r = tap(data)
    assert r.ok and store.get(rec.id).status == "sent" and "/q/" in r.forward and r.card.buttons == []
    assert r.forward.startswith("Hi Dana, here's your quote") and "only opens on this computer" in r.followup
    assert r.card.png and r.card.png.startswith(b"\x89PNG")                    # fresh image with the SENT badge


def test_stale_card_after_revision_is_refused(env):
    store, rec, tap = env
    old = owner.quote_card(BOOK, rec, with_png=False).buttons[0][0].data
    svc.revise_from_words(store, BOOK, rec.id, "add a shutoff valve")
    r = tap(old)
    assert not r.ok and "v2" in r.toast and store.get(rec.id).status == "draft"


def test_accept_card_books_a_day_skipping_sunday(env):
    store, rec, tap = env
    tap(owner.quote_card(BOOK, rec, with_png=False).buttons[0][0].data)
    store.transition(rec.id, "accepted", actor="customer")
    card = owner.event_card(BOOK, store.get(rec.id), "accepted", store=store, today=date(2026, 11, 6))   # Friday
    labels = [b.label for b in card.buttons[0]]
    assert labels == ["📅 Sat Nov 7", "📅 Mon Nov 9", "Pick a day"]
    r = tap(card.buttons[0][1].data)
    assert r.ok and store.get(rec.id).status == "scheduled" and store.get(rec.id).scheduled_for == "2026-11-09"


def test_decline_card_carries_reason(env):
    store, rec, tap = env
    tap(owner.quote_card(BOOK, rec, with_png=False).buttons[0][0].data)
    store.transition(rec.id, "declined", actor="customer", detail="Too expensive")
    card = owner.event_card(BOOK, store.get(rec.id), "declined", store=store)
    assert "Too expensive" in card.text


# ---------------------------------------------------------------- adapters with real SDKs, fake connections

class LoopThread:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self

    def run(self, fn):
        async def wrapper():
            return fn()
        return asyncio.run_coroutine_threadsafe(wrapper(), self.loop).result(5)

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(5)

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append((name, kwargs))
            return SimpleNamespace(ok=True)
        return method


def test_telegram_send_and_tap(env):
    from telegram import InlineKeyboardMarkup
    store, rec, tap = env
    handlers = []
    app = SimpleNamespace(add_handler=handlers.append, bot=Recorder())
    ch = TelegramChannel(lambda data, platform, user: tap(data, platform, user))
    with LoopThread() as lt:
        lt.run(lambda: ch.factory(app, None))
        assert handlers and handlers[0].pattern.pattern == "^docket:"
        card = owner.quote_card(BOOK, rec)
        assert ch.send({"chat_id": "555", "user_id": "42"}, card)               # called from another thread
        name, kw = app.bot.calls[-1]
        assert name == "send_photo" and isinstance(kw["reply_markup"], InlineKeyboardMarkup)
        assert kw["reply_markup"].inline_keyboard[0][0].callback_data == card.buttons[0][0].data
        q = Recorder()
        q.data, q.from_user = card.buttons[0][0].data, SimpleNamespace(id=42)
        q.message = Recorder()
        q.message.photo = [1]
        lt.call(ch.on_callback(SimpleNamespace(callback_query=q), None))
    assert store.get(rec.id).status == "sent"
    assert [c[0] for c in q.calls] == ["answer", "edit_message_media"]
    media = q.calls[1][1]["media"]
    assert media.caption.endswith("· sent")                                     # image redrawn with new status
    assert q.message.calls[0][0] == "reply_text"


def test_slack_blocks_and_action(env):
    store, rec, tap = env
    owner.set_owner(store, "slack", chat_id="C1", user_id="U1")
    ch = SlackChannel(lambda data, platform, user: tap(data, platform, user))
    app = SimpleNamespace(client=Recorder())
    card = owner.quote_card(BOOK, rec, with_png=False)
    blocks = SlackChannel.blocks(card)
    assert blocks[1]["elements"][0]["action_id"] == card.buttons[0][0].data
    acks = []

    async def ack():
        acks.append(1)
    with LoopThread() as lt:
        lt.run(lambda: ch.factory(app, None))
        body = {"user": {"id": "U1"}, "channel": {"id": "C1"}, "message": {"ts": "1.2"}}
        lt.call(ch.on_action(ack, body, {"action_id": card.buttons[0][0].data, "value": card.buttons[0][0].data}))
    assert acks == [1] and store.get(rec.id).status == "sent"
    assert [c[0] for c in app.client.calls] == ["chat_update", "chat_postMessage", "chat_postMessage"]


def test_discord_view_and_interaction(env):
    store, rec, tap = env
    owner.set_owner(store, "discord", chat_id="77", user_id="9")
    ch = DiscordChannel(lambda data, platform, user: tap(data, platform, user))
    listeners = []
    bot = SimpleNamespace(add_listener=lambda fn, name: listeners.append(name))
    card = owner.quote_card(BOOK, rec, with_png=False)
    with LoopThread() as lt:
        lt.run(lambda: ch.factory(bot, None))
        assert listeners == ["on_interaction"]
        view = lt.run(lambda: ch.view(card))
        assert [item.custom_id for item in view.children] == [b.data for b in card.buttons[0]]
        message, followup, response = Recorder(), Recorder(), Recorder()
        message.attachments = []
        inter = SimpleNamespace(data={"custom_id": card.buttons[0][0].data}, user=SimpleNamespace(id=9),
                                response=response, message=message, followup=followup)
        lt.call(ch.on_interaction(inter))
        other = SimpleNamespace(data={"custom_id": "approve:123"}, user=SimpleNamespace(id=9),
                                response=Recorder(), message=None, followup=Recorder())
        lt.call(ch.on_interaction(other))                                       # not ours: untouched
    assert store.get(rec.id).status == "sent"
    assert [c[0] for c in response.calls] == ["defer"] and message.calls[0][0] == "edit"
    assert other.response.calls == []
