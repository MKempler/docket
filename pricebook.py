"""Price book loading and validation.

The price book is a human-edited YAML file. Loading never guesses: any malformed value is
reported as an error naming the YAML path and line, and the book is not used until it is clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

UNITS_WHOLE = {"each"}          # quantities in these units must be whole numbers
MULTIPLIER_FLAGS = ("after_hours", "weekend", "emergency")


@dataclass(frozen=True)
class Material:
    code: str
    name: str
    unit: str
    cost: Decimal
    markup: Decimal

    @property
    def price(self) -> Decimal:
        from .money import cents
        return cents(self.cost * (Decimal(1) + self.markup))


@dataclass(frozen=True)
class Fee:
    code: str
    name: str
    unit: str
    amount: Decimal


@dataclass(frozen=True)
class Component:
    code: str
    qty: Decimal


@dataclass(frozen=True)
class Assembly:
    code: str
    name: str
    unit: str
    labor_hours: Decimal
    materials: tuple[Component, ...]


@dataclass(frozen=True)
class Discount:
    key: str
    rate: Decimal
    applies_to: str
    label: str


@dataclass(frozen=True)
class Zone:
    name: str
    amount: Decimal
    zips: frozenset[str]


@dataclass
class PriceBook:
    business: dict[str, str]
    currency: str
    tax_name: str
    tax_rate: Decimal
    tax_applies_to: frozenset[str]
    labor_rate: Decimal
    labor_minimum_hours: Decimal
    multipliers: dict[str, Decimal]
    discounts: dict[str, Discount]
    service_call: Fee | None
    zones: tuple[Zone, ...]
    materials: dict[str, Material]
    fees: dict[str, Fee]
    assemblies: dict[str, Assembly]
    source: str = ""

    def zone_for_zip(self, zip_code: str) -> Zone | None:
        z = (zip_code or "").strip()
        return next((zone for zone in self.zones if z in zone.zips), None)

    def lookup(self, code: str) -> tuple[str, Any] | None:
        c = (code or "").strip().upper()
        if c in self.assemblies:
            return "assembly", self.assemblies[c]
        if c in self.materials:
            return "material", self.materials[c]
        if c in self.fees:
            return "fee", self.fees[c]
        if c == "LABOR":
            return "labor", None
        return None


@dataclass
class ValidationError:
    path: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"{where}{self.path}: {self.message}"


@dataclass
class _Reader:
    errors: list[ValidationError] = field(default_factory=list)

    def line_of(self, node: Any, key: Any = None) -> int | None:
        lc = getattr(node, "lc", None)
        if lc is None:
            return None
        try:
            if isinstance(key, int):
                return lc.item(key)[0] + 1
            if key is not None:
                return lc.key(key)[0] + 1
            return lc.line + 1
        except Exception:
            try:
                return lc.line + 1
            except Exception:
                return None

    def err(self, path: str, message: str, node: Any = None, key: Any = None) -> None:
        self.errors.append(ValidationError(path, message, self.line_of(node, key) if node is not None else None))

    def money(self, node: Any, key: str, path: str, *, required: bool = True, allow_zero: bool = True) -> Decimal | None:
        if not isinstance(node, dict) or key not in node or node[key] is None:
            if required:
                self.err(f"{path}.{key}", "is required", node)
            return None
        return self.decimal(node[key], f"{path}.{key}", node, key, allow_zero=allow_zero)

    def decimal(self, raw: Any, path: str, node: Any = None, key: Any = None, *, allow_zero: bool = True) -> Decimal | None:
        if isinstance(raw, bool):
            self.err(path, f"must be a number, got {raw!r}", node, key)
            return None
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError):
            self.err(path, f"must be a number, got {raw!r}", node, key)
            return None
        if not value.is_finite():
            self.err(path, f"must be a finite number, got {raw!r}", node, key)
            return None
        if value < 0 or (value == 0 and not allow_zero):
            self.err(path, f"must be {'greater than' if not allow_zero else 'at least'} 0, got {raw!r}", node, key)
            return None
        return value

    def text(self, node: Any, key: str, path: str, *, required: bool = True) -> str:
        value = node.get(key) if isinstance(node, dict) else None
        if value is None or str(value).strip() == "":
            if required:
                self.err(f"{path}.{key}", "is required", node)
            return ""
        return str(value).strip()


def load_pricebook(path: str | Path) -> tuple[PriceBook | None, list[ValidationError]]:
    """Load and validate a price book. Returns (book, []) when clean, else (None, errors)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [ValidationError(str(p), f"cannot read file: {exc}")]
    return parse_pricebook(text, source=str(p))


