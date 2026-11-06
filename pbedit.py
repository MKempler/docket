"""Price-book edits in the owner's own words: paste a price list, a CSV, or one line like "labor 145/hr".

    read      words / CSV  ->  items and settings (never invents a price: every number comes from the text)
    plan      items        ->  a list of changes against the current book, and the new file text
    save      after the owner confirms, with a backup kept for undo

Nothing is written until the owner says so. The new file is patched in place (the owner's comments and layout
stay exactly as they wrote them) and must pass the same validator as a hand-edited book, then every planned
change is checked against the re-loaded book before the preview is shown.

A model, when one is configured, is used only for lines the deterministic reader could not place, and only its
item names, units and kinds are trusted: a price it returns must appear, digit for digit, in the owner's text.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .money import cents, fmt_money, fmt_qty
from .pricebook import PriceBook, parse_pricebook


class EditError(ValueError):
    """Shown to the owner as is."""


# ---------------------------------------------------------------- reading

@dataclass
class Item:
    name: str
    price: Decimal | None = None
    unit: str = ""
    kind: str = ""                     # material | flat | "" (decided from the words)
    code: str = ""
    cost: Decimal | None = None
    markup: Decimal | None = None
    hours: Decimal | None = None       # an assembly's labor hours ("TOILET-REPLACE 2 hours")
    aliases: tuple[str, ...] = ()
    line: str = ""


@dataclass
class Reading:
    items: list[Item] = field(default_factory=list)
    settings: dict[str, Decimal] = field(default_factory=dict)   # labor_rate, labor_minimum, tax_rate, service_call
    business: str = ""
    skipped: list[str] = field(default_factory=list)
    source: str = "parser"


NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
MONEY_RE = re.compile(r"\$\s*(" + NUM + r")")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
NUMBER_RE = re.compile(NUM)
BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")
UNITS = {
    "ft": ("ft", "foot", "feet", "lf", "linear ft", "linear foot", "linear feet", "'"),
    "each": ("each", "ea", "pc", "pcs", "piece", "unit", "apiece"),
    "hour": ("hr", "hrs", "hour", "hours", "h"),
    "flat": ("flat", "flat rate", "job", "per job", "visit", "trip", "flat fee"),
    "gal": ("gal", "gallon", "gallons"), "lb": ("lb", "lbs", "pound", "pounds"), "yd": ("yd", "yard", "yards"),
    "bag": ("bag", "bags"), "box": ("box", "boxes"), "roll": ("roll", "rolls"), "set": ("set", "sets"),
}
UNIT_WORDS = {w: unit for unit, words in UNITS.items() for w in words}
UNIT_ALT = "|".join(sorted((re.escape(w) for w in UNIT_WORDS), key=len, reverse=True))
# after a price: "/ft", "per foot", "a foot", "an hour", "each", "flat" (and an optional note in brackets)
AFTER_PRICE_RE = re.compile(r"^\s*(?:(?:/|per\b|an?\b)\s*)?(?P<unit>" + UNIT_ALT + r")?(?![A-Za-z])\s*(?P<rest>.*)$", re.I)
HOURS_ONLY_RE = re.compile(r"(?<![/\w$])(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b(?!\s*(?:labor|rate))", re.I)
SECOND_PRICE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:flat|each|ea|(?:/|per\s|an?\s)\s*(?:" + UNIT_ALT + r"))(?![A-Za-z])", re.I)
LEAD_FILLER_RE = re.compile(r"^(?:(?:please|and|also|plus)\s+)*(?:(?:i|we)\s+(?:charge|get|do|bill)\s+|"
                            r"(?:add|put|include|new|create)\s+(?:(?:an?|the|some|new)\s+)*|(?:my|our|the)\s+)?", re.I)
WHERE_RE = re.compile(r"\s+(?:to|in|into|on)\s+(?:my|the|our)\s+(?:price\s?book|price\s+list|prices|list|book)\b", re.I)
TAIL_FILLER_RE = re.compile(r"(?:\s*(?:[:=@–—,-]|\b(?:is|are|costs?|runs?|at|for|goes for|price|priced at)\b))+\s*$", re.I)
# words that mark a phrase as a sentence rather than a price-list entry: those go to the model (or are reported)
CHATTY = {"i", "we", "it", "it's", "its", "is", "are", "was", "now", "so", "hey", "also", "just", "started", "raised",
          "sell", "charge", "doing", "they", "he", "she", "you", "my", "our", "going", "gonna", "want", "think",
          "supply", "house", "bumping", "bump", "make", "made", "please", "from", "then", "and"}
SIZE_NUMBER_RE = re.compile(r"\d+(?:[-/.]\d+)*(?:\s*(?:\"|''|'|in\b|inch|gal|gallon|hp|amp|amps|v\b|volt|w\b|watt|mm|ft|"
                            r"foot|x\b|-?(?:gal|in|inch|ton|piece|way|turn)\b))", re.I)
LABOR_RE = re.compile(r"\b(labor|labour|hourly|shop rate|per hour rate|hourly rate|my rate|our rate)\b", re.I)
MINIMUM_RE = re.compile(r"\bminimum\b", re.I)
TAX_RE = re.compile(r"\btax\b", re.I)
SERVICE_CALL_RE = re.compile(r"\b(service call|trip charge|trip fee|dispatch fee|call[- ]?out|diagnostic fee)\b", re.I)
JOB_STEMS = ("install", "replac", "repair", "fix", "swap", "remov", "inspect", "camera", "clean", "clear", "unclog",
             "snake", "jet", "flush", "haul", "permit", "visit", "diagnos", "reset", "rebuild", "hook", "mount", "tune",
             "maintenance", "repipe", "re-pipe", "relocat", "convert", "service", "labor", "estimate")
PART_NOUNS = {"kit", "valve", "pipe", "tank", "ring", "line", "trap", "fitting", "fittings", "faucet", "toilet",
              "heater", "disposal", "cartridge", "hose", "tube", "elbow", "tee", "coupling", "bolt", "bolts", "part"}


def read(text: str, llm: Any = None, *, book: PriceBook | None = None) -> Reading:
    """Owner's text (pasted list, CSV/TSV, a file's contents, or one sentence) -> Reading."""
    raw = (text or "").strip()
    if not raw:
        return Reading()
    table = _read_table(raw)
    if table is not None:
        return table
    out = Reading()
    leftovers: list[str] = []
    for line in _lines(raw):
        if not _read_line(line, out, book):
            leftovers.append(line)
    if leftovers and llm is not None:
        try:
            got, missed = _model_read(llm, leftovers)
            out.items += got
            out.skipped += missed
            out.source = "parser+model" if out.items else "model"
            return out
        except Exception as exc:
            out.skipped += [f'"{ln}": could not read it (model unavailable: {type(exc).__name__})' for ln in leftovers]
            return out
    out.skipped += [f'"{ln}": no clear price (write it like "item $12.50 each")' for ln in leftovers]
    return out


def _lines(raw: str) -> list[str]:
    parts: list[str] = []
    for line in raw.splitlines():
        line = BULLET_RE.sub("", line).strip()
        # one-line lists: "labor 145/hr; toilets 350 flat, 3/4 pex 2.20/ft" (never split 1,250.00)
        for piece in re.split(r";|,(?!\d{3}\b)\s+|\s+\|\s+|(?<!\d)\.\s+", line):
            piece = piece.strip(" .")
            if piece and not piece.startswith("#"):
                parts.append(piece)
    return parts


def _dec(s: str) -> Decimal | None:
    try:
        v = Decimal(str(s).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return v if v.is_finite() and v >= 0 else None


def _chatty(name: str) -> bool:
    """A sentence, not a list entry: conversational words, a bare number, or too many words."""
    words = re.findall(r"[a-z']+", name.lower())
    bare = re.search(r"(?<![\w/.\-\"'])\d+(?:\.\d+)?(?![\w/.\-\"'])", SIZE_NUMBER_RE.sub(" ", name))
    return len(words) > 7 or bool(bare) or (len(words) > 1 and bool(CHATTY & set(words)))


def _clean_name(name: str) -> str:
    name = WHERE_RE.sub(" ", name.strip())
    name = re.sub(r"^(?:(?:add|new)\s+)?(?:new\s+)?(?:item|product|part|material|service|job)\s*:\s*", "", name, flags=re.I)
    name = LEAD_FILLER_RE.sub("", name.strip())
    name = re.sub(r"\s+(?:per|/|a|an)\s*(?:" + UNIT_ALT + r")\s*$", "", name, flags=re.I)
    prev = None
    while prev != name:
        prev = name
        name = TAIL_FILLER_RE.sub("", name).strip(" -–—:=@,")
    return " ".join(name.split())


def _read_line(line: str, out: Reading, book: PriceBook | None) -> bool:
    """One phrase -> a setting or an item. False when it can't be placed without guessing."""
    low = line.lower()
    pct = PCT_RE.search(line)
    if TAX_RE.search(line) and (pct or NUMBER_RE.search(line)):
        rate = _dec(pct.group(1)) / 100 if pct else _dec(NUMBER_RE.search(line).group(0))
        if rate is None:
            return False
        out.settings["tax_rate"] = rate / 100 if rate >= 1 else rate
        return True
    money = list(MONEY_RE.finditer(line))
    if len(money) > 1:
        return False                                    # two prices in one phrase: leave it to the owner/model
    hours = HOURS_ONLY_RE.search(line)
    if not money and hours and MINIMUM_RE.search(line):
        out.settings["labor_minimum"] = _dec(hours.group(1))
        return True
    if money:
        m = money[0]
        name_part, after = line[:m.start()], line[m.end():]
        price = _dec(m.group(1))
    else:
        if hours and not re.search(r"(/|\bper\b|\ban?\b)\s*(?:h|hr|hrs|hour)\b", low):
            name = _clean_name(line[:hours.start()] + " " + line[hours.end():])
            if not name:
                return False
            out.items.append(Item(name=name, hours=_dec(hours.group(1)), line=line))
            return True
        nums = list(NUMBER_RE.finditer(line))
        if not nums:
            return False
        # the price is the last number, and nothing but a unit (or a note in brackets) may follow it
        m = nums[-1]
        name_part, after = line[:m.start()], line[m.end():]
        if m.start() > 0 and line[m.start() - 1] in "/-\"'":
            return False                                   # "3/4", "1-1/2": part of a size, not a price
        price = _dec(m.group(0))
    a = AFTER_PRICE_RE.match(after)
    if price is None or not a:
        return False
    rest = a.group("rest").strip()
    if rest and not (rest.startswith("(") and rest.endswith(")")):
        return False                                       # words after the price: ambiguous
    unit = UNIT_WORDS.get((a.group("unit") or "").lower(), "")
    name = _clean_name(name_part)
    # a second price hiding in the name ("toilets 350 flat and pex"): not ours to guess
    if SECOND_PRICE_RE.search(name):
        return False
    if LABOR_RE.search(line) or (unit == "hour" and (not name or name.lower() in {"rate", "labor", "i charge"})):
        out.settings["labor_rate"] = price
        return True
    if SERVICE_CALL_RE.search(line):
        out.settings["service_call"] = price
        return True
    if not name or _chatty(name):
        return False
    kind = ""
    if re.search(r"\b(material|part|parts)\b", low):
        kind = "material"
    elif unit == "flat" or re.search(r"\b(flat|installed|labor included)\b", low):
        kind = "flat"
    out.items.append(Item(name=name, price=price, unit=unit, kind=kind, line=line))
    return True


