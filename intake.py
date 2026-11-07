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
from .spoken import normalize as normalize_spoken

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
FLAG_PATTERNS = {
    "emergency": re.compile(r"\b(emergency|urgent|asap|right away|flooding)\b", re.I),
    "weekend": re.compile(r"\b(weekend|saturday|sunday|sat|sun)\b", re.I),
    "after_hours": re.compile(r"\b(after[- ]hours|evening|tonight|overnight|night)\b", re.I),
}
MEMBER_RE = re.compile(r"\b(member|membership|club)\b", re.I)
STREET_SUFFIX = (r"st|street|ave|avenue|rd|road|ln|lane|dr|drive|blvd|boulevard|ct|court|way|pl|place|cir|circle|"
                 r"pkwy|parkway|hwy|highway|ter|terrace|trl|trail")
ADDRESS_RE = re.compile(r"\b\d+[a-z]?\s+(?:[A-Za-z][\w'.-]*\s+){0,3}?(?:" + STREET_SUFFIX + r")\b\.?", re.I)
LENGTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ft|feet|foot|')\b", re.I)
TIMES_RE = re.compile(r"(?:\b(\d{1,2})\s*x\b|\bx\s*(\d{1,2})\b|\bqty\s*(\d{1,3})\b)", re.I)
LEADING_COUNT_RE = re.compile(r"^\s*(\d{1,2})\s+(?!(?:gal|gallon|gallons|in|inch|inches|hp|amp|amps|\"|mm|ft|feet|foot)\b)", re.I)
WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
SIZE_WORDS = r"gal|gallon|gallons|in|inch|inches|hp|amp|amps|mm|ft|feet|foot|v|volt|volts|hour|hours|hr|hrs|h|ton|psi"
INNER_COUNT_RE = re.compile(r"(?<![\d./-])(\d{1,2})\s+(?!(?:" + SIZE_WORDS + r")\b)([A-Za-z][\w'-]*)", re.I)
LEADING_WORD_RE = re.compile(r"^\s*(one|two|three|four|five|six)\s+", re.I)
SPLIT_RE = re.compile(r"[,;:\n!?]+|(?<!\d)\.(?!\d)")
LABOR_HOURS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b(?:\s+(?:of\s+)?(?:labor|labour|work|extra))?", re.I)
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3}$")
CAP_NAME_RE = re.compile(r"\b([A-Z][a-z'][A-Za-z'-]*(?:\s+(?:[A-Z]\.|[A-Z][a-z'][A-Za-z'-]*)){1,2})\b")
NOT_NAMES = {"hey", "hi", "hello", "so", "ok", "okay", "she", "he", "they", "her", "his", "the", "needs", "need", "also",
             "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "weekend", "today",
             "tomorrow", "quote", "job", "customer", "mr", "mrs", "ms", "dr", "please", "thanks"}
# A leftover phrase becomes a needs-price line only if it names work (a verb) AND something specific (a noun
# that is not filler). "needs a swap" or "she can only do saturday" are dropped; "install a tankless heater" stays.
WORK_WORDS = {"install", "replace", "repair", "fix", "swap", "add", "remove", "move", "reroute", "run", "mount",
              "tile", "retile", "re-tile", "re-til", "paint", "drywall", "demo", "haul", "dig", "trench", "pour",
              "hook", "connect", "upgrade", "build", "patch", "leak", "clog", "unclog", "snake", "jet", "dig",
              "relocate", "convert", "cap", "seal", "rebuild", "supply", "put"}
FILLER = {"a", "an", "the", "it", "its", "it's", "one", "old", "new", "some", "this", "that", "them", "her", "his",
          "their", "she", "he", "they", "needs", "need", "wants", "want", "would", "like", "can", "could", "do",
          "also", "and", "or", "to", "of", "in", "on", "at", "for", "with", "so", "hey", "just", "only", "maybe",
          "probably", "too", "as", "well", "please", "over", "up", "out", "is", "be", "get", "got", "should", "will",
          "i", "we", "you", "me", "us", "my", "our", "your", "job", "work", "thing", "stuff", "there", "here"}


