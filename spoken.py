"""Spoken-style text -> the written forms intake expects. Used on every job description (typed or transcribed).

Voice transcripts and people typing the way they talk write numbers as words and split ZIP codes:
"six two seven oh seven", "627-01", "forty two Linden Avenue", "an hour and a half". This turns those into
digits. It only rewrites numbers; it never adds, drops or reorders words, and it leaves a lone "one" alone
("put a new one in" must not become "put a new 1 in").
"""

from __future__ import annotations

import re

DIGIT = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
         "six": "6", "seven": "7", "eight": "8", "nine": "9"}
UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
NUMBER_WORD = "|".join(sorted([*UNITS, *TENS, "hundred", "thousand", "zero"], key=len, reverse=True))

# five single digits in a row, spoken one at a time: "six two seven oh seven", "6, 2, 7, 0, 7"
DIGIT_WORD = r"(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine|\d)"
SPELLED_ZIP_RE = re.compile(rf"\b{DIGIT_WORD}(?:[\s,.-]+{DIGIT_WORD}){{4}}\b", re.I)
# a ZIP broken into groups: "627-01", "62 707", "6-2-7-0-7" (exactly five digits, never money)
SPLIT_ZIP_RE = re.compile(r"(?<![\d$.,])\d{1,4}(?:[\s-]\d{1,4}){1,4}(?![\d.,]?\d)")
CARDINAL_RE = re.compile(rf"\b(?:{NUMBER_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{NUMBER_WORD}))*\b", re.I)
HALF_HOURS = [
    (re.compile(r"\b(?:an?|1|one)\s+(extra\s+)?hour\s+and\s+a\s+half\b", re.I), r"1.5 \1hours"),
    (re.compile(r"\b(\d+)\s+and\s+a\s+half\s+(extra\s+)?hours?\b", re.I), r"\1.5 \2hours"),
    (re.compile(r"\b(\d+)\s+(extra\s+)?hours?\s+and\s+a\s+half\b", re.I), r"\1.5 \2hours"),
    (re.compile(r"\b(?:half\s+an\s+hour|a\s+half[\s-]hour)\b", re.I), "0.5 hours"),
    (re.compile(r"\ban\s+(extra\s+)?hour\b", re.I), r"1 \1hour"),
]


def _cardinal(words: str) -> int | None:
    total, current = 0, 0
    for w in re.split(r"[\s-]+", words.lower()):
        if w == "and":
            continue
        if w in UNITS:
            current += UNITS[w]
        elif w in TENS:
            current += TENS[w]
        elif w == "zero":
            current += 0
        elif w == "hundred":
            current = (current or 1) * 100
        elif w == "thousand":
            total, current = total + (current or 1) * 1000, 0
        else:
            return None
    return total + current


def _zip_from_spelled(m: re.Match) -> str:
    digits = [DIGIT.get(t.lower(), t) for t in re.findall(DIGIT_WORD, m.group(0), re.I)]
    return "".join(digits) if len(digits) == 5 else m.group(0)


def _zip_from_split(m: re.Match) -> str:
    raw = m.group(0)
    digits = re.sub(r"[\s-]", "", raw)
    if len(digits) != 5 or not re.search(r"[\s-]", raw):
        return raw
    groups = re.split(r"[\s-]", raw)
    if len(groups) == 2 and len(groups[0]) <= 2 and len(groups[1]) <= 2:
        return raw                                     # "2 70" style small numbers can't be a ZIP
    return digits


def _cardinal_sub(m: re.Match) -> str:
    text = m.group(0)
    if text.lower() == "one":
        return text                                    # "a new one", "the old one"
    value = _cardinal(text)
    return text if value is None else str(value)


def normalize(text: str) -> str:
    """Numbers as digits, ZIPs joined, half hours as decimals. Everything else unchanged."""
    if not text:
        return text
    t = SPELLED_ZIP_RE.sub(_zip_from_spelled, text)
    t = CARDINAL_RE.sub(_cardinal_sub, t)
    t = re.sub(r"\bone\s+and\s+a\s+half\b", "1 and a half", t, flags=re.I)
    t = SPELLED_ZIP_RE.sub(_zip_from_spelled, t)       # mixed forms, e.g. "6 2 seven 0 7"
    t = SPLIT_ZIP_RE.sub(_zip_from_split, t)
    t = re.sub(r"(?<=[A-Za-z])(\d{5})\b", r" \1", t)    # "S62711": a ZIP glued to the word before it
    for pattern, repl in HALF_HOURS:
        t = pattern.sub(repl, t)
    return t
