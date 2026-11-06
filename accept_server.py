"""Customer accept page: a small stdlib HTTP server (no extra dependencies).

Routes (token = HMAC-signed, expiring, bound to one quote version):
  GET  /q/<token>          the quote, mobile-friendly; marks it viewed
  POST /q/<token>/accept   accept  -> owner is notified
  POST /q/<token>/decline  decline (optional reason) -> owner is notified
  GET  /q/<token>/pdf      the quote PDF
  GET  /health

Binds 127.0.0.1 by default. Exposing it to real customers is the owner's choice (a tunnel), documented in the
README and disclosed in the catalog entry.
"""

from __future__ import annotations

import html
import logging
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs

from .money import fmt_money

logger = logging.getLogger(__name__)

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8787
MAX_BODY = 4096

CSS = """
:root{--ink:#18202a;--muted:#6b7280;--line:#e5e7eb;--bg:#f6f7f9;--card:#fff;--ok:#15803d;--no:#b91c1c;--accent:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--ink:#e5e7eb;--muted:#9ca3af;--line:#2b3340;--bg:#0f141a;--card:#161d26;--accent:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:20px 16px 48px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px;color:var(--muted);font-weight:500}
.biz{color:var(--muted);font-size:14px}.row{display:flex;justify-content:space-between;gap:16px;padding:9px 0;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}.qty{color:var(--muted);font-size:14px}.tot{font-weight:600;font-size:19px}
.note{color:var(--muted);font-size:13px;margin-top:8px}.btns{display:flex;gap:12px;flex-wrap:wrap}
button,.btn{flex:1;min-width:140px;border:0;border-radius:10px;padding:14px 18px;font-size:17px;font-weight:600;cursor:pointer;text-align:center;text-decoration:none}
.yes{background:var(--ok);color:#fff}.no{background:transparent;color:var(--no);border:1px solid var(--line)}
.link{color:var(--accent)}textarea{width:100%;margin-top:10px;border:1px solid var(--line);border-radius:8px;padding:10px;font:inherit;background:var(--card);color:var(--ink)}
.status{display:inline-block;padding:3px 10px;border-radius:99px;font-size:13px;font-weight:600;background:var(--line)}
"""


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body><main>{body}</main></body></html>").encode()


def quote_page(book: Any, rec: Any, token: str, *, message: str = "") -> bytes:
    q, cur, biz = rec.quote, rec.quote.get("currency", "USD"), book.business
    rows = []
    for line in q["lines"]:
        qty = Decimal(line["qty"]).normalize()
        unit = line["unit"] if line["unit"] and line["unit"] != "each" else ""
        qty_label = f"{format(qty, 'f')} {unit}".strip() if (qty != 1 or unit) else ""
        rows.append(f"<div class=row><div>{html.escape(line['description'])}"
                    f"{f' <span class=qty>· {html.escape(qty_label)}</span>' if qty_label else ''}</div>"
                    f"<div>{fmt_money(Decimal(line['amount']), cur)}</div></div>")
    surcharge = ""
    if q.get("multiplier_flag"):
        surcharge = (f"<p class=note>Labor is charged at the {html.escape(q['multiplier_flag'].replace('_', '-'))} rate "
                     f"(× {format(Decimal(q['multiplier']).normalize(), 'f')}).</p>")
    tax_label = f"{html.escape(q['tax_name'])} ({format((Decimal(q['tax_rate']) * 100).normalize(), 'f')}% on materials)"
    open_ = rec.status in {"sent", "viewed"}
    actions = ""
    if open_:
        actions = (f"<div class=card><form method=post action='/q/{token}/accept'><div class=btns>"
                   f"<button class=yes type=submit>Accept quote</button></div></form>"
                   f"<details style='margin-top:14px'><summary class=link>Decline or ask a question</summary>"
                   f"<form method=post action='/q/{token}/decline'><textarea name=reason rows=3 maxlength=500 "
                   f"placeholder='Optional: tell {html.escape(biz.get('name', 'us'))} why'></textarea>"
                   f"<div class=btns style='margin-top:10px'><button class=no type=submit>Decline</button></div>"
                   f"</form></details></div>")
    status = {"accepted": "Accepted ✓", "declined": "Declined", "sent": "Awaiting your answer",
              "viewed": "Awaiting your answer"}.get(rec.status, rec.status.replace("_", " ").title())
    banner = f"<div class=card><strong>{html.escape(message)}</strong></div>" if message else ""
    body = (f"{banner}<div class=card><div class=biz>{html.escape(biz.get('name', ''))} · "
            f"{html.escape(biz.get('phone', ''))}</div><h1>Quote {html.escape(rec.number)}</h1>"
            f"<h2>For {html.escape(rec.customer.get('name', ''))}{(' · ' + html.escape(rec.site)) if rec.site else ''}"
            f" · <span class=status>{html.escape(status)}</span></h2>{''.join(rows)}{surcharge}</div>"
            f"<div class=card><div class=row><div>Subtotal</div><div>{fmt_money(Decimal(q['subtotal']), cur)}</div></div>"
            f"<div class=row><div>{tax_label}</div><div>{fmt_money(Decimal(q['tax']), cur)}</div></div>"
            f"<div class='row tot'><div>Total</div><div>{fmt_money(Decimal(q['total']), cur)}</div></div>"
            f"<p class=note>Fixed price for the work listed · <a class=link href='/q/{token}/pdf'>Download PDF</a></p></div>"
            f"{actions}")
    return _page(f"Quote {rec.number} · {biz.get('name', '')}", body)