@dataclass
class IntakeResult:
    args: dict[str, Any]
    customer_name: str = ""
    site: str = ""
    source: str = "parser"           # "model" or "parser"
    email: str = ""
    notes: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- fallback parser

def _stem(token: str) -> str:
    t = re.sub(r"'s$", "", token.lower().strip("\".")).strip("'")
    for suffix in ("ing", "ed", "s"):
        if len(t) > 4 and t.endswith(suffix) and not t.endswith("ss"):
            return t[: -len(suffix)]
    return t


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[A-Za-z0-9\"']+(?:-[A-Za-z0-9]+)*", text.lower())]


def parse_words(book: PriceBook, text: str) -> IntakeResult:
    raw = normalize_spoken((text or "").strip())
    email_m = EMAIL_RE.search(raw)
    raw = EMAIL_RE.sub(" ", raw)
    zip_match = ZIP_RE.search(raw)
    zip_code = zip_match.group(1) if zip_match else ""
    flags = [f for f, pat in FLAG_PATTERNS.items() if pat.search(raw)]
    tier = "member" if MEMBER_RE.search(raw) and "member" in book.discounts else ""

    aliases = sorted(((tuple(_tokens(p)), c) for p, c in book.alias_index().items()), key=lambda kv: -len(kv[0]))
    codes = set(book.assemblies) | set(book.fees) | set(book.materials) | {"LABOR"}

    site = ""
    addr = ADDRESS_RE.search(raw)
    if addr:
        site = " ".join(addr.group(0).split()).rstrip(".")
    customer = _find_name(raw, site)

    items: list[dict[str, Any]] = []
    unpriced: list[str] = []
    ignored: list[str] = []
    for seg in SPLIT_RE.split(raw):
        seg = (seg or "").strip()
        if not seg:
            continue
        rest = ZIP_RE.sub(" ", seg)
        if site:
            rest = rest.replace(addr.group(0), " ") if addr else rest
        if customer:
            rest = re.sub(re.escape(customer), " ", rest, flags=re.I)
        rest = " ".join(rest.split()).strip(" .-")
        if not rest:
            continue
        hours = LABOR_HOURS_RE.search(rest)
        if hours and not _find_items(rest, aliases, set(book.assemblies)):
            items.append({"code": "LABOR", "qty": hours.group(1)})      # "plus 2 hours labor", "1.5 hrs extra"
            continue
        found = [c for c in _find_items(rest, aliases, codes) if not book.excluded_by(c, rest)]
        if not found and any(book.excluded_by(c, rest) for c in _find_items(rest, aliases, codes)):
            unpriced.append(rest)                       # e.g. "tankless water heater": not the gas heater swap
            continue
        if found:
            for code in found:
                items.append({"code": code, "qty": _qty_for(book, code, rest, single=len(found) == 1)})
            continue
        if not customer and not items and NAME_RE.match(rest) and not _is_work(rest):
            customer = rest
            continue
        if _is_work(rest):
            unpriced.append(rest)
        else:
            ignored.append(rest)

    args = {"items": items, "zip": zip_code, "flags": flags, "customer_tier": tier, "unpriced": unpriced}
    notes = [] if items else ["No price-book items recognised; add codes or aliases."]
    if ignored:
        notes.append("Not used: " + "; ".join(f"'{x}'" for x in ignored))
    return IntakeResult(args=args, customer_name=customer, site=site, source="parser", notes=notes,
                        email=email_m.group(0) if email_m else "")


def _find_name(raw: str, site: str) -> str:
    for m in CAP_NAME_RE.finditer(raw):
        words = m.group(1).split()
        while words and words[0].lower().strip(".") in NOT_NAMES:
            words = words[1:]
        if len(words) < 2 or any(w.lower().strip(".") in NOT_NAMES for w in words):
            continue
        name = " ".join(words)
        if site and name in site:
            continue
        if re.search(r"\b(" + STREET_SUFFIX + r")\b\.?$", name, re.I):
            continue
        return name
    return ""


