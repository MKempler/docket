from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket.hints import hint_for  # noqa: E402


def test_quoting_messages_get_the_exact_call():
    h = hint_for("Quote Dana Henderson, 14 Maple St 62707, water heater")
    assert '"name": "docket_quote"' in h and "word for word" in h
    assert hint_for("Dana needs her toilet reset", mentions_work=True)


def test_send_messages_point_at_the_gated_send_tool():
    assert '"docket_send_quote"' in hint_for("Send Q-1010 to the customer now.")


def test_unrelated_messages_and_slash_commands_get_nothing():
    assert hint_for("What's the weather tomorrow?") is None
    assert hint_for("/quote Dana, water heater") is None
    assert hint_for("") is None