def message_page(title: str, text: str) -> bytes:
    return _page(title, f"<div class=card><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></div>")


class AcceptApp:
    """Framework-free request logic, so it is testable without sockets."""

    def __init__(self, get_store: Callable[[], Any], get_book: Callable[[], Any],
                 notify: Callable[[str, Any], None] | None = None, render_pdf: Callable | None = None):
        self.get_store, self.get_book, self.notify, self.render_pdf = get_store, get_book, notify, render_pdf

    def handle(self, method: str, path: str, form: dict[str, str]) -> tuple[int, str, bytes]:
        from .service import ServiceError, customer_action, accept_url
        if path == "/health":
            return 200, "text/plain", b"ok"
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or parts[0] != "q":
            return 404, "text/html", message_page("Not found", "This page does not exist.")
        token, action = parts[1], (parts[2] if len(parts) > 2 else "")
        store, book = self.get_store(), self.get_book()
        try:
            if method == "GET" and action == "":
                rec = customer_action(store, token, "view")
                if rec.status == "viewed" and self.notify:
                    self._safe_notify("viewed", rec)
                return 200, "text/html", quote_page(book, rec, token)
            if method == "GET" and action == "pdf" and self.render_pdf:
                rec = customer_action(store, token, "view")
                return 200, "application/pdf", self.render_pdf(book, rec, accept_url(store, rec))
            if method == "POST" and action in {"accept", "decline"}:
                before = customer_action(store, token, "view").status
                rec = customer_action(store, token, action, note=(form.get("reason") or "")[:500])
                if rec.status != before and self.notify:
                    self._safe_notify(rec.status, rec)
                msg = ("Thanks! Your quote is accepted. We'll be in touch to book a time."
                       if rec.status == "accepted" else "Thanks for letting us know.")
                return 200, "text/html", quote_page(book, rec, token, message=msg)
        except ServiceError as exc:
            return 410, "text/html", message_page("Quote unavailable", str(exc))
        return 405, "text/html", message_page("Not allowed", "That action is not available.")

    def _safe_notify(self, event: str, rec: Any) -> None:
        try:
            self.notify(event, rec)  # type: ignore[misc]
        except Exception:
            logger.exception("Docket owner notification failed")


def make_handler(app: AcceptApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Docket"

        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text") else ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._send(*app.handle("GET", self.path.split("?", 1)[0], {}))

        def do_POST(self):  # noqa: N802
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            self._send(*app.handle("POST", self.path.split("?", 1)[0], form))

        def log_message(self, fmt, *args):  # quiet; Hermes logs carry what matters
            logger.debug("accept page: " + fmt, *args)

    return Handler


_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()


def ensure_running(app: AcceptApp, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """Start the server in a daemon thread once per process. Returns its base URL (or the reason it isn't up)."""
    global _server
    with _server_lock:
        if _server is None:
            try:
                _server = ThreadingHTTPServer((host, port), make_handler(app))
            except OSError as exc:  # port taken: often another Hermes process already serving
                logger.info("Docket accept page not started on %s:%s (%s)", host, port, exc)
                return f"http://{host}:{port}"
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, name="docket-accept", daemon=True).start()
            logger.info("Docket accept page on http://%s:%s", host, port)
        h, p = _server.server_address[:2]
        return f"http://{h}:{p}"


def serve_forever(app: AcceptApp, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Docket accept page on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