HEADERS = {
    "name": ("name", "item", "description", "product", "service", "task", "item name"),
    "price": ("price", "sell", "sell price", "unit price", "rate", "amount", "customer price", "retail"),
    "cost": ("cost", "unit cost", "our cost", "wholesale"),
    "markup": ("markup", "mark up", "margin"),
    "unit": ("unit", "uom", "per", "units"),
    "code": ("code", "sku", "id", "item code", "part number", "part #"),
    "kind": ("type", "kind", "category"),
    "hours": ("hours", "labor hours", "labor_hours", "hrs"),
    "aliases": ("aliases", "also called", "keywords", "other names"),
}


def _read_table(raw: str) -> Reading | None:
    first = raw.splitlines()[0]
    delim = max(",\t;", key=first.count)
    if first.count(delim) < 1:
        return None
    cells = [c.strip().lower() for c in next(csv.reader([first], delimiter=delim))]
    col: dict[str, int] = {}
    for i, c in enumerate(cells):
        for key, names in HEADERS.items():
            if c in names and key not in col:
                col[key] = i
    if "name" not in col or not ({"price", "cost", "hours"} & set(col)):
        return None
    out = Reading(source="table")
    for row in csv.reader(io.StringIO(raw), delimiter=delim):
        if row is None or [c.strip().lower() for c in row] == cells or not any(c.strip() for c in row):
            continue
        get = lambda k: (row[col[k]].strip() if k in col and col[k] < len(row) else "")  # noqa: E731
        name, line = _clean_name(get("name")), delim.join(row)
        price, cost, hours = _dec(get("price")) if get("price") else None, _dec(get("cost")) if get("cost") else None, \
            _dec(get("hours")) if get("hours") else None
        if not name or (price is None and cost is None and hours is None):
            out.skipped.append(f'"{line}": {"no name" if not name else "no price"}')
            continue
        markup = None
        if get("markup"):
            mk = _dec(get("markup").rstrip("%"))
            markup = (mk / 100 if (mk is not None and (mk > 5 or get("markup").endswith("%"))) else mk)
        kind_raw = get("kind").lower()
        kind = ("flat" if any(w in kind_raw for w in ("flat", "service", "fee", "job", "labor")) else
                "material" if any(w in kind_raw for w in ("material", "part", "product")) else "")
        aliases = tuple(a.strip().lower() for a in re.split(r"[;|/]", get("aliases")) if a.strip())
        out.items.append(Item(name=name, price=price, cost=cost, markup=markup, hours=hours, kind=kind, line=line,
                              unit=UNIT_WORDS.get(get("unit").lower(), get("unit").lower()), code=get("code").upper(),
                              aliases=aliases))
    return out


