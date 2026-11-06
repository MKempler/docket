"""Words -> JobSpec arguments. Never prices.

Two paths, same output shape:
  * model path: ctx.llm.complete_structured maps the owner's words onto price-book codes. The schema has
    no price fields, and every code the model returns is checked against the book: unknown codes move to
    "unpriced" so the owner sees them as needs-price lines instead of silent guesses.
  * fallback parser: deterministic matching on codes and the owner's own aliases, used when no model is
    configured or the model call fails. It is deliberately conservative: anything it cannot place becomes
    an unpriced line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .pricebook import UNITS_WHOLE, PriceBook

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
FLAG_PATTERNS = {
    "emergency": re.compile(r"\b(emergency|urgent|asap|right away|flooding)\b", re.I),
    "weekend": re.compile(r"\b(weekend|saturday|sunday|sat|sun)\b", re.I),
    "after_hours": re.compile(r"\b(after[- ]hours|evening|tonight|overnight|night)\b", re.I),
}
MEMBER_RE = re.compile(r"\b(member|membership|club)\b", re.I)
STREET_RE = re.compile(
    r"^\s*\d+[a-z]?\s+[\w .'-]*?\b(st|street|ave|avenue|rd|road|ln|lane|dr|drive|blvd|boulevard|ct|court|way|pl|"
    r"place|cir|circle|pkwy|parkway|hwy|highway|ter|terrace|trl|trail)\b\.?", re.I)
LENGTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ft|feet|foot|')\b", re.I)
TIMES_RE = re.compile(r"(?:\b(\d{1,2})\s*x\b|\bx\s*(\d{1,2})\b|\bqty\s*(\d{1,3})\b)", re.I)
LEADING_COUNT_RE = re.compile(r"^\s*(\d{1,2})\s+(?!(?:gal|gallon|gallons|in|inch|inches|hp|amp|amps|\"|mm|ft|feet|foot)\b)", re.I)
WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
LEADING_WORD_RE = re.compile(r"^\s*(one|two|three|four|five|six)\s+", re.I)
SPLIT_RE = re.compile(r"[,;\n]+")
FLAG_ONLY_RE = re.compile(r"^\s*(it'?s\s+)?(an?\s+)?(emergency|urgent|asap|weekend|saturday|sunday|after[- ]hours|evening|"
                          r"tonight|member|they'?re a member|member pricing|on saturday|on sunday|this weekend)\s*\.?\s*$", re.I)
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3}$")


@dataclass
class IntakeResult:
    args: dict[str, Any]
    customer_name: str = ""
    site: str = ""
    source: str = "parser"           # "model" or "parser"
    notes: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- fallback parser

def parse_words(book: PriceBook, text: str) -> IntakeResult:
    raw = (text or "").strip()
    zip_match = ZIP_RE.search(raw)
    zip_code = zip_match.group(1) if zip_match else ""
    flags = [f for f, pat in FLAG_PATTERNS.items() if pat.search(raw)]
    tier = "member" if MEMBER_RE.search(raw) and "member" in book.discounts else ""

    aliases = sorted(book.alias_index().items(), key=lambda kv: -len(kv[0]))
    codes = set(book.assemblies) | set(book.fees) | set(book.materials) | {"LABOR"}

    items: list[dict[str, Any]] = []
    unpriced: list[str] = []
    customer, site = "", ""
    for seg in SPLIT_RE.split(raw):
        seg = seg.strip()
        if not seg:
            continue
        seg_wo_zip = ZIP_RE.sub("", seg).strip(" .")
        if not seg_wo_zip or FLAG_ONLY_RE.match(seg_wo_zip):
            continue
        street = STREET_RE.match(seg_wo_zip)
        if street and not site:
            site = seg_wo_zip
            continue
        found = _find_items(seg_wo_zip, aliases, codes)
        if found:
            for code in found:
                items.append({"code": code, "qty": _qty_for(book, code, seg_wo_zip, single=len(found) == 1)})
            continue
        if not customer and NAME_RE.match(seg_wo_zip) and not items:
            customer = seg_wo_zip
            continue
        unpriced.append(seg_wo_zip)

    args = {"items": items, "zip": zip_code, "flags": flags, "customer_tier": tier, "unpriced": unpriced}
    notes = [] if items else ["No price-book items recognised; add codes or aliases."]
    return IntakeResult(args=args, customer_name=customer, site=site, source="parser", notes=notes)


def _find_items(segment: str, aliases: list[tuple[str, str]], codes: set[str]) -> list[str]:
    found: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", segment):
        if token.upper() in codes and token.upper() not in found and (token.isupper() or "-" in token):
            found.append(token.upper())
    norm = " " + " ".join(re.sub(r"[^a-z0-9\" -]", " ", segment.lower()).split()) + " "
    taken: list[tuple[int, int]] = []
    for phrase, code in aliases:
        idx = norm.find(" " + phrase + " ")
        while idx != -1:
            span = (idx, idx + len(phrase) + 2)
            if not any(a < span[1] and span[0] < b for a, b in taken):
                taken.append(span)
                if code not in found:
                    found.append(code)
                break
            idx = norm.find(" " + phrase + " ", idx + 1)
    return found


def _qty_for(book: PriceBook, code: str, segment: str, *, single: bool) -> str:
    kind, entry = book.lookup(code)  # type: ignore[misc]
    unit = "h" if kind == "labor" else entry.unit
    if unit == "ft":
        m = LENGTH_RE.search(segment)
        return m.group(1) if m else "1"
    if unit == "h":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", segment, re.I)
        return m.group(1) if m else "1"
    if not single:
        return "1"
    m = TIMES_RE.search(segment)
    if m:
        return next(g for g in m.groups() if g)
    m = LEADING_COUNT_RE.match(segment)
    if m:
        return m.group(1)
    m = LEADING_WORD_RE.match(segment)
    if m:
        return str(WORD_NUMBERS[m.group(1).lower()])
    return "1"


# ---------------------------------------------------------------- model path

INTAKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "customer_name": {"type": "string"},
        "site_address": {"type": "string"},
        "zip": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"code": {"type": "string"}, "qty": {"type": "number"}, "note": {"type": "string"}},
            "required": ["code", "qty"]}},
        "flags": {"type": "array", "items": {"type": "string", "enum": ["after_hours", "weekend", "emergency"]}},
        "member": {"type": "boolean"},
        "unpriced": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items"],
}


def catalog_text(book: PriceBook) -> str:
    rows = ["CODE | what it is | unit"]
    for a in book.assemblies.values():
        rows.append(f"{a.code} | {a.name} | per {a.unit}" + (f" | also called: {', '.join(a.aliases)}" if a.aliases else ""))
    for f in book.fees.values():
        rows.append(f"{f.code} | {f.name} | per {f.unit}" + (f" | also called: {', '.join(f.aliases)}" if f.aliases else ""))
    for m in book.materials.values():
        rows.append(f"{m.code} | {m.name} (material sold on its own) | per {m.unit}")
    rows.append("LABOR | extra hourly labor not covered by an assembly | hours")
    return "\n".join(rows)


INSTRUCTIONS = """You turn a tradesperson's job description into price-book line items. You never price anything.
Rules:
- Use ONLY codes from the catalog. If the work is not in the catalog, put a short description in "unpriced".
- Prefer an assembly over separate materials when an assembly covers the work.
- qty is in the item's unit (count for 'each', feet for 'ft', hours for LABOR). Sizes like "50 gallon" are
  not quantities.
