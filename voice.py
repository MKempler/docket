"""Voice notes: teach the speech recognizer the owner's own trade words.

Hermes transcribes voice notes before the agent sees them. Its pre_transcription hook lets a plugin add a
vocabulary prompt (Whisper's `initial_prompt` / the cloud `prompt`). Docket builds one from the owner's price
book (job names, part names, the words they actually use for them) so "PEX", "P-trap" and "angle stop" come out
spelled the way the price book spells them. The prompt is only a spelling hint: it never adds words to what was
said, and Docket still prices only what the transcript names.

Whisper reads at most ~224 prompt tokens, so the prompt stays short: every item's name, then aliases while
there is room.
"""

from __future__ import annotations

import re
from typing import Any

from .pricebook import PriceBook

MAX_CHARS = 700            # ~170 tokens: well inside Whisper's prompt window, leaving room for Hermes's own prompt
COMMON = {"replace", "install", "new", "old", "the", "a", "an", "of", "and", "with", "for", "on", "in", "each"}


def vocab_prompt(book: PriceBook, *, max_chars: int = MAX_CHARS) -> str:
    """A short list of the owner's trade terms: every item's name, then their other words for items."""
    phrases: list[str] = []
    words: set[str] = set()

    def add(phrase: str) -> None:
        p = " ".join(re.sub(r"[\"“”]", "", phrase).split()).strip(" ,.")
        new = {w for w in re.findall(r"[a-z0-9/'-]+", p.lower()) if w not in COMMON} - words
        if p and new:                        # only phrases that teach a word not already in the prompt
            words.update(new)
            phrases.append(p)

    groups = (book.assemblies, book.fees, book.materials)
    for group in groups:                     # every item's own name first
        for entry in group.values():
            add(entry.name)
    for group in groups:                     # then the owner's other words for them, while there's room
        for entry in group.values():
            for alias in entry.aliases:
                add(alias)
    head = f"{book.business.get('name', 'Job notes')}. Quote: customer, address, ZIP. "
    body, room = "", max_chars - len(head) - 1
    for phrase in phrases:
        nxt = f"{body}, {phrase}" if body else phrase
        if len(nxt) > room:
            break
        body = nxt
    return head + body + "."


def make_pre_transcription_hook(get_book):
    """pre_transcription(file_path, provider, model, language, prompt, source) -> {"prompt": ...} or None.

    Appends Docket's vocabulary after any prompt the owner configured (stt.prompt), so theirs is kept."""
    def hook(prompt: str | None = None, source: str | None = None, **_: Any):
        try:
            book, errors, _path, _demo = get_book()
            if book is None or errors:
                return None
            ours = vocab_prompt(book)
            mine = (prompt or "").strip()
            return {"prompt": f"{mine} {ours}".strip() if mine else ours}
        except Exception:
            return None                      # never block a transcription
    return hook
