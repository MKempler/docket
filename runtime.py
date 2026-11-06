"""Process-wide handles inside Hermes: the store, the price book, data folders, the model."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .pricebook import PriceBook, load_pricebook
from .store import Store, open_store
from .store_paths import data_dir, pricebook_path

_lock = threading.Lock()
_store: Store | None = None


def store() -> Store:
    global _store
    with _lock:
        if _store is None:
            _store = open_store()
            _store.listeners.append(_broadcast_change)
        return _store


def _broadcast_change(quote_id: int, status: str) -> None:
    """Tell connected Hermes Desktop windows the board changed (no-op when none are connected)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("docket", "board.updated", {"quote_id": quote_id, "status": status})
    except Exception:
        pass


def reset_for_tests(s: Store | None = None) -> None:
    global _store
    with _lock:
        _store = s


def book() -> tuple[PriceBook | None, list, Path, bool]:
    path, is_demo = pricebook_path()
    b, errors = load_pricebook(path)
    return b, errors, path, is_demo


def require_book() -> PriceBook:
    b, errors, path, _ = book()
    if errors or b is None:
        raise RuntimeError(f"Price book {path} has {len(errors)} error(s). Run /pricebook validate.")
    return b


def outbox_root() -> Path:
    root = data_dir() / "outbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


CTX: Any = None                    # the PluginContext, for dispatch_tool / has_plugin
INTAKE_TASK: str | None = None     # set by register() when the host supports plugin auxiliary tasks
OWNER_NOTIFIERS: list = []         # callables(event, record) — Telegram adds one when the gateway connects


def notify_owner(event: str, rec: Any) -> None:
    import logging
    logging.getLogger(__name__).info("Docket: %s %s (%s)", rec.number, event, rec.customer.get("name"))
    try:
        from .owner import event_card, push
        card = event_card(require_book(), store().get(rec.id), event, store=store())
        if card is not None:
            push(store(), card)
    except Exception:
        logging.getLogger(__name__).exception("Docket owner card failed")
    for fn in list(OWNER_NOTIFIERS):
        try:
            fn(event, rec)
        except Exception:
            logging.getLogger(__name__).exception("Docket owner notifier failed")


def accept_app():
    from .accept_server import AcceptApp
    try:
        from .render import render_pdf
    except Exception:
        render_pdf = None
    return AcceptApp(store, require_book, notify=notify_owner, render_pdf=render_pdf)


def ensure_accept_server() -> str:
    """Start the local accept page if needed; return the base URL customer links should use."""
    from .accept_server import ensure_running
    local = ensure_running(accept_app())
    return store().get_meta("public_base_url") or local


class _TaskLLM:
    """ctx.llm routed through Docket's own auxiliary task (thinking off, longer timeout, user-pinnable model)."""

    def __init__(self, inner: Any, task: str):
        self._inner, self._task = inner, task

    def complete_structured(self, **kwargs: Any) -> Any:
        return self._inner.complete_structured(task=self._task, **kwargs)


def llm(ctx: Any) -> Any:
    """ctx.llm when the host offers it. Intake falls back to the built-in parser on any failure."""
    try:
        inner = getattr(ctx, "llm", None)
    except Exception:
        return None
    if inner is not None and INTAKE_TASK:
        return _TaskLLM(inner, INTAKE_TASK)
    return inner


def render_pdf_fn():
    try:
        from .render import render_pdf
        return render_pdf
    except Exception:
        return None


def tap(data: str, platform: str, user_id: str):
    """A button press from any platform, with this process's store, price book and accept page."""
    from .owner import perform
    return perform(data, platform=platform, user_id=user_id, store=store(), book=require_book(),
                   outbox_root=outbox_root(), base_url=ensure_accept_server, render_pdf=render_pdf_fn())


def push_quote_card(rec: Any, surface: str) -> bool:
    """On a messaging platform with a registered owner, send the quote as a card with buttons."""
    from .owner import CHANNELS, get_owner, push, quote_card
    if surface not in CHANNELS or not CHANNELS[surface].live or not get_owner(store(), surface):
        return False
    return bool(push(store(), quote_card(require_book(), rec), platforms=(surface,)))


def invoice_handoff():
    """obol_create_invoice through Hermes's dispatch_tool when Obol is installed, else None (local invoice)."""
    ctx = CTX
    try:
        if ctx is None or not ctx.has_plugin("obol"):
            return None
    except Exception:
        return None

    def send(invoice: dict) -> str:
        import json as _json
        from .invoices import obol_payload
        raw = ctx.dispatch_tool("obol_create_invoice", obol_payload(invoice))
        data = _json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict) or data.get("error") or data.get("ok") is False:
            raise RuntimeError(str((data or {}).get("error") or data)[:200])
        return str(data.get("invoice_id") or data.get("id") or data.get("number") or "")
    return send


def close_out(quote_id: int, *, extra_words: str = "", actor: str = "owner", surface: str = "", llm: Any = None):
    from .invoices import close_out as _close
    from .render import render_invoice_pdf
    return _close(store(), require_book(), quote_id, outbox_root=outbox_root(), extra_words=extra_words, llm=llm,
                  handoff=invoice_handoff(), actor=actor, surface=surface, render=render_invoice_pdf)