MODEL_INSTRUCTIONS = """You read a tradesperson's price list and return each priced item. You never invent or work out a price.
Rules:
- One item per thing that has a price written next to it. Skip anything with no price.
- price: copy the number exactly as written (digits and a decimal point only, no $ or commas).
- unit: ft, each, hour, flat, or the unit written. Use flat for a job with one price.
- kind: material for a part or product sold by the piece or foot; flat_rate_job for a job or service.
- also_called: up to 3 other short names the owner might use for it, only if obvious. Otherwise empty.
- Hourly labor ("I charge 145 an hour") is an item named "labor" with unit hour. Sales tax is an item named "sales tax" whose price is the percent."""

MODEL_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "price": {"type": "string"}, "unit": {"type": "string"},
        "kind": {"type": "string", "enum": ["material", "flat_rate_job"]},
        "also_called": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "price"]}}},
    "required": ["items"],
}


def _ground(name: str, price: Decimal, line: str) -> tuple[str, bool]:
    """Put back what the model dropped from the owner's words: a size just before the name ("3/4 pex"), and
    "flat" between the name and its price ("toilets are 350 flat")."""
    low = line.lower()
    at = low.find(name.lower()) if name else -1
    if at < 0:
        first = name.lower().split()[0] if name.split() else ""
        at = low.find(first) if first else -1
        if at < 0:
            return name, False
    before = line[:at].rstrip().split()
    if before and re.fullmatch(r"\d+(?:[-/.]\d+)*(?:\"|''|-?(?:gal|in|inch|hp))?", before[-1], re.I):
        name = f"{before[-1]} {name}"
    after = low[at:]
    price_at = next((m.start() for m in NUMBER_RE.finditer(after) if _dec(m.group(0)) == price), -1)
    tail = after[price_at:price_at + 30] if price_at >= 0 else ""
    flat = bool(re.search(r"^\S+\s*(?:flat|per job|a job)\b", tail))
    return name, flat


def _model_read(llm: Any, lines: list[str]) -> tuple[list[Item], list[str]]:
    text = "\n".join(lines)
    result = llm.complete_structured(instructions=MODEL_INSTRUCTIONS, input=[{"type": "text", "text": text}],
                                     json_schema=MODEL_SCHEMA, schema_name="docket_price_list", temperature=0,
                                     timeout=90, purpose="docket price list")
    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict):
        raise ValueError("no items")
    written = {_dec(n.group(0)) for n in NUMBER_RE.finditer(text)}
    items, used_lines, refused = [], set(), []
    for raw in parsed.get("items") or []:
        if not isinstance(raw, dict):
            continue
        name, price = str(raw.get("name") or "").strip(), _dec(str(raw.get("price") or ""))
        if not name:
            continue
        if price is None or price not in written:          # the guard: no price the owner didn't write
            refused.append(f'"{name}": write its price as a number (Docket only takes prices you typed)')
            continue
        line = next((ln for ln in lines if name.lower().split()[0] in ln.lower() and
                     any(_dec(n.group(0)) == price for n in NUMBER_RE.finditer(ln))), "")
        used_lines.add(line)
        name, flat = _ground(name, price, line)
        unit = UNIT_WORDS.get(str(raw.get("unit") or "").lower().strip(), str(raw.get("unit") or "").lower().strip())
        if TAX_RE.search(name) or LABOR_RE.search(name) or SERVICE_CALL_RE.search(name):
            items.append(Item(name=name, price=price, unit=unit, line=line or text[:80], kind="setting"))
            continue
        aliases = tuple(" ".join(a.lower().split()) for a in (raw.get("also_called") or [])[:3]
                        if isinstance(a, str) and len(a.strip()) > 2)
        items.append(Item(name=name, price=price, unit="flat" if flat else unit, line=line or text[:80], aliases=aliases,
                          kind="flat" if flat or raw.get("kind") == "flat_rate_job" else "material"))
    missed = [f'"{ln}": no price Docket could confirm in your words' for ln in lines
              if ln not in used_lines and not refused and NUMBER_RE.search(ln) and not any(_dec(n.group(0)) == it.price for it in items
                                                                  for n in NUMBER_RE.finditer(ln))]
    return items, refused + missed


