"""pre_llm_call hint: when the owner's message is about quoting, tell the model exactly how to reach Docket.

Hermes defers plugin tools behind tool_search, and small models sometimes fail to find or call them. On a
quoting message only, Docket adds one line naming the exact bridge call. Other messages get nothing, so the
prompt stays clean and cache-friendly.
"""

from __future__ import annotations

import re

QUOTE_RE = re.compile(r"\b(quote|quoting|estimate|price|pricing|how much|cost)\b", re.I)
SEND_RE = re.compile(r"\b(send|resend|email)\b.*\bQ-?\d{3,6}\b|\bQ-?\d{3,6}\b.*\b(send|resend)\b", re.I)
JOBS_RE = re.compile(r"\b(pipeline|unsigned|follow[- ]?ups?|my jobs|open quotes|booked jobs)\b", re.I)


def hint_for(message: str, *, mentions_work: bool = False) -> str | None:
    text = (message or "").strip()
    if not text or text.startswith("/"):
        return None                     # slash commands never reach the model
    if SEND_RE.search(text):
        return ('[Docket] To send it, call tool_call with calls=[{"name": "docket_send_quote", '
                '"arguments": {"quote_id": "<the Q-number>"}}]. The owner will be asked to approve.')
    if QUOTE_RE.search(text) or mentions_work:
        return ('[Docket] To quote this job, call tool_call ONCE with calls=[{"name": "docket_quote", '
                '"arguments": {"description": "<the owner\'s message, word for word>"}}]. '
                'Docket prices it from the owner\'s price book; never state or estimate prices yourself.')
    if JOBS_RE.search(text):
        return ('[Docket] For the owner\'s quotes and jobs, call tool_call with calls=[{"name": "docket_jobs", '
                '"arguments": {}}] (or docket_followups / docket_pipeline).')
    return None
