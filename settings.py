"""Owner settings from Hermes's plugin settings (plugin.yaml config_schema -> Desktop's settings form, or
`plugins.entries.docket.settings` in config.yaml). Every value has a safe default, so Docket works with none set.

Read through the plugin context Hermes gave register(); outside Hermes (tests, the eval) the defaults apply.
"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "business_name": "", "business_phone": "", "business_email": "", "license": "",
    "quote_valid_days": 30, "invoice_due_days": 14, "payment_terms": "", "public_link": "", "followup_after_days": 5,
}
LIMITS = {"quote_valid_days": (1, 365), "invoice_due_days": (0, 365), "followup_after_days": (1, 90)}
BUSINESS_KEYS = {"business_name": "name", "business_phone": "phone", "business_email": "email",
                 "license": "license", "payment_terms": "payment_terms"}


def setting(key: str) -> Any:
    """The owner's value for key (clamped to sane limits), else the default."""
    default = DEFAULTS[key]
    try:
        from . import runtime
        ctx = runtime.CTX
        value = ctx.get_config(key, default) if ctx is not None else default
    except Exception:
        value = default
    if isinstance(default, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        lo, hi = LIMITS.get(key, (value, value))
        return min(max(value, lo), hi)
    return str(value or "").strip()


def apply_business(book: Any) -> Any:
    """Overlay the business details set in Settings onto the price book's business section (non-empty only)."""
    if book is None:
        return book
    for key, field in BUSINESS_KEYS.items():
        value = setting(key)
        if value:
            book.business[field] = value
    return book