# ---------------------------------------------------------------- planning

@dataclass
class Change:
    action: str          # add | update | remove | setting | alias
    code: str
    label: str           # one line the owner reads in the preview


@dataclass
class Plan:
    changes: list[Change]
    text: str            # the full new price book, validated
    base_hash: str
    skipped: list[str]
    summary: str = ""

    @property
    def token(self) -> str:
        return hashlib.sha256((self.base_hash + self.text).encode()).hexdigest()[:10]

    def preview(self, book: PriceBook | None = None) -> str:
        if not self.changes:
            out = "No changes to make."
        else:
            out = f"Price book changes ({len(self.changes)}), not saved yet:\n" + "\n".join(
                f"  {c.label}" for c in self.changes)
        if self.skipped:
            out += "\nNot changed:" + "".join(f"\n  ? {s[:240]}" for s in self.skipped[:8])
            if len(self.skipped) > 8:
                out += f"\n  … and {len(self.skipped) - 8} more"
        if self.summary:
            out += f"\n{self.summary}"
        return out

    def to_json(self) -> dict[str, Any]:
        return {"token": self.token, "base_hash": self.base_hash, "text": self.text, "skipped": self.skipped,
                "summary": self.summary, "changes": [c.__dict__ for c in self.changes]}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Plan":
        return cls([Change(**c) for c in d["changes"]], d["text"], d["base_hash"], d.get("skipped", []),
                   d.get("summary", ""))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _norm(text: str) -> str:
    from .intake import _tokens
    t = re.sub(r"(\d)\s*(?:\"|''|\binch(?:es)?\b|\bin\b\.?)", r"\1", text.lower())
    return " ".join(_tokens(t.replace("-", " ")))


def _index(book: PriceBook) -> dict[str, tuple[str, str]]:
    """Normalized name/alias/code -> (section, code), for alias clash checks."""
    idx: dict[str, tuple[str, str]] = {}
    for section, group in (("assemblies", book.assemblies), ("fees", book.fees), ("materials", book.materials)):
        for code, e in group.items():
            for phrase in (code, e.name, *e.aliases):
                idx.setdefault(_norm(phrase), (section, code))
    return idx


def _match(book: PriceBook, item: Item) -> tuple[tuple[str, str] | None, list[str]]:
    """(section, code) for the existing item this line means, or (None, candidates) when it's ambiguous.

    A code wins. Then a line with a price looks at parts and flat-rate items before jobs, and a line with
    hours looks at jobs first ("wax ring 12" is the part; "drain 2 hours" is the job). In each group an exact
    name or alias beats a loose match, and a loose match (every word of the line in one item's name or
    alias: "3/4 pex" -> '3/4" PEX pipe') counts only when exactly one item fits."""
    groups = {"materials": book.materials, "fees": book.fees, "assemblies": book.assemblies}
    for code in filter(None, (item.code, item.name.strip().upper())):
        for section, group in groups.items():
            if code in group:
                return (section, code), []
    words = _norm(item.name)
    if not words:
        return None, []
    tiers = [("materials", "fees"), ("assemblies",)]
    if item.kind == "flat":
        tiers = [("fees",), ("assemblies",)]               # "toilets are 350 flat" is never the toilet itself
    if item.hours is not None and item.price is None:
        tiers.reverse()
    ambiguous: list[str] = []
    for tier in tiers:
        exact = [(sec, code) for sec in tier for code, e in groups[sec].items()
                 if any(_norm(ph) == words for ph in (code, e.name, *e.aliases))]
        if exact and ambiguous and exact[0][0] == "assemblies":
            return None, ambiguous + [exact[0][1]]          # "water heater $1200": the heater, the kit, or the job?
        if exact:
            return exact[0], []
        wanted = set(words.split())
        loose = [(sec, code) for sec in tier for code, e in groups[sec].items()
                 if any(wanted <= set(_norm(ph).split()) for ph in (e.name, *e.aliases))]
        if len(loose) == 1:
            return loose[0], []
        if loose and not ambiguous:
            ambiguous = [code for _, code in loose]
    return None, ambiguous


def _fmt_dec(v: Decimal) -> str:
    v = Decimal(v)
    return format(v.quantize(Decimal("0.01")) if v == v.quantize(Decimal("0.01")) else v.normalize(), "f")


def _code_for(name: str, taken: set[str]) -> str:
    words = [re.sub(r"[^A-Za-z0-9]", "", w).upper() for w in re.split(r"[\s/\-]+", name)]
    words = [w for w in words if w and w not in {"A", "AN", "THE", "OF", "AND", "FOR", "WITH"}]
    base = "-".join(words)[:22].strip("-") or "ITEM"
    code, n = base, 2
    while code in taken or code == "LABOR":
        code, n = f"{base}-{n}", n + 1
    return code


def _is_job(item: Item) -> bool:
    if item.kind in {"flat", "material"}:
        return item.kind == "flat"
    if item.unit in {"flat", "hour"}:
        return True
    toks = re.findall(r"[a-z-]+", item.name.lower())
    if toks and toks[-1] in PART_NOUNS:
        return False
    return any(t.startswith(s) for t in toks for s in JOB_STEMS)


