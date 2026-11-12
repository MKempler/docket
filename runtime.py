"""Handles inside Hermes: the store, the price book, data folders, the model.

One Hermes process can serve several profiles (a multiplexing gateway). Each profile has its own Docket data
folder (Hermes's plugin_data_dir follows the active profile), so each gets its own store and price book here.
The customer accept page is one local server for the process; a link names its profile (/p/<name>/q/<token>,
or /q/<token> for the process's own profile) so a click with no profile active still reaches the right quotes.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
from functools import partial
from pathlib import Path
from typing import Any

from .pricebook import PriceBook, load_pricebook
from .store import DB_FILENAME, Store, open_store, open_store_at
from .store_paths import data_dir, pricebook_path, pricebook_path_in

logger = logging.getLogger(__name__)
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")      # Hermes profile ids (hermes_constants.PROFILE_ID_RE)

_lock = threading.Lock()
_stores: dict[str, Store] = {}           # resolved data folder -> its store, one per profile
_test_store: Store | None = None


def store() -> Store:
    """The active profile's store."""
    if _test_store is not None:
        return _test_store
    return _store_for(data_dir(), open_store)


def _store_for(folder: Path, opener: Any) -> Store:
    key = str(Path(folder).resolve())
    with _lock:
        s = _stores.get(key)
        if s is None:
            s = opener()
            s.listeners.append(_broadcast_change)
            _stores[key] = s
        return s


def process_data_dir() -> Path:
    """This process's own Docket data folder: data_dir() with no profile active (an empty context), the folder
    of the profile the process was started as. Named profiles a gateway serves live in <root>/profiles/<name>."""
    return contextvars.Context().run(data_dir)


def _named_profile_dir(name: str, root: Path) -> Path:
    return root.parent.parent / "profiles" / name / "plugin-data" / "docket"


def profile_route() -> str:
    """'' for the process's own profile, '/p/<name>' for a named profile it serves: the accept-link prefix."""
    try:
        here, root = data_dir().resolve(), process_data_dir().resolve()
    except Exception:
        return ""                                    # outside Hermes (tests, CLI tools): one store
    if here == root:
        return ""
    name = here.parent.parent.name
    if PROFILE_RE.fullmatch(name) and _named_profile_dir(name, root).resolve() == here:
        return f"/p/{name}"
    logger.warning("Docket: data folder %s is not a profile of this Hermes; customer links use the default", here)
    return ""


def route_target(name: str):
    """The store, price book and owner notifier behind an accept link's profile name ('' = this process's own)."""
    from .accept_server import Target
    if not name:
        return Target(store(), require_book(), notify_owner, link_base(""))
    if not PROFILE_RE.fullmatch(name):
        return None
    folder = _named_profile_dir(name, process_data_dir().resolve())
    if not (folder / DB_FILENAME).exists():
        return None                                  # unknown profile, or one that never used Docket
    from .settings import scoped
    st = _store_for(folder, partial(open_store_at, folder))
    scope = partial(scoped, json.loads(st.get_meta(SNAPSHOT_KEY) or "{}"))
    with scope():                                    # this profile's settings, not the default profile's
        bk = _book_in(folder)
        base = link_base(f"/p/{name}", st)
    return Target(st, bk, partial(notify_owner, st=st, bk=bk), base, scope)


def _broadcast_change(quote_id: int, status: str) -> None:
    """Tell connected Hermes Desktop windows the board changed (no-op when none are connected)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("docket", "board.updated", {"quote_id": quote_id, "status": status})
    except Exception:
        pass


def reset_for_tests(s: Store | None = None) -> None:
    global _test_store
    with _lock:
        _test_store = s
        _stores.clear()


def book() -> tuple[PriceBook | None, list, Path, bool]:
    from .settings import apply_business
    path, is_demo = pricebook_path()
    b, errors = load_pricebook(path)
    _remember_settings()
    return apply_business(b), errors, path, is_demo


SNAPSHOT_KEY = "settings_snapshot"


def _remember_settings() -> None:
    """Keep this profile's settings in its own store, so the accept page can use them when a customer clicks
    with no profile active (Hermes settings follow the active profile)."""
    try:
        from .settings import in_scope, snapshot
        if in_scope():
            return                                   # reading another profile's remembered settings
        snap = json.dumps(snapshot(), sort_keys=True)
        st = store()
        if st.get_meta(SNAPSHOT_KEY) != snap:
            st.set_meta(SNAPSHOT_KEY, snap)
    except Exception:
        logger.debug("Docket settings snapshot skipped", exc_info=True)


def _book_in(folder: Path) -> PriceBook:
    """A named profile's price book with its business details; call inside that profile's settings scope."""
    from .settings import apply_business
    path, _ = pricebook_path_in(folder)
    b, errors = load_pricebook(path)
    if errors or b is None:
        raise RuntimeError(f"Price book `{path}` has {len(errors)} error(s).")
    return apply_business(b)


def require_book() -> PriceBook:
    b, errors, path, _ = book()
    if errors or b is None:
        raise RuntimeError(f"Price book `{path}` has {len(errors)} error(s). Run /pricebook validate.")
    return b


def outbox_root() -> Path:
    root = data_dir() / "outbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


CTX: Any = None                    # the PluginContext (has_plugin, config, llm)
INTAKE_TASK: str | None = None     # set by register() when the host supports plugin auxiliary tasks
OWNER_NOTIFIERS: list = []         # extra callables(event, record) run after the owner card; none by default


def notify_owner(event: str, rec: Any, st: Store | None = None, bk: PriceBook | None = None) -> None:
    """Tell the owner a customer opened / accepted / declined. st, bk: the quote's profile (accept page)."""
    st = st or store()
    logger.info("Docket: %s %s (%s)", rec.number, event, rec.customer.get("name"))
    try:
        from .owner import event_card, push
        card = event_card(bk or require_book(), st.get(rec.id), event, store=st)
        if card is not None:
            push(st, card)
    except Exception:
        logger.exception("Docket owner card failed")
    for fn in list(OWNER_NOTIFIERS):
        try:
            fn(event, rec)
        except Exception:
            logger.exception("Docket owner notifier failed")


