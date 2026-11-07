from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import runtime, service as svc  # noqa: E402
from docket.links import read_token  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.settings import apply_business, setting  # noqa: E402
from docket.store import Store  # noqa: E402

DEMO = Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml"


class FakeCtx:
    def __init__(self, **values):
        self.values = values

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def set_config(self, key, value):
        self.values[key] = value


@pytest.fixture
def ctx(monkeypatch):
    fake = FakeCtx()
    monkeypatch.setattr(runtime, "CTX", fake)
    return fake


def test_defaults_without_hermes(monkeypatch):
    monkeypatch.setattr(runtime, "CTX", None)
    assert setting("quote_valid_days") == 30 and setting("invoice_due_days") == 14 and setting("public_link") == ""


def test_numbers_are_clamped_and_junk_falls_back(ctx):
    ctx.values.update(quote_valid_days=10_000, followup_after_days=0, invoice_due_days="soon")
    assert setting("quote_valid_days") == 365 and setting("followup_after_days") == 1 and setting("invoice_due_days") == 14


def test_business_details_override_the_price_book(ctx):
    ctx.values.update(business_name="Rivera Plumbing", business_phone="", payment_terms="Net 7")
    book, _ = load_pricebook(DEMO)
    apply_business(book)
    assert book.business["name"] == "Rivera Plumbing" and book.business["payment_terms"] == "Net 7"
    assert book.business["phone"] == "(555) 010-4477"              # blank setting keeps the price book's


def test_link_expiry_and_public_link_follow_settings(ctx, tmp_path):
    ctx.values.update(quote_valid_days=7, public_link="https://quotes.example.com/")
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    book, _ = load_pricebook(DEMO)
    d = svc.draft_from_words(store, book, "Dana Henderson, 14 Maple St 62707, water heater")
    url = svc.accept_url(store, store.get(d.record.id))
    assert url.startswith("https://quotes.example.com/q/")
    token = url.rsplit("/", 1)[1]
    assert read_token(store.link_secret(), token)                    # valid now
    with pytest.raises(Exception):
        read_token(store.link_secret(), token, now=time.time() + 8 * 86400)   # expired after 7 days


def test_docket_link_command_writes_the_setting(ctx, monkeypatch):
    from docket import commands
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    runtime.reset_for_tests(store)
    try:
        commands.docket_admin("link https://q.example.com/")
        assert ctx.values["public_link"] == "https://q.example.com" and runtime.public_link() == "https://q.example.com"
        commands.docket_admin("link local")
        assert runtime.public_link() == ""
    finally:
        runtime.reset_for_tests(None)