def plan(current_text: str, book: PriceBook, reading: Reading, *, removals: tuple[str, ...] = (),
         aliases: tuple[tuple[str, str], ...] = ()) -> Plan:
    """Turn what was read into concrete edits of current_text. Raises EditError if the result wouldn't load."""
    doc = _Doc(current_text)
    cur = book.currency
    changes: list[Change] = []
    checks: list = []
    skipped = list(reading.skipped)
    idx = _index(book)
    taken = set(book.materials) | set(book.fees) | set(book.assemblies)

    s = reading.settings
    for item in list(reading.items):
        if item.kind == "setting":                         # model-read labor / tax / service call lines
            reading.items.remove(item)
            key = ("tax_rate" if TAX_RE.search(item.name) else "service_call" if SERVICE_CALL_RE.search(item.name)
                   else "labor_rate")
            s[key] = (item.price / 100 if key == "tax_rate" and item.price >= 1 else item.price)
    if "labor_rate" in s and s["labor_rate"] != book.labor_rate:
        if s["labor_rate"] <= 0:
            raise EditError("Labor rate must be more than 0.")
        doc.set(["labor", "rate"], _q(_fmt_dec(s["labor_rate"])))
        changes.append(Change("setting", "labor.rate", f"~ Labor rate {fmt_money(book.labor_rate, cur)}/h → "
                                                       f"{fmt_money(s['labor_rate'], cur)}/h"))
        checks.append(lambda b, v=s["labor_rate"]: b.labor_rate == v)
    if "labor_minimum" in s and s["labor_minimum"] != book.labor_minimum_hours:
        doc.set(["labor", "minimum_hours"], _q(fmt_qty(s["labor_minimum"])))
        changes.append(Change("setting", "labor.minimum_hours", f"~ Labor minimum {fmt_qty(book.labor_minimum_hours)} h"
                                                                f" → {fmt_qty(s['labor_minimum'])} h"))
        checks.append(lambda b, v=s["labor_minimum"]: b.labor_minimum_hours == v)
    if "tax_rate" in s and s["tax_rate"] != book.tax_rate:
        if s["tax_rate"] >= 1:
            raise EditError("Give tax as a percent, e.g. 8.25%.")
        doc.set(["tax", "rate"], _q(format(s["tax_rate"].normalize(), "f")))
        changes.append(Change("setting", "tax.rate", f"~ {book.tax_name} {fmt_qty(book.tax_rate * 100)}% → "
                                                     f"{fmt_qty(s['tax_rate'] * 100)}%"))
        checks.append(lambda b, v=s["tax_rate"]: b.tax_rate == v)
    if "service_call" in s:
        sc = book.service_call
        if sc is None:
            doc.add_section("service_call", ["code: SERVICE-CALL", "name: Service call",
                                             f"amount: {_q(_fmt_dec(s['service_call']))}"])
            changes.append(Change("setting", "service_call", f"+ Service call {fmt_money(s['service_call'], cur)} "
                                                             f"(on every quote)"))
        elif sc.amount != s["service_call"]:
            doc.set(["service_call", "amount"], _q(_fmt_dec(s["service_call"])))
            changes.append(Change("setting", "service_call", f"~ {sc.name} {fmt_money(sc.amount, cur)} → "
                                                             f"{fmt_money(s['service_call'], cur)}"))
        checks.append(lambda b, v=s["service_call"]: b.service_call is not None and b.service_call.amount == v)

    seen: set[str] = set()
    added: dict[str, str] = {}                              # new items in this same change, by normalized name
    for item in reading.items:
        said = f"\"{item.line or item.name}\""
        hit, maybe = _match(book, item)
        if hit is None and maybe:
            jobs = item.price is None or all(c in book.assemblies for c in maybe)
            how = ("2 hours" if jobs else "$" + _fmt_dec(item.price))
            why = " (jobs are priced from labor hours plus parts)" if jobs and item.price is not None else ""
            skipped.append(f"{said}: could be {' or '.join(maybe[:4])}{why}. Use the code, e.g. \"{maybe[0]} {how}\"")
            continue
        if hit and hit[1] in seen:
            skipped.append(f"{said}: {hit[1]} is already changed above")
            continue
        if hit:
            section, code = hit
            changes_for, check = _update(doc, book, section, code, item, skipped)
            if changes_for:
                seen.add(code)
            elif not any(x.startswith(said) for x in skipped):
                skipped.append(f"{said}: {code} already has {'those hours' if item.price is None else 'that price'}")
            changes += changes_for
            if check:
                checks.append(check)
            continue
        if _norm(item.name) in added:
            skipped.append(f"{said}: listed twice; kept the first")
            continue
        if item.hours is not None and item.price is None:
            skipped.append(f"\"{item.line or item.name}\": no job by that name to set hours on")
            continue
        price = item.price if item.price is not None else (
            cents(item.cost * (1 + (item.markup or 0))) if item.cost is not None else None)
        if price is None:
            skipped.append(f"\"{item.line or item.name}\": no price")
            continue
        code = item.code if item.code and item.code not in taken else _code_for(item.name, taken)
        taken.add(code)
        added[_norm(item.name)] = code
        aliases_ok = [a for a in item.aliases if _norm(a) not in idx and a.lower() != item.name.lower()]
        for key in map(_norm, aliases_ok):
            idx[key] = ("", code)
        alias_txt = f", aliases: {json.dumps(aliases_ok)}" if aliases_ok else ""
        name_txt = json.dumps(item.name[:80], ensure_ascii=False)
        if _is_job(item):
            unit = "each" if item.unit in {"", "flat"} else item.unit
            doc.add_entry("fees", code, f"{{ name: {name_txt}, unit: {unit}, amount: {_q(_fmt_dec(price))}{alias_txt} }}")
            per = "" if unit == "each" else f"/{unit}"  # flat-rate: "$350.00"
            changes.append(Change("add", code, f"+ {code}  {item.name} · {fmt_money(price, cur)}{per}  (flat-rate job)"))
            checks.append(lambda b, c=code, v=price: c in b.fees and b.fees[c].amount == v)
        else:
            unit = item.unit if item.unit not in {"", "flat", "hour"} else "each"
            cost = item.cost if item.cost is not None else price
            markup = item.markup if item.cost is not None and item.markup is not None else Decimal(0)
            doc.add_entry("materials", code, f"{{ name: {name_txt}, unit: {unit}, cost: {_q(_fmt_dec(cost))}, "
                                             f"markup: {_q(_fmt_dec(markup))}{alias_txt} }}")
            changes.append(Change("add", code, f"+ {code}  {item.name} · {fmt_money(price, cur)}{_per(unit)}  (material)"))
            checks.append(lambda b, c=code, v=price: c in b.materials and b.materials[c].price == v)

    for code in removals:
        code = code.strip().upper()
        found = book.lookup(code)
        if not found or found[0] == "labor":
            raise EditError(f"{code} is not in your price book.")
        users = [a.code for a in book.assemblies.values() if any(m.code == code for m in a.materials)]
        users += [f.code for f in book.fees.values() if code in f.limit_to]
        if users:
            raise EditError(f"Can't remove {code}: {', '.join(users)} use{'s' if len(users) == 1 else ''} it. "
                            f"Remove or change {'that' if len(users) == 1 else 'those'} first.")
        section = {"assembly": "assemblies", "material": "materials", "fee": "fees"}[found[0]]
        doc.remove_entry(section, code)
        changes.append(Change("remove", code, f"− {code}  {found[1].name}"))
        checks.append(lambda b, c=code: b.lookup(c) is None)

    for code, alias in aliases:
        code, alias = code.strip().upper(), " ".join(alias.lower().split())
        found = book.lookup(code)
        if not found or found[0] == "labor":
            raise EditError(f"{code} is not in your price book.")
        owner = idx.get(_norm(alias))
        if owner and owner[1] != code:
            raise EditError(f'"{alias}" already means {owner[1]}.')
        if owner:
            skipped.append(f'"{alias}" already means {code}')
            continue
        section = {"assembly": "assemblies", "material": "materials", "fee": "fees"}[found[0]]
        doc.add_alias(section, code, alias)
        changes.append(Change("alias", code, f'+ "{alias}" now means {code} ({found[1].name})'))
        checks.append(lambda b, c=code, a=alias: a in b.lookup(c)[1].aliases)

    new_text = doc.text
    new_book, errors = parse_pricebook(new_text)
    if errors or new_book is None:
        raise EditError("Those changes would break the price book: " + "; ".join(str(e) for e in errors[:5]))
    for check in checks:
        if not check(new_book):
            raise EditError("Docket couldn't apply those changes safely. Edit the file by hand: "
                            f"{book.source or 'pricebook.yaml'}")
    counts = (f"Checked: the new price book is valid ({len(new_book.assemblies)} jobs, {len(new_book.fees)} flat-rate "
              f"items, {len(new_book.materials)} materials).")
    return Plan(changes, new_text, text_hash(current_text), skipped, counts if changes else "")


