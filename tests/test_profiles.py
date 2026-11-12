"""Two Hermes profiles in one process: separate stores, links that name their profile, and an accept page that
reaches the right quotes and business with no profile active (as when a customer clicks)."""
from __future__ import annotations

import contextvars
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import runtime, store_paths  # noqa: E402
from docket import service as svc  # noqa: E402
from docket.store import DB_FILENAME, Store  # noqa: E402

HOME: contextvars.ContextVar = contextvars.ContextVar("hermes_home")   # stands in for Hermes's profile override
BUSINESS = {"root": "Copperline Plumbing", "b": "Brightside Electric"}


class Ctx:
    """get_config follows the active profile, like Hermes's plugin settings."""

    def get_config(self, key, default=None):
        if key != "business_name":
            return default
        home = HOME.get(None)
        return BUSINESS["b" if home is not None and home.name == "b" else "root"]


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    root = tmp_path / "hermes"

    def data_dir():
        home = HOME.get(None) or root                       # no override = the process's own (default) home
        d = home / "plugin-data" / "docket"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(runtime, "data_dir", data_dir)
    monkeypatch.setattr(store_paths, "data_dir", data_dir)
    monkeypatch.setattr(runtime, "open_store",
                        lambda: Store(sqlite3.connect(data_dir() / DB_FILENAME, check_same_thread=False)))
    monkeypatch.setattr(runtime, "CTX", Ctx())
    runtime.reset_for_tests(None)
    in_b = contextvars.copy_context()
    in_b.run(HOME.set, root / "profiles" / "b")
    yield root, in_b
    runtime.reset_for_tests(None)


def send(words, tmp_path):
    st, bk = runtime.store(), runtime.require_book()
    d = svc.draft_from_words(st, bk, words)
    return st, svc.send_quote(st, bk, d.record.id, outbox_root=tmp_path, actor="owner", base_url=runtime.link_base())


def test_each_profile_has_its_own_store_and_links(two_profiles, tmp_path):
    root, in_b = two_profiles
    st_a, sent_a = send("Dana Henderson, 14 Maple St 62707, water heater", tmp_path)
    st_b, sent_b = in_b.run(send, "Ray Ortiz, 62701, clogged drain", tmp_path)
    assert st_a is not st_b and st_a is runtime.store() and in_b.run(runtime.store) is st_b
    assert (root / "plugin-data/docket" / DB_FILENAME).exists() and (root / "profiles/b/plugin-data/docket" / DB_FILENAME).exists()
    assert sent_a["accept_url"].startswith("http://127.0.0.1:8787/q/")
    assert sent_b["accept_url"].startswith("http://127.0.0.1:8787/p/b/q/")
    assert [q.customer["name"] for q in st_a.list()] == ["Dana Henderson"]
    assert [q.customer["name"] for q in st_b.list()] == ["Ray Ortiz"]


def test_accept_page_reaches_the_right_profile_with_none_active(two_profiles, tmp_path, monkeypatch):
    _, in_b = two_profiles
    _, sent_a = send("Dana Henderson, 14 Maple St 62707, water heater", tmp_path)
    st_b, sent_b = in_b.run(send, "Ray Ortiz, 62701, clogged drain", tmp_path)
    path_a = sent_a["accept_url"].split("8787", 1)[1]
    path_b = sent_b["accept_url"].split("8787", 1)[1]
    told = []
    monkeypatch.setattr(runtime, "notify_owner", lambda event, rec, st=None, bk=None: told.append((event, rec.customer["name"], st)))
    app = contextvars.Context().run(runtime.accept_app)                 # a click: no profile active
    status, _, body = contextvars.Context().run(app.handle, "GET", path_b, {})
    assert status == 200 and b"Ray Ortiz" in body and b"Brightside Electric" in body and b"Copperline" not in body
    assert f"action='{path_b}/accept'".encode() in body and f"href='{path_b}/pdf'".encode() in body
    status, _, body = contextvars.Context().run(app.handle, "POST", path_b + "/accept", {})
    assert status == 200 and st_b.list()[0].status == "accepted" and told[-1] == ("accepted", "Ray Ortiz", st_b)
    status, _, body = contextvars.Context().run(app.handle, "GET", path_a, {})
    assert status == 200 and b"Dana Henderson" in body and b"Copperline Plumbing" in body


def test_links_cannot_cross_profiles_or_escape(two_profiles, tmp_path):
    _, in_b = two_profiles
    _, sent_a = send("Dana Henderson, 14 Maple St 62707, water heater", tmp_path)
    _, sent_b = in_b.run(send, "Ray Ortiz, 62701, clogged drain", tmp_path)
    token_a, token_b = sent_a["accept_url"].rsplit("/", 1)[1], sent_b["accept_url"].rsplit("/", 1)[1]
    app = contextvars.Context().run(runtime.accept_app)
    get = lambda p: contextvars.Context().run(app.handle, "GET", p, {})[0]
    assert get(f"/q/{token_b}") == 410 and get(f"/p/b/q/{token_a}") == 410       # signed by the other profile
    assert get(f"/p/nobody/q/{token_b}") == 404                                   # no such profile
    assert get(f"/p/../q/{token_b}") == 404 and get(f"/p/B/q/{token_b}") == 404   # not a profile id
    assert get(f"/p/b/q/{token_b}") == 200
