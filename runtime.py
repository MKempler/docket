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
        return _store


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


INTAKE_TASK: str | None = None     # set by register() when the host supports plugin auxiliary tasks
OWNER_NOTIFIERS: list = []         # callables(event, record) — Telegram adds one when the gateway connects


def notify_owner(event: str, rec: Any) -> None:
    import logging
    logging.getLogger(__name__).info("Docket: %s %s (%s)", rec.number, event, rec.customer.get("name"))
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