def _q(s: str) -> str:
    return f'"{s}"'


def _update(doc: "_Doc", book: PriceBook, section: str, code: str, item: Item, skipped: list[str]):
    cur = book.currency
    if section == "assemblies":
        a = book.assemblies[code]
        if item.hours is None:
            skipped.append(f"\"{item.line or item.name}\": {code} ({a.name}) is priced from its labor hours and "
                           f"parts. Change its hours instead, e.g. \"{code} 2 hours\"")
            return [], None
        if item.hours == a.labor_hours:
            return [], None
        doc.set(["assemblies", code, "labor_hours"], _q(fmt_qty(item.hours)))
        return ([Change("update", code, f"~ {code}  {a.name}: labor {fmt_qty(a.labor_hours)} h → {fmt_qty(item.hours)} h")],
                lambda b, v=item.hours: b.assemblies[code].labor_hours == v)
    if item.hours is not None and item.price is None:
        skipped.append(f"\"{item.line or item.name}\": {code} has a set price, not hours")
        return [], None
    if section == "fees":
        f = book.fees[code]
        price = item.price if item.price is not None else item.cost
        if price is None or price == f.amount:
            return [], None
        doc.set(["fees", code, "amount"], _q(_fmt_dec(price)))
        return ([Change("update", code, f"~ {code}  {f.name}: {fmt_money(f.amount, cur)} → {fmt_money(price, cur)}")],
                lambda b, v=price: b.fees[code].amount == v)
    m = book.materials[code]
    if item.cost is not None:                               # a cost (and markup) column: take them as given
        cost, markup = item.cost, item.markup if item.markup is not None else m.markup
    else:
        price = item.price
        if price is None or price == m.price:
            return [], None
        cost, markup = price, Decimal(0)
        if m.markup:                                        # keep the owner's markup when it reproduces the price
            back = (price / (1 + m.markup)).quantize(Decimal("0.0001"))
            if cents(back * (1 + m.markup)) == price:
                cost, markup = back, m.markup
    new_price = cents(cost * (1 + markup))
    if new_price == m.price and cost == m.cost:
        return [], None
    doc.set(["materials", code, "cost"], _q(_fmt_dec(cost)))
    if markup != m.markup:
        doc.set(["materials", code, "markup"], _q(_fmt_dec(markup)))
    note = "" if markup == m.markup else f" (markup {fmt_qty(m.markup * 100)}% → {fmt_qty(markup * 100)}%)"
    return ([Change("update", code, f"~ {code}  {m.name}: {fmt_money(m.price, cur)} → {fmt_money(new_price, cur)}"
                                    f"{_per(m.unit)}{note}")],
            lambda b, v=new_price: b.materials[code].price == v)


def _per(unit: str) -> str:
    return " each" if unit == "each" else f"/{unit}"


# ---------------------------------------------------------------- text patching (keeps the owner's layout)

