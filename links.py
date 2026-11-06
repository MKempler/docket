"""Signed, expiring customer links. A token names one quote version; revising the quote invalidates it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

DEFAULT_TTL_DAYS = 30


class LinkError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(secret: bytes, quote_id: int, version: int, *, ttl_days: int = DEFAULT_TTL_DAYS,
               now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + ttl_days * 86400)
    payload = f"{quote_id}.{version}.{expires}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).digest()[:16]
    return f"{_b64(payload.encode())}.{_b64(sig)}"


def read_token(secret: bytes, token: str, *, now: float | None = None) -> tuple[int, int]:
    """Return (quote_id, version) for a valid, unexpired token, else raise LinkError."""
    try:
        body, sig = token.split(".", 1)
        payload = _unb64(body).decode()
        quote_id, version, expires = (int(x) for x in payload.split("."))
        given = _unb64(sig)
    except Exception:
        raise LinkError("This link is not valid.") from None
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(expected, given):
        raise LinkError("This link is not valid.")
    if (now if now is not None else time.time()) > expires:
        raise LinkError("This link has expired. Ask for an updated quote.")
    return quote_id, version