def _is_work(phrase: str) -> bool:
    toks = _tokens(phrase)
    has_verb = any(t in WORK_WORDS for t in toks)
    specific = [t for t in toks if t not in FILLER and t not in WORK_WORDS and not t.isdigit()]
    return has_verb and bool(specific)


def _find_items(segment: str, aliases: list[tuple[tuple[str, ...], str]], codes: set[str]) -> list[str]:
    """Codes written out, then the owner's aliases: alias words in order, allowing up to 2 extra words between."""
    found: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", segment):
        if token.upper() in codes and token.upper() not in found and (token.isupper() or "-" in token):
            found.append(token.upper())
    toks = _tokens(segment)
    used: set[int] = set()
    for phrase, code in aliases:
        if not phrase:
            continue
        span = _match(toks, phrase, used)
        if span is not None:
            used.update(span)
            if code not in found:
                found.append(code)
    return found


def _match(toks: list[str], phrase: tuple[str, ...], used: set[int], max_gap: int = 2) -> list[int] | None:
    for start, tok in enumerate(toks):
        if tok != phrase[0] or start in used:
            continue
        positions, pos = [start], start
        for word in phrase[1:]:
            nxt = next((j for j in range(pos + 1, min(len(toks), pos + 2 + max_gap)) if toks[j] == word and j not in used), None)
            if nxt is None:
                break
            positions.append(nxt)
            pos = nxt
        if len(positions) == len(phrase):
            return positions
    return None


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
    # a count right before one of this item's own words: "reset 2 toilets", "swap 3 angle stops"
    item_words = {w for phrase in (entry.name, *entry.aliases) for w in _tokens(phrase)} - FILLER
    for m in INNER_COUNT_RE.finditer(segment):
        if 1 < int(m.group(1)) <= 12 and _stem(m.group(2)) in item_words:
            return m.group(1)
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
- Installs, replacements and repairs use the ASSEMBLY code (it already includes its parts and labor). Never split
  an assembly into its materials, and never add a material that an assembly already includes (a water heater
  replacement already includes the expansion tank and install kit). Use a material code only for parts the owner
  asks for in addition, e.g. "two extra supply lines".
- Add a fee (haul-away, permit, camera inspection) ONLY if the owner mentions it. "Keeping the old unit" or
  "no permit needed" means leave that fee out.
- Add LABOR ONLY when the owner states a number of hours. Never add labor on top of an assembly.
- qty is in the item's unit (count for 'each', feet for 'ft', hours for LABOR). Sizes like "50 gallon",
  "1/2 inch" or "3/4 inch" are not quantities.
- flags: weekend (Saturday/Sunday work), after_hours (evenings, nights, tonight), emergency (urgent, asap,
  same day, flooding).