class _Doc:
    """Edits YAML text in place. After every edit the text is re-parsed, so positions are always fresh."""

    def __init__(self, text: str):
        self.text = text if text.endswith("\n") else text + "\n"

    def _load(self):
        from ruamel.yaml import YAML
        return YAML(typ="rt").load(self.text)

    def _lines(self) -> list[str]:
        return self.text.splitlines(keepends=True)

    def _node(self, path: list[str]):
        node = self._load()
        for key in path:
            node = node[key]
        return node

    def set(self, path: list[str], value: str) -> None:
        parent, key = self._node(path[:-1]), path[-1]
        lines = self._lines()
        if key in parent:
            line, col = parent.lc.value(key)
            text = lines[line]
            end = _token_end(text, col)
            lines[line] = text[:col] + value + text[end:]
        elif _is_flow(parent):
            line, col = self._flow_start(path[:-1])
            close = _matching(lines[line], col)
            if close < 0:
                raise EditError("Docket couldn't edit that entry (it spans several lines). Edit the file by hand.")
            body = lines[line][col + 1:close].rstrip()
            sep = ", " if body.strip() else " "
            lines[line] = lines[line][:col + 1] + body + f"{sep}{key}: {value} " + lines[line][close:]
        else:
            first = next(iter(parent))
            line, col = parent.lc.key(first)
            lines.insert(line + 1, " " * col + f"{key}: {value}\n")
        self.text = "".join(lines)

    def _flow_start(self, path: list[str]) -> tuple[int, int]:
        grand = self._node(path[:-1])
        return grand.lc.value(path[-1])

    def _section_end(self, data, section: str) -> int:
        keys = list(data)
        i = keys.index(section)
        lines = self._lines()
        end = data.lc.key(keys[i + 1])[0] if i + 1 < len(keys) else len(lines)
        while end > 0 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        return end

    def add_entry(self, section: str, code: str, flow: str) -> None:
        data = self._load()
        lines = self._lines()
        if section not in data or data[section] is None:
            if section in data:                                 # "fees:" with nothing under it
                line = data.lc.key(section)[0]
                lines.insert(line + 1, f"  {code}: {flow}\n")
            else:
                if lines and lines[-1].strip():
                    lines.append("\n")
                lines += [f"{section}:\n", f"  {code}: {flow}\n"]
            self.text = "".join(lines)
            return
        sec = data[section]
        if _is_flow(sec) or not len(sec):
            if len(sec):
                raise EditError(f"Docket can't add to a one-line '{section}:' section. Edit the file by hand.")
            line, col = data.lc.value(section)                  # "fees: {}  # note" -> "fees:  # note" + entry
            close = _matching(lines[line], col)
            tail = lines[line][close + 1:].strip() if close >= 0 else ""
            lines[line] = lines[line][:col].rstrip() + (f"  {tail}" if tail.startswith("#") else "") + "\n"
            lines.insert(line + 1, f"  {code}: {flow}\n")
            self.text = "".join(lines)
            return
        indent = sec.lc.key(next(iter(sec)))[1]
        end = self._section_end(data, section)
        lines.insert(end, " " * indent + f"{code}: {flow}\n")
        self.text = "".join(lines)

    def add_section(self, section: str, body: list[str]) -> None:
        data = self._load()
        lines = self._lines()
        keys = list(data)
        at = data.lc.key("travel_zones")[0] if "travel_zones" in keys else len(lines)
        block = [f"{section}:\n"] + [f"  {b}\n" for b in body] + ["\n"]
        if at == len(lines) and lines and lines[-1].strip():
            block.insert(0, "\n")
        lines[at:at] = block
        self.text = "".join(lines)

    def remove_entry(self, section: str, code: str) -> None:
        data = self._load()
        sec = data[section]
        if _is_flow(sec):
            raise EditError(f"Docket can't edit a one-line '{section}:' section. Edit the file by hand.")
        keys = list(sec)
        i = keys.index(code)
        start = sec.lc.key(code)[0]
        lines = self._lines()
        end = sec.lc.key(keys[i + 1])[0] if i + 1 < len(keys) else self._section_end(data, section)
        del lines[start:end]
        self.text = "".join(lines)
        if not len(self._load()[section]):
            self.text = re.sub(rf"(?m)^{re.escape(section)}:(\s*#.*)?$", f"{section}: {{}}\\1", self.text, count=1)

    def add_alias(self, section: str, code: str, alias: str) -> None:
        entry = self._node([section, code])
        lines = self._lines()
        quoted = json.dumps(alias, ensure_ascii=False)
        if "aliases" in entry and entry["aliases"] is not None and _is_flow(entry["aliases"]):
            line, col = entry.lc.value("aliases")
            close = _matching(lines[line], col)
            if close < 0:
                raise EditError("Docket couldn't edit that alias list. Edit the file by hand.")
            body = lines[line][col + 1:close].rstrip()
            lines[line] = lines[line][:col + 1] + (body + ", " if body.strip() else "") + quoted + lines[line][close:]
            self.text = "".join(lines)
        elif "aliases" in entry and entry["aliases"]:
            seq = entry["aliases"]
            line, col = seq.lc.item(len(seq) - 1)
            lines.insert(line + 1, " " * (col - 2) + f"- {quoted}\n")
            self.text = "".join(lines)
        else:
            self.set([section, code, "aliases"], f"[{quoted}]")


def _is_flow(node) -> bool:
    try:
        return bool(node.fa.flow_style())
    except Exception:
        return False


def _token_end(text: str, col: int) -> int:
    if col < len(text) and text[col] in "\"'":
        q, i = text[col], col + 1
        while i < len(text):
            if text[i] == "\\" and q == '"':
                i += 2
                continue
            if text[i] == q:
                if q == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                return i + 1
            i += 1
        return len(text.rstrip("\n"))
    i = col
    while i < len(text) and text[i] not in ",}]#\n":
        i += 1
    while i > col and text[i - 1] == " ":
        i -= 1
    return i


def _matching(text: str, col: int) -> int:
    """Index of the bracket closing the one at text[col] on the same line, or -1."""
    pairs = {"{": "}", "[": "]"}
    if col >= len(text) or text[col] not in pairs:
        return -1
    depth, i, quote = 0, col, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# ---------------------------------------------------------------- starting fresh

def starter_text(reading: Reading, business: str) -> str:
    s = reading.settings
    if not business:
        raise EditError("Name your business, e.g. /pricebook new Rivera Plumbing, labor 120/hr, tax 7%")
    if "labor_rate" not in s:
        raise EditError("Include your hourly labor rate, e.g. /pricebook new Rivera Plumbing, labor 120/hr, tax 7%")
    tax = s.get("tax_rate", Decimal(0))
    sc = s.get("service_call")
    lines = [
        f"# {business} price book. Every price Docket quotes comes from this file.",
        "# Add items with /pricebook add, or edit by hand and run /pricebook validate.", "",
        "business:", f"  name: {json.dumps(business, ensure_ascii=False)}", "", "currency: USD", "",
        "tax:", "  name: Sales tax", f"  rate: \"{format(tax.normalize(), 'f')}\"", "  applies_to: [materials]", "",
        "labor:", f"  rate: \"{_fmt_dec(s['labor_rate'])}\"",
        f"  minimum_hours: \"{fmt_qty(s.get('labor_minimum', Decimal(1)))}\"", "",
        "multipliers:              # labor only; when several apply, the highest one wins",
        '  after_hours: "1.5"', '  weekend: "1.25"', '  emergency: "2.0"', "",
    ]
    if sc is not None:
        lines += ["service_call:", "  code: SERVICE-CALL", "  name: Service call", f"  amount: \"{_fmt_dec(sc)}\"", ""]
    lines += ["travel_zones: {}          # by ZIP code, e.g.  A: { amount: \"0.00\", zips: [\"62701\"] }", "",
              "materials: {}", "", "fees: {}", "", "assemblies: {}", ""]
    return "\n".join(lines)