def parse_pricebook(text: str, source: str = "<text>") -> tuple[PriceBook | None, list[ValidationError]]:
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    try:
        data = YAML(typ="rt").load(text)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return None, [ValidationError("yaml", str(getattr(exc, "problem", exc)), mark.line + 1 if mark else None)]
    if not isinstance(data, dict):
        return None, [ValidationError("root", "price book must be a mapping of sections")]

    r = _Reader()

    business = data.get("business") or {}
    if not isinstance(business, dict):
        r.err("business", "must be a mapping", data, "business")
        business = {}
    r.text(business, "name", "business")

    currency = str(data.get("currency") or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        r.err("currency", "must be a 3-letter code such as USD", data, "currency")

    tax = data.get("tax") or {}
    tax_rate = r.money(tax, "rate", "tax") if isinstance(tax, dict) else None
    if tax_rate is not None and tax_rate >= 1:
        r.err("tax.rate", "must be a fraction such as 0.0825, not a percentage", tax, "rate")
    applies = frozenset(str(x).strip() for x in (tax.get("applies_to") or []) if str(x).strip()) if isinstance(tax, dict) else frozenset()
    for scope in applies:
        if scope not in {"materials", "labor", "fees"}:
            r.err("tax.applies_to", f"unknown scope {scope!r} (use materials, labor, fees)", tax, "applies_to")

    labor = data.get("labor") or {}
    labor_rate = r.money(labor, "rate", "labor", allow_zero=False)
    labor_min = r.money(labor, "minimum_hours", "labor", required=False) or Decimal(0)

    multipliers: dict[str, Decimal] = {}
    mult_node = data.get("multipliers") or {}
    for key, raw in (mult_node.items() if isinstance(mult_node, dict) else []):
        if key not in MULTIPLIER_FLAGS:
            r.err(f"multipliers.{key}", f"unknown multiplier (use {', '.join(MULTIPLIER_FLAGS)})", mult_node, key)
            continue
        value = r.decimal(raw, f"multipliers.{key}", mult_node, key, allow_zero=False)
        if value is not None:
            if value < 1:
                r.err(f"multipliers.{key}", "must be 1 or more", mult_node, key)
            else:
                multipliers[key] = value

    discounts: dict[str, Discount] = {}
    disc_node = data.get("discounts") or {}
    for key, node in (disc_node.items() if isinstance(disc_node, dict) else []):
        path = f"discounts.{key}"
        rate = r.money(node, "rate", path)
        target = str((node or {}).get("applies_to") or "labor").strip()
        if target != "labor":
            r.err(f"{path}.applies_to", "only 'labor' discounts are supported", node, "applies_to")
        if rate is not None and rate >= 1:
            r.err(f"{path}.rate", "must be a fraction such as 0.10", node, "rate")
        if rate is not None:
            discounts[str(key)] = Discount(str(key), rate, target, str((node or {}).get("label") or key))

    service_call = None
    sc = data.get("service_call")
    if sc:
        amount = r.money(sc, "amount", "service_call")
        if amount is not None:
            service_call = Fee(str(sc.get("code") or "SERVICE-CALL").upper(), r.text(sc, "name", "service_call"), "each", amount)

    zones: list[Zone] = []
    zone_node = data.get("travel_zones") or {}
    seen_zips: dict[str, str] = {}
    for name, node in (zone_node.items() if isinstance(zone_node, dict) else []):
        path = f"travel_zones.{name}"
        amount = r.money(node, "amount", path)
        zips = [str(z).strip() for z in ((node or {}).get("zips") or [])]
        for z in zips:
            if z in seen_zips:
                r.err(f"{path}.zips", f"ZIP {z} is also in zone {seen_zips[z]}", node, "zips")
            seen_zips[z] = str(name)
        if amount is not None:
            zones.append(Zone(str(name), amount, frozenset(zips)))

    materials: dict[str, Material] = {}
    mat_node = data.get("materials") or {}
    for code, node in (mat_node.items() if isinstance(mat_node, dict) else []):
        c, path = str(code).upper(), f"materials.{code}"
        cost = r.money(node, "cost", path)
        markup = r.money(node, "markup", path, required=False) or Decimal(0)
        name, unit = r.text(node, "name", path), r.text(node, "unit", path)
        if cost is not None and name and unit:
            materials[c] = Material(c, name, unit, cost, markup)

    fees: dict[str, Fee] = {}
    fee_node = data.get("fees") or {}
    for code, node in (fee_node.items() if isinstance(fee_node, dict) else []):
        c, path = str(code).upper(), f"fees.{code}"
        amount = r.money(node, "amount", path)
        name = r.text(node, "name", path)
        if amount is not None and name:
            fees[c] = Fee(c, name, str((node or {}).get("unit") or "each"), amount)

    assemblies: dict[str, Assembly] = {}
    asm_node = data.get("assemblies") or {}
    for code, node in (asm_node.items() if isinstance(asm_node, dict) else []):
        c, path = str(code).upper(), f"assemblies.{code}"
        hours = r.money(node, "labor_hours", path)
        name, unit = r.text(node, "name", path), r.text(node, "unit", path)
        comps: list[Component] = []
        comp_list = (node or {}).get("materials") or []
        for i, comp in enumerate(comp_list):
            cpath = f"{path}.materials[{i}]"
            mcode = str((comp or {}).get("code") or "").upper()
            if mcode not in materials and mcode not in {str(k).upper() for k in mat_node}:
                r.err(f"{cpath}.code", f"{mcode or '(blank)'} is not in materials", comp_list, i)
                continue
            qty = r.money(comp, "qty", cpath, allow_zero=False)
            if qty is not None:
                comps.append(Component(mcode, qty))
        if hours is not None and name and unit:
            assemblies[c] = Assembly(c, name, unit, hours, tuple(comps))

    clashes = (set(materials) & set(fees)) | (set(materials) & set(assemblies)) | (set(fees) & set(assemblies))
    for code in sorted(clashes):
        r.err(code, "code is used in more than one section; codes must be unique")
    if "LABOR" in set(materials) | set(fees) | set(assemblies):
        r.err("LABOR", "is reserved for hourly labor")

    if r.errors:
        return None, r.errors
    return PriceBook(
        business={k: str(v) for k, v in business.items()},
        currency=currency,
        tax_name=str((tax or {}).get("name") or "Tax"),
        tax_rate=tax_rate or Decimal(0),
        tax_applies_to=applies,
        labor_rate=labor_rate,  # type: ignore[arg-type]
        labor_minimum_hours=labor_min,
        multipliers=multipliers,
        discounts=discounts,
        service_call=service_call,
        zones=tuple(zones),
        materials=materials,
        fees=fees,
        assemblies=assemblies,
        source=source,
    ), []
