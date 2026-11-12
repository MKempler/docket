"""docket_close_job never calls another plugin's tool, and never runs from scheduled jobs."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import docket  # noqa: E402
from docket import runtime  # noqa: E402
from docket import service as svc  # noqa: E402
from docket.commands import _invoice_text  # noqa: E402
from docket.store import Store  # noqa: E402

WORDS = "Dana Henderson dana@example.com, 14 Maple St 62707, water heater, permit, haul away"


class FakeCtx:
    """A PluginContext stand-in: Obol is installed; any direct tool dispatch is a test failure."""

    def __init__(self, obol=True, cron=None):
        self.obol, self.cron, self.dispatched = obol, cron, []

    def has_plugin(self, name):
        return self.obol and name == "obol"

    def dispatch_tool(self, name, args):              # must never be reached
        self.dispatched.append(name)
        raise AssertionError(f"Docket called {name} directly")

    def current_cron_execution(self):
        return self.cron


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    runtime.reset_for_tests(store)
    monkeypatch.setattr(runtime, "outbox_root", lambda: tmp_path)
    monkeypatch.setattr(runtime, "ensure_accept_server", lambda: "http://127.0.0.1:8787")
    book = runtime.require_book()

    def booked(words=WORDS):
        d = svc.draft_from_words(store, book, words)
        svc.send_quote(store, book, d.record.id, outbox_root=tmp_path, actor="owner")
        for status in ("accepted", "scheduled"):
            store.transition(d.record.id, status, actor="test")
        return store.get(d.record.id)

    yield store, booked
    runtime.reset_for_tests(None)


def call_close(ctx, monkeypatch, number):
    monkeypatch.setattr(runtime, "CTX", ctx)
    return json.loads(docket._make_close_tool(ctx)({"quote_id": number}))


def test_close_tool_returns_obol_payload_and_calls_nothing(env, monkeypatch):
    store, booked = env
    rec, ctx = booked(), FakeCtx(obol=True)
    out = call_close(ctx, monkeypatch, rec.number)
    assert out["ok"] and ctx.dispatched == []
    assert out["handoff"] == "obol_ready" and out["obol_payload"]["customer_email"] == "dana@example.com"
    assert out["obol_payload"]["send"] is False and "NOT created anything in Obol" in out["next"]
    assert "obol_create_invoice" in out["next"] and "ask your agent" not in out["text"]
    again = call_close(ctx, monkeypatch, out["invoice"])                 # owner asks later: "send INV-1001 to Obol"
    assert again["invoice"] == out["invoice"] and again["obol_payload"] == out["obol_payload"] and ctx.dispatched == []
    assert "not one of your invoices" in call_close(ctx, monkeypatch, "INV-9999")["error"]


def test_close_tool_without_obol_is_local_only(env, monkeypatch):
    _, booked = env
    rec = booked()
    out = call_close(FakeCtx(obol=False), monkeypatch, rec.number)
    assert out["handoff"] == "local" and "obol_payload" not in out and "next" not in out and "Obol" not in out["text"]


def test_slash_command_and_desktop_paths_call_nothing(env, monkeypatch):
    store, booked = env
    ctx = FakeCtx(obol=True)
    monkeypatch.setattr(runtime, "CTX", ctx)
    inv = runtime.close_out(booked().id, surface="telegram")             # what /quote done and Desktop call
    assert ctx.dispatched == [] and inv["obol_payload"] and "ask your agent" in _invoice_text(inv)


def test_close_job_is_blocked_from_scheduled_runs_but_not_gated_interactively():
    gate = docket._make_gate(FakeCtx(cron={"job": "nightly"}))
    blocked = gate(tool_name="docket_close_job", args={"quote_id": "Q-1001"})
    assert blocked["action"] == "block" and "scheduled" in blocked["message"]
    assert docket._make_gate(FakeCtx(cron=None))(tool_name="docket_close_job", args={}) is None
    send = docket._make_gate(FakeCtx(cron=None))(tool_name="docket_send_quote", args={"quote_id": "Q-1"})
    assert send["action"] == "approve"                                  # existing gates unchanged
    assert gate(tool_name="docket_send_quote", args={})["action"] == "block"
    assert docket._make_gate(FakeCtx())(tool_name="docket_jobs", args={}) is None


def test_accept_page_starts_when_a_platform_connects_not_from_argv(monkeypatch):
    started = []
    monkeypatch.setattr(runtime, "ensure_accept_server", lambda: started.append(1) or "http://127.0.0.1:8787")

    class Chan:
        wired = []

        def factory(self, native, adapter=None):
            self.wired.append((native, adapter))

    class InlineThread:                                                  # run the start-up inline for the test
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("threading.Thread", InlineThread)
    chan = Chan()
    docket._on_connect(chan)("native-app", "adapter")
    assert chan.wired == [("native-app", "adapter")] and started == [1]
    assert not hasattr(docket, "_in_gateway")
