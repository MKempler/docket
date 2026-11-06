"""pre_llm_call hint: when the owner's message is about quoting, tell the model exactly how to reach Docket.

Hermes defers plugin tools behind tool_search, and small models sometimes fail to find or call them. On a
quoting message only, Docket adds one line naming the exact bridge call. Other messages get nothing, so the
prompt stays clean and cache-friendly.
"""

from __future__ import annotations

import re

QUOTE_RE = re.compile(r"\b(quote|quoting|estimate|price|pricing|how much|cost)\b", re.I)
SEND_RE = re.compile(r"\b(send|resend|email)\b.*\bQ-?\d{3,6}\b|\bQ-?\d{3,6}\b.*\b(send|resend)\b", re.I)
PRICEBOOK_RE = re.compile(r"\b(price ?book|price list|my (?:labor|labour|hourly|shop) rate|(?:raise|lower|change|update|"
                          r"set|bump) (?:my |the |our )?(?:labor|labour|hourly|rate|prices?|tax|service call)|"
                          r"(?:add|put) .{1,60} (?:to|in|into) (?:my|the) (?:price ?book|prices))\b", re.I)
JOBS_RE = re.compile(r"\b(pipeline|unsigned|follow[- ]?ups?|my jobs|open quotes|booked jobs)\b", re.I)


def hint_for(message: str, *, mentions_work: bool = False) -> str | None:
    text = (message or "").strip()
    if not text or text.startswith("/"):
        return None                     # slash commands never reach the model
    if SEND_RE.search(text):
        return ('[Docket] To send it, call tool_call with calls=[{"name": "docket_send_quote", '
                '"arguments": {"quote_id": "<the Q-number>"}}]. The owner will be asked to approve.')
    if PRICEBOOK_RE.search(text):
        return ('[Docket] Call tool_call NOW, before replying, with calls=[{"name": "docket_pricebook_edit", '
                '"arguments": {"changes": "<the owner\'s words or pasted list, word for word>"}}]. It only shows a '
                'preview and saves nothing, so no other step comes first. Show the owner its preview; save only after '
                'they say yes.')
    if QUOTE_RE.search(text) or mentions_work:
        return ('[Docket] To quote this job, call tool_call ONCE with calls=[{"name": "docket_quote", '
                '"arguments": {"description": "<the owner\'s message, word for word>"}}]. '
                'Docket prices it from the owner\'s price book; never state or estimate prices yourself.')
    if JOBS_RE.search(text):
        return ('[Docket] For the owner\'s quotes and jobs, call tool_call with calls=[{"name": "docket_jobs", '
                '"arguments": {}}] (or docket_followups / docket_pipeline).')
    return None