- member: true if the customer is said to be a member.
- Always copy the 5-digit ZIP into "zip". Put the customer's name and the street address in their fields.
- If something essential is ambiguous, add a short question to "questions" instead of guessing.
Return JSON only."""


def model_intake(llm: Any, book: PriceBook, text: str, *, previous: dict[str, Any] | None = None,
                 timeout: float = 90.0) -> IntakeResult:
    text = normalize_spoken(text)            # "six two seven oh seven" -> 62707: the guards below read digits
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
    return _validated(book, parsed, text=text, previous=previous)


PARTS_ONLY_RE = re.compile(r"\b(parts? only|materials? only|drop(?:ped)? off|supply only|just the part)\b", re.I)
HOURS_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:h|hr|hrs|hour|hours)\b", re.I)


def _validated(book: PriceBook, parsed: dict[str, Any], *, text: str = "", previous: dict[str, Any] | None = None
               ) -> IntakeResult:
    """Check the model's job against the price book AND against the words themselves.

    Code reads what code can read reliably (ZIP, timing flags, membership) and the model may not invent work:
    LABOR needs stated hours, and a fee needs the owner to have mentioned it (by name or alias).
    """
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

    prev_codes = {i["code"] for i in (previous or {}).get("items", [])}
    if text:
        aliases = sorted(((tuple(_tokens(p)), c) for p, c in book.alias_index().items()), key=lambda kv: -len(kv[0]))
        codes = set(book.assemblies) | set(book.fees) | set(book.materials) | {"LABOR"}
        mentioned = set(_find_items(text, aliases, codes))
        kept = []
        for it in items:
            kind = book.lookup(it["code"])[0]
            word = book.excluded_by(it["code"], text)
            if word:
                notes.append(f"'{word}' is not covered by {it['code']}; listed as needs price")
                unpriced.append(f"{word} job (not {it['code']})")
                continue
            if it["code"] == "LABOR" and "LABOR" not in prev_codes and not HOURS_RE.search(text):
                notes.append("Dropped hourly labor the job did not ask for")
                continue
            if kind == "fee" and it["code"] not in prev_codes and it["code"] not in mentioned:
                notes.append(f"Dropped {book.fees[it['code']].name.lower()}: not mentioned")
                continue
            kept.append(it)
        items = kept

        # A loose part becomes the full job when the words name the job (e.g. TOILET -> TOILET-REPLACE for
        # "new toilet"), unless the owner asked for parts only.
        if not PARTS_ONLY_RE.search(text):
            chosen = {it["code"] for it in items}
            for it in items:
                if book.lookup(it["code"])[0] != "material":
                    continue
                for asm_code in mentioned:
                    asm = book.assemblies.get(asm_code)
                    if asm and asm_code not in chosen and any(c.code == it["code"] for c in asm.materials):
                        notes.append(f"Used the full job '{asm.name}' instead of the loose part {it['code']}")
                        it["code"] = asm_code
                        chosen.add(asm_code)
                        break

    # A fee can't exceed what it applies to (price book `limit_to`), e.g. one haul-away per replaced fixture.
    for it in items:
        fee = book.fees.get(it["code"])
        if fee and fee.limit_to:
            cap = sum((Decimal(str(o["qty"])) for o in items if o["code"] in fee.limit_to), Decimal(0))
            if cap > 0 and Decimal(str(it["qty"])) > cap:
                notes.append(f"{fee.name}: {it['qty']} reduced to {cap} (one per {', '.join(fee.limit_to)})")
                it["qty"] = str(cap)

    # Stated hours of labor ("plus 1 hour labor", "2.5 hrs") are read by code, whatever the model did with them.
    if text and not any(it["code"] == "LABOR" for it in items):
        m = LABOR_HOURS_RE.search(text)
        if m and re.search(r"\b(labou?r|work|extra|on site|onsite)\b", text, re.I):
            items.append({"code": "LABOR", "qty": m.group(1), "note": "stated hours"})
            unpriced = [u for u in unpriced if not LABOR_HOURS_RE.search(u)]
            notes.append(f"Added {m.group(1)} h of labor as stated")

    flags = [f for f in parsed.get("flags") or [] if f in FLAG_PATTERNS]
    if text:
        flags = sorted(set(flags) | {f for f, pat in FLAG_PATTERNS.items() if pat.search(text)})
    zm = ZIP_RE.search(text or "") or ZIP_RE.search(str(parsed.get("zip") or ""))
    zip_code = zm.group(1) if zm else str((previous or {}).get("zip") or "")
    member = bool(parsed.get("member")) or bool(text and MEMBER_RE.search(text))
    args = {"items": items, "zip": zip_code, "flags": flags,
            "customer_tier": "member" if member and "member" in book.discounts else "",
            "unpriced": unpriced}
    questions = [str(q) for q in parsed.get("questions") or [] if str(q).strip()]
    if previous:   # a revision already knows the customer, address, ZIP and tier: don't ask again
        questions = [q for q in questions if not re.search(r"\b(name|address|zip|member|customer)\b", q, re.I)]
    em = EMAIL_RE.search(text or "")
    return IntakeResult(args=args, customer_name=str(parsed.get("customer_name") or "").strip(),
                        site=str(parsed.get("site_address") or "").strip(), source="model", notes=notes,
                        questions=questions, email=em.group(0) if em else "")


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
