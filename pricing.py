"""Deterministic pricing engine.

Input: a JobSpec (item codes + quantities + flags), usually produced from the owner's words by the
model. Output: a Quote whose every line names the rule that produced it.

Rules (in order):
1. Each item is priced from the price book only. Unknown codes become "needs price" lines that
   the owner must fill in; they are never guessed, and a quote with one cannot be sent.
2. Labor = hours x labor rate x multiplier. Only one multiplier applies: the highest flagged.
3. Material component quantities in whole-number units ("each") round up per line.
4. Each line amount is rounded half-up to the cent once; the subtotal is the exact sum of lines.
5. If the job has labor below the minimum, a top-up line bills the difference.
6. Discounts apply to labor (including the top-up) and appear as their own negative line.
7. Tax applies to the scopes the price book names (demo: materials only), rounded once.
8. Total = subtotal + tax, exactly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .money import ceil_whole, cents, fmt_money, fmt_qty
from .pricebook import MULTIPLIER_FLAGS, UNITS_WHOLE, PriceBook


@dataclass
class SpecItem:
    code: str
    qty: Decimal
    note: str = ""


@dataclass
class JobSpec:
    items: list[SpecItem]
    zip_code: str = ""
    flags: tuple[str, ...] = ()
    customer_tier: str = ""         # a discounts key, e.g. "member"
    include_service_call: bool = True
    unpriced: list[str] = field(default_factory=list)   # things the owner mentioned with no code


@dataclass
class QuoteLine:
    kind: str                       # assembly | material | fee | labor | service_call | travel | minimum | discount | needs_price
    code: str
    description: str
    qty: Decimal
    unit: str
    amount: Decimal
    labor: Decimal = Decimal(0)
    materials: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    labor_hours: Decimal = Decimal(0)
    rule: str = ""
    components: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Quote:
    currency: str
    lines: list[QuoteLine]
    subtotal: Decimal
    tax_name: str
    tax_rate: Decimal
    taxable: Decimal
    tax: Decimal
    total: Decimal
    multiplier_flag: str
    multiplier: Decimal
    needs_price: list[str]
    warnings: list[str]

    @property
    def complete(self) -> bool:
        return not self.needs_price

    def to_dict(self) -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, list):
                return [conv(x) for x in v]
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            return v
        d = conv(asdict(self))
        d["complete"] = self.complete
        return d


class SpecError(ValueError):
    pass


def spec_from_args(args: dict[str, Any]) -> JobSpec:
    """Build a JobSpec from tool arguments, rejecting malformed quantities with a clear message."""
    items: list[SpecItem] = []
    for i, raw in enumerate(args.get("items") or []):
        if not isinstance(raw, dict):
            raise SpecError(f"items[{i}] must be an object with code and qty")
        code = str(raw.get("code") or "").strip().upper()
        if not code:
            raise SpecError(f"items[{i}] has no code")
        try:
            qty = Decimal(str(raw.get("qty")).strip())
        except (InvalidOperation, ValueError):
            raise SpecError(f"items[{i}] ({code}) qty {raw.get('qty')!r} is not a number") from None
        if not qty.is_finite() or qty <= 0:
            raise SpecError(f"items[{i}] ({code}) qty must be greater than 0")
        items.append(SpecItem(code, qty, str(raw.get("note") or "")))
    flags = tuple(str(f).strip().lower() for f in (args.get("flags") or []) if str(f).strip())
    return JobSpec(
        items=items,
        zip_code=str(args.get("zip") or args.get("zip_code") or "").strip(),
        flags=flags,
        customer_tier=str(args.get("customer_tier") or "").strip().lower(),
        include_service_call=bool(args.get("include_service_call", True)),
        unpriced=[str(x) for x in (args.get("unpriced") or []) if str(x).strip()],
    )


def price(book: PriceBook, spec: JobSpec) -> Quote:
    cur = book.currency
    lines: list[QuoteLine] = []
    needs: list[str] = []
    warnings: list[str] = []

    flag, mult = _multiplier(book, spec.flags, warnings)
    mult_note = f" x {fmt_qty(mult)} {flag.replace('_', ' ')}" if flag else ""

    def labor_amount(hours: Decimal) -> Decimal:
        return cents(hours * book.labor_rate * mult)

    if spec.include_service_call and book.service_call:
        sc = book.service_call
        lines.append(QuoteLine("service_call", sc.code, sc.name, Decimal(1), "each", sc.amount,
                               fees=sc.amount, rule=f"Flat service call {fmt_money(sc.amount, cur)}"))

    if spec.zip_code:
        zone = book.zone_for_zip(spec.zip_code)
        if zone is None:
            needs.append(f"Travel to ZIP {spec.zip_code} (not in any travel zone)")
            lines.append(QuoteLine("needs_price", "TRAVEL", f"Travel to ZIP {spec.zip_code}", Decimal(1), "each",
                                   Decimal(0), rule="ZIP not in price book travel zones: owner must price"))
        elif zone.amount > 0:
            lines.append(QuoteLine("travel", f"ZONE-{zone.name}", f"Travel (zone {zone.name})", Decimal(1), "each",
                                   zone.amount, fees=zone.amount,
                                   rule=f"ZIP {spec.zip_code} is in zone {zone.name}: {fmt_money(zone.amount, cur)}"))

    for item in spec.items:
        found = book.lookup(item.code)
        if found is None:
            needs.append(f"{item.code} x {fmt_qty(item.qty)}")
            lines.append(QuoteLine("needs_price", item.code, item.note or f"{item.code} (not in price book)",
                                   item.qty, "", Decimal(0), rule="Not in price book: owner must price"))
            continue
        kind, entry = found
        if kind == "labor":
            amt = labor_amount(item.qty)
            lines.append(QuoteLine("labor", "LABOR", item.note or "Labor", item.qty, "h", amt, labor=amt,
                                   labor_hours=item.qty,
                                   rule=f"{fmt_qty(item.qty)} h x {fmt_money(book.labor_rate, cur)}{mult_note} = {fmt_money(amt, cur)}"))
            continue
        if entry.unit in UNITS_WHOLE and item.qty != item.qty.to_integral_value():
            needs.append(f"{item.code} x {fmt_qty(item.qty)} (must be a whole number of {entry.unit})")
            lines.append(QuoteLine("needs_price", item.code, f"{entry.name} (quantity {fmt_qty(item.qty)} is not whole)",
                                   item.qty, entry.unit, Decimal(0), rule=f"{entry.unit} items need a whole quantity"))
            continue
        if kind == "fee":
            amt = cents(entry.amount * item.qty)
            lines.append(QuoteLine("fee", entry.code, entry.name, item.qty, entry.unit, amt, fees=amt,
                                   rule=f"{fmt_qty(item.qty)} x {fmt_money(entry.amount, cur)} = {fmt_money(amt, cur)}"))
        elif kind == "material":
            amt = cents(entry.price * item.qty)
            lines.append(QuoteLine("material", entry.code, entry.name, item.qty, entry.unit, amt, materials=amt,
                                   rule=f"{fmt_qty(item.qty)} {entry.unit} x {fmt_money(entry.price, cur)} = {fmt_money(amt, cur)}"))
        else:
            lines.append(_assembly_line(book, entry, item, mult, mult_note))

    for text in spec.unpriced:
        needs.append(text)
        lines.append(QuoteLine("needs_price", "CUSTOM", text, Decimal(1), "each", Decimal(0),
                               rule="Mentioned but not in price book: owner must price"))

    hours = sum((l.labor_hours for l in lines), Decimal(0))
    if hours > 0 and hours < book.labor_minimum_hours:
        top = book.labor_minimum_hours - hours
        amt = labor_amount(top)
        lines.append(QuoteLine("minimum", "LABOR-MIN", "Minimum labor charge", top, "h", amt, labor=amt, labor_hours=top,
                               rule=f"Job labor {fmt_qty(hours)} h is under the {fmt_qty(book.labor_minimum_hours)} h minimum: "
                                    f"+{fmt_qty(top)} h x {fmt_money(book.labor_rate, cur)}{mult_note} = {fmt_money(amt, cur)}"))

    if spec.customer_tier:
        disc = book.discounts.get(spec.customer_tier)
        if disc is None:
            warnings.append(f"Customer tier {spec.customer_tier!r} has no discount in the price book; none applied")
        else:
            labor_total = sum((l.labor for l in lines), Decimal(0))
            if labor_total > 0:
                amt = -cents(labor_total * disc.rate)
                lines.append(QuoteLine("discount", f"DISC-{disc.key.upper()}", disc.label, Decimal(1), "each", amt, labor=amt,
                                       rule=f"{fmt_qty(disc.rate * 100)}% of labor {fmt_money(labor_total, cur)} = {fmt_money(amt, cur)}"))

    subtotal = sum((l.amount for l in lines), Decimal(0))
    taxable = Decimal(0)
    if "materials" in book.tax_applies_to:
        taxable += sum((l.materials for l in lines), Decimal(0))
    if "labor" in book.tax_applies_to:
        taxable += sum((l.labor for l in lines), Decimal(0))
    if "fees" in book.tax_applies_to:
        taxable += sum((l.fees for l in lines), Decimal(0))
    tax = cents(taxable * book.tax_rate)

    return Quote(currency=cur, lines=lines, subtotal=subtotal, tax_name=book.tax_name, tax_rate=book.tax_rate,
                 taxable=taxable, tax=tax, total=subtotal + tax, multiplier_flag=flag, multiplier=mult,
                 needs_price=needs, warnings=warnings)


def _multiplier(book: PriceBook, flags: tuple[str, ...], warnings: list[str]) -> tuple[str, Decimal]:
    best_flag, best = "", Decimal(1)
    for f in flags:
        if f not in MULTIPLIER_FLAGS:
            warnings.append(f"Unknown flag {f!r} ignored (known: {', '.join(MULTIPLIER_FLAGS)})")
            continue
        value = book.multipliers.get(f)
        if value is None:
            warnings.append(f"Flag {f!r} has no multiplier in the price book; none applied")
            continue
        if value > best:
            best_flag, best = f, value
    return best_flag, best


def _assembly_line(book: PriceBook, asm, item: SpecItem, mult: Decimal, mult_note: str) -> QuoteLine:
    cur = book.currency
    hours = asm.labor_hours * item.qty
    labor = cents(hours * book.labor_rate * mult)
    comps: list[dict[str, str]] = []
    mat_total = Decimal(0)
    parts: list[str] = []
    for comp in asm.materials:
        m = book.materials[comp.code]
        raw_qty = comp.qty * item.qty
        qty = ceil_whole(raw_qty) if m.unit in UNITS_WHOLE else raw_qty
        amt = cents(m.price * qty)
        mat_total += amt
        rounded = " (rounded up)" if qty != raw_qty else ""
        parts.append(f"{m.code} {fmt_qty(qty)} {m.unit}{rounded} @ {fmt_money(m.price, cur)}")
        comps.append({"code": m.code, "name": m.name, "qty": str(qty), "unit": m.unit,
                      "unit_price": str(m.price), "amount": str(amt)})
    rule = f"{fmt_qty(hours)} h x {fmt_money(book.labor_rate, cur)}{mult_note} = {fmt_money(labor, cur)}"
    if parts:
        rule += f"; materials {fmt_money(mat_total, cur)} ({'; '.join(parts)})"
    return QuoteLine("assembly", asm.code, asm.name, item.qty, asm.unit, labor + mat_total, labor=labor,
                     materials=mat_total, labor_hours=hours, rule=rule, components=comps)


def render_text(q: Quote) -> str:
    """Plain-text quote for CLI/chat surfaces."""
    cur = q.currency
    out = []
    for l in q.lines:
        qty = f"{fmt_qty(l.qty)} {l.unit}".strip()
        amount = "NEEDS PRICE" if l.kind == "needs_price" else fmt_money(l.amount, cur)
        out.append(f"{l.description:<38} {qty:>9}  {amount:>12}")
        out.append(f"    ↳ {l.rule}")
    out.append(f"{'Subtotal':<49}  {fmt_money(q.subtotal, cur):>12}")
    out.append(f"{q.tax_name + ' ' + fmt_qty(q.tax_rate * 100) + '% on ' + fmt_money(q.taxable, cur):<49}  {fmt_money(q.tax, cur):>12}")
    out.append(f"{'Total':<49}  {fmt_money(q.total, cur):>12}")
    if q.needs_price:
        out.append("Cannot send yet. Needs a price: " + "; ".join(q.needs_price))
    for w in q.warnings:
        out.append(f"Note: {w}")
    return "\n".join(out)
