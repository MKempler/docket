from __future__ import annotations

import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import service as svc  # noqa: E402
from docket.accept_server import AcceptApp, make_handler  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.render import render_pdf  # noqa: E402
from docket.store import Store  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")


@pytest.fixture
def env(tmp_path):
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    events = []
    app = AcceptApp(lambda: store, lambda: BOOK, notify=lambda e, r: events.append((e, r.number)), render_pdf=render_pdf)
    d = svc.draft_from_words(store, BOOK, "Dana Henderson, 14 Maple St 62707, water heater, permit, haul away")
    sent = svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    token = sent["accept_url"].rsplit("/", 1)[1]
    return store, app, token, events, d.record.id


def test_view_accept_flow(env):
    store, app, token, events, qid = env
    status, ctype, body = app.handle("GET", f"/q/{token}", {})
    assert status == 200 and b"Accept quote" in body and b"$1,972.36" in body and b"Dana Henderson" in body
    assert store.get(qid).status == "viewed" and events == [("viewed", "Q-1001")]
    status, _, body = app.handle("POST", f"/q/{token}/accept", {})
    assert status == 200 and b"accepted" in body and b"Accept quote" not in body
    assert store.get(qid).status == "accepted" and events[-1] == ("accepted", "Q-1001")
    app.handle("POST", f"/q/{token}/accept", {})                     # repeat tap: no second notification
    assert [e for e, _ in events].count("accepted") == 1


def test_decline_with_reason(env):
    store, app, token, events, qid = env
    app.handle("POST", f"/q/{token}/decline", {"reason": "Going with another plumber"})
    assert store.get(qid).status == "declined"
    assert store.events(qid)[0]["detail"] == "Going with another plumber"


def test_bad_and_stale_tokens(env):
    store, app, token, events, qid = env
    assert app.handle("GET", "/q/garbage", {})[0] == 410
    svc.revise_from_words(store, BOOK, qid, "add a shutoff valve")
    status, _, body = app.handle("POST", f"/q/{token}/accept", {})
    assert status == 410 and b"updated" in body and store.get(qid).status == "draft"


def test_html_is_escaped(tmp_path):
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    app = AcceptApp(lambda: store, lambda: BOOK)
    d = svc.draft_from_words(store, BOOK, "<script>alert(1)</script> Smith, 62701, water heater")
    sent = svc.send_quote(store, BOOK, d.record.id, outbox_root=tmp_path, actor="owner")
    body = app.handle("GET", "/q/" + sent["accept_url"].rsplit("/", 1)[1], {})[2]
    assert b"<script>" not in body


def test_real_http_roundtrip(env):
    store, app, token, events, qid = env
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(f"{base}/q/{token}").read()
        assert b"Accept quote" in page
        pdf = urllib.request.urlopen(f"{base}/q/{token}/pdf")
        assert pdf.headers["Content-Type"] == "application/pdf" and pdf.read().startswith(b"%PDF")
        data = urllib.parse.urlencode({}).encode()
        urllib.request.urlopen(urllib.request.Request(f"{base}/q/{token}/accept", data=data, method="POST")).read()
        assert store.get(qid).status == "accepted"
    finally:
        server.shutdown()


def test_ensure_running_moves_to_a_free_port_when_taken(env, monkeypatch):
    import socket
    from docket import accept_server
    store, app, token, events, qid = env
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    monkeypatch.setattr(accept_server, "_server", None)
    try:
        url = accept_server.ensure_running(app, "127.0.0.1", taken)
        assert url != f"http://127.0.0.1:{taken}" and url.startswith("http://127.0.0.1:")
        assert urllib.request.urlopen(f"{url}/health").read() == b"ok"
    finally:
        accept_server._server.shutdown()
        accept_server._server = None
        blocker.close()