- flags: weekend (Saturday/Sunday work), after_hours (evenings/nights), emergency (urgent, same day, flooding).
- member: true only if the customer is said to be a member.
- Put the customer's name, the street address and the 5-digit ZIP in their fields when given.
- If something essential is ambiguous, add a short question to "questions" instead of guessing.
Return JSON only."""


def model_intake(llm: Any, book: PriceBook, text: str, *, previous: dict[str, Any] | None = None,
                 timeout: float = 45.0) -> IntakeResult:
    prompt = f"Catalog:\n{catalog_text(book)}\n\n"
    if previous:
        prompt += ("Current quote items (apply the change below to these and return the FULL updated job):\n"
                   f"{json.dumps(previous, sort_keys=True)}\n\nChange: ")
    else:
        prompt += "Job description: "
    prompt += text
    result = llm.complete_structured(instructions=INSTRUCTIONS, input=[{"type": "text", "text": prompt}],
                                     json_schema=INTAKE_SCHEMA, schema_name="docket_jobspec",
                                     temperature=0, timeout=timeout, purpose="docket intake")
    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict):
        raise ValueError("model did not return a job")
    return _validated(book, parsed)


def _validated(book: PriceBook, parsed: dict[str, Any]) -> IntakeResult:
    notes: list[str] = []
    items: list[dict[str, Any]] = []
    unpriced = [str(u).strip() for u in parsed.get("unpriced") or [] if str(u).strip()]
    for raw in parsed.get("items") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        try:
            qty = Decimal(str(raw.get("qty")))
        except (InvalidOperation, ValueError):
            qty = Decimal(0)
        found = book.lookup(code)
        if found is None:
            unpriced.append(str(raw.get("note") or code).strip() or code)
            notes.append(f"{code or '(blank)'} is not in the price book; listed as needs price")
            continue
        if not qty.is_finite() or qty <= 0:
            notes.append(f"{code} had no usable quantity; listed as needs price")
            unpriced.append(str(raw.get("note") or code))
            continue
        kind, entry = found
        if kind != "labor" and entry.unit in UNITS_WHOLE and qty != qty.to_integral_value():
            notes.append(f"{code} quantity {qty} rounded up to whole {entry.unit}")
            qty = qty.to_integral_value(rounding="ROUND_CEILING")
        items.append({"code": code, "qty": str(qty), "note": str(raw.get("note") or "")})
    flags = [f for f in parsed.get("flags") or [] if f in FLAG_PATTERNS]
    zip_raw = str(parsed.get("zip") or "")
    zm = ZIP_RE.search(zip_raw)
    args = {"items": items, "zip": zm.group(1) if zm else "", "flags": flags,
            "customer_tier": "member" if parsed.get("member") and "member" in book.discounts else "",
            "unpriced": unpriced}
    return IntakeResult(args=args, customer_name=str(parsed.get("customer_name") or "").strip(),
                        site=str(parsed.get("site_address") or "").strip(), source="model", notes=notes,
                        questions=[str(q) for q in parsed.get("questions") or [] if str(q).strip()])


def intake(book: PriceBook, text: str, llm: Any = None, *, previous: dict[str, Any] | None = None) -> IntakeResult:
    """Model path when available, deterministic parser otherwise (or when the model fails)."""
    if llm is not None:
        try:
            return model_intake(llm, book, text, previous=previous)
        except Exception as exc:
            fallback = parse_words(book, text)
            fallback.notes.insert(0, f"Model intake unavailable ({type(exc).__name__}); used the built-in parser.")
            return fallback
    return parse_words(book, text)