def business_from(text: str) -> str:
    first = re.split(r"[,;\n]", text.strip(), maxsplit=1)[0].strip()
    return "" if re.search(r"\d", first) else first[:80]


# ---------------------------------------------------------------- saving

PENDING_KEY = "pricebook_pending"
PREVIEW_TTL = 30 * 60          # a preview older than this must be made again before it can be saved


def stage(store: Any, p: Plan) -> str:
    store.set_meta(PENDING_KEY, json.dumps({**p.to_json(), "staged_at": time.time()}))
    return p.token


def pending(store: Any, *, now: float | None = None) -> Plan | None:
    """The change waiting for review, or None (also None once it is older than PREVIEW_TTL)."""
    raw = store.get_meta(PENDING_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if (now or time.time()) - float(data.get("staged_at", 0)) > PREVIEW_TTL:
            return None
        return Plan.from_json(data)
    except Exception:
        return None


def cancel(store: Any, token: str = "") -> str:
    p = pending(store)
    if p is None or (token and p.token != token):
        return "Nothing to cancel."
    store.set_meta(PENDING_KEY, "")
    return "Cancelled. Your price book is unchanged."


def save(store: Any, own_path: Path, current_path: Path, token: str = "") -> str:
    """Write the staged plan to own_path, keeping a backup for undo. Refuses stale previews."""
    p = pending(store)
    if p is None:
        raise EditError("There is no price book change waiting (previews expire after 30 minutes). "
                        "Preview it again with /pricebook add <changes>.")
    if token and token != p.token:
        raise EditError("That preview is out of date. Preview the change again.")
    current = current_path.read_text(encoding="utf-8")
    if text_hash(current) != p.base_hash:
        store.set_meta(PENDING_KEY, "")
        raise EditError("The price book changed since that preview. Preview the change again.")
    _, errors = parse_pricebook(p.text)
    if errors:
        raise EditError("The staged change no longer loads; nothing was saved.")
    history = own_path.parent / "pricebook-history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns():020d}"      # sorts in save order
    if own_path.exists():
        (history / f"{stamp}.yaml").write_text(own_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        (history / f"{stamp}.none").write_text("", encoding="utf-8")   # undo goes back to the demo book
    tmp = own_path.with_suffix(".yaml.tmp")
    tmp.write_text(p.text, encoding="utf-8")
    os.replace(tmp, own_path)
    store.set_meta(PENDING_KEY, "")
    n = len(p.changes)
    listed = "".join(f"\n  {c.label}" for c in p.changes[:12]) + (f"\n  … and {n - 12} more" if n > 12 else "")
    return f"Saved {n} change{'s' if n != 1 else ''} to your price book:{listed}\n/pricebook undo puts it back."


def undo(store: Any, own_path: Path) -> str:
    history = own_path.parent / "pricebook-history"
    backups = sorted(history.glob("*.*")) if history.exists() else []
    backups = [b for b in backups if b.suffix in {".yaml", ".none"}]
    if not backups:
        return "Nothing to undo."
    last = backups[-1]
    if last.suffix == ".none":
        own_path.unlink(missing_ok=True)
        msg = "Undone. Docket is back on the demo price book."
    else:
        tmp = own_path.with_suffix(".yaml.tmp")
        tmp.write_text(last.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(tmp, own_path)
        msg = "Undone. Your price book is back to how it was before the last change."
    last.unlink()
    store.set_meta(PENDING_KEY, "")
    return msg


# ---------------------------------------------------------------- one entry point for every surface

def propose(store: Any, current_path: Path, text: str = "", *, llm: Any = None, removals: tuple[str, ...] = (),
            aliases: tuple[tuple[str, str], ...] = (), fresh: bool = False) -> Plan:
    """Preview a change against the book at current_path and stage it for save(). Nothing is written."""
    current = current_path.read_text(encoding="utf-8")
    if fresh:
        reading = read(text)                                # deterministic: a name, a labor rate, maybe tax
        business = business_from(text)
        new_text = starter_text(reading, business)
        new_book, errors = parse_pricebook(new_text)
        if errors or new_book is None:
            raise EditError("Docket couldn't start that price book: " + "; ".join(map(str, errors[:3])))
        tax = f", {fmt_qty(new_book.tax_rate * 100)}% tax on materials" if new_book.tax_rate else ", no sales tax"
        sc = f", {fmt_money(new_book.service_call.amount)} service call" if new_book.service_call else ""
        p = Plan([Change("new", "", f"★ New price book for {business}: labor "
                                    f"{fmt_money(new_book.labor_rate, new_book.currency)}/h{tax}{sc}. No items yet; "
                                    f"add them with /pricebook add")],
                 new_text, text_hash(current), [],
                 "Replaces the current price book. It is kept as a backup: /pricebook undo brings it back.")
    else:
        book, errors = parse_pricebook(current, source=str(current_path))
        if errors or book is None:
            raise EditError(f"Your price book has {len(errors)} error(s); fix them first (/pricebook validate).")
        if not text.strip() and not removals and not aliases:
            raise EditError('Tell me what to change, e.g. /pricebook add labor 145/hr, 3/4" PEX $2.20/ft')
        p = plan(current, book, read(text, llm, book=book), removals=removals, aliases=aliases)
    if p.changes:
        stage(store, p)
    return p
