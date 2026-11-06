"""Exact money helpers. All amounts are Decimal; rounding happens once, half-up, to the cent."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ceil_whole(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1), rounding=ROUND_CEILING)


def fmt_money(value: Decimal, currency: str = "USD") -> str:
    symbol = {"USD": "$", "NZD": "$", "AUD": "$", "CAD": "$", "GBP": "£", "EUR": "€"}.get(currency, "")
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}" if symbol else f"{sign}{abs(value):,.2f} {currency}"


def fmt_qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text
