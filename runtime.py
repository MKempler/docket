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


def llm(ctx: Any) -> Any:
    """ctx.llm when the host offers it. Intake falls back to the built-in parser on any failure."""
    try:
        return getattr(ctx, "llm", None)
    except Exception:
        return None