def accept_app():
    from .accept_server import AcceptApp
    try:
        from .render import render_pdf
    except Exception:
        render_pdf = None
    return AcceptApp(store, require_book, notify=notify_owner, render_pdf=render_pdf, resolve=route_target)


def ensure_accept_server() -> str:
    """Start the local accept page if needed; return the base URL customer links should use (this profile's)."""
    from .accept_server import ensure_running
    local = ensure_running(accept_app())
    return (public_link() or local).rstrip("/") + profile_route()


def public_link() -> str:
    """The owner's public base URL for customer links ('' = this computer only)."""
    from .settings import setting
    return setting("public_link") or store().get_meta("public_base_url") or ""


def link_base(route: str | None = None, st: Store | None = None) -> str:
    """Base URL for links built without starting the accept page (reports, Desktop, the accept page's PDF).
    route None = the active profile's; st = a named profile's store (read inside its settings scope)."""
    from .service import DEFAULT_BASE_URL
    from .settings import setting
    base = setting("public_link") or (st or store()).get_meta("public_base_url") or ""
    return (base or DEFAULT_BASE_URL).rstrip("/") + (profile_route() if route is None else route)


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
                   outbox_root=outbox_root(), base_url=ensure_accept_server, render_pdf=render_pdf_fn(),
                   pricebook=(pricebook_save, pricebook_cancel))


def push_quote_card(rec: Any, surface: str) -> bool:
    """On a messaging platform with a registered owner, send the quote as a card with buttons."""
    from .owner import CHANNELS, get_owner, push, quote_card
    if surface not in CHANNELS or not CHANNELS[surface].live or not get_owner(store(), surface):
        return False
    return bool(push(store(), quote_card(require_book(), rec), platforms=(surface,)))


def obol_installed() -> bool:
    """The owner's invoicing plugin (Obol) is installed. Docket never calls it: close-out returns obol_payload for
    the agent to pass to obol_create_invoice, so Obol's own approval, risk checks and cron block run."""
    ctx = CTX
    try:
        return bool(ctx is not None and ctx.has_plugin("obol"))
    except Exception:
        return False


def close_out(quote_id: int, *, extra_words: str = "", actor: str = "owner", surface: str = "", llm: Any = None):
    from .invoices import close_out as _close
    from .render import render_invoice_pdf
    return _close(store(), require_book(), quote_id, outbox_root=outbox_root(), extra_words=extra_words, llm=llm,
                  obol=obol_installed(), actor=actor, surface=surface, render=render_invoice_pdf)


# ---------------------------------------------------------------- price book edits

def own_pricebook_path() -> Path:
    return data_dir() / "pricebook.yaml"


def pricebook_propose(text: str = "", *, llm: Any = None, removals: tuple[str, ...] = (),
                      aliases: tuple[tuple[str, str], ...] = (), fresh: bool = False):
    from .pbedit import propose
    current, _ = pricebook_path()
    return propose(store(), current, text, llm=llm, removals=removals, aliases=aliases, fresh=fresh)


def pricebook_save(token: str = "") -> str:
    from .pbedit import save
    current, _ = pricebook_path()
    return save(store(), own_pricebook_path(), current, token)


def pricebook_cancel(token: str = "") -> str:
    from .pbedit import cancel
    return cancel(store(), token)


def pricebook_undo() -> str:
    from .pbedit import undo
    return undo(store(), own_pricebook_path())


def push_pricebook_card(plan: Any, surface: str) -> bool:
    """On Telegram / Discord / Slack, show the preview with Save and Cancel buttons."""
    from .owner import CHANNELS, get_owner, push, pricebook_card
    if surface not in CHANNELS or not CHANNELS[surface].live or not get_owner(store(), surface):
        return False
    return bool(push(store(), pricebook_card(plan), platforms=(surface,)))
