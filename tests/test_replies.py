"""Replies never contain a bare local file path: Hermes's gateway attaches any such file to the chat and cuts the
path out of the text (gateway/platforms/base.py extract_local_files). Paths go in inline code instead."""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import commands, runtime  # noqa: E402
from docket.store import Store  # noqa: E402

# Hermes's pattern, minus its existence check: absolute or ~/ paths with a deliverable-looking extension.
BARE_PATH = re.compile(r"(?<![/:\w.`])(?:~/|/)(?:[\w.\-]+/)*[\w.\-]+\.(?:yaml|yml|pdf|eml|txt|csv|png|json)\b")


def _bare_paths(text: str) -> list[str]:
    outside_code = re.sub(r"`[^`]*`", "", text)
    return BARE_PATH.findall(outside_code)


def test_pricebook_replies_quote_their_paths():
    for text in (commands.pricebook_text(), commands.pricebook_text(validate=True)):
        assert "`" in text and not _bare_paths(text), text


def test_invoice_and_send_replies_quote_their_paths():
    inv = {"number": "INV-1001", "quote_number": "Q-1004", "customer": {"name": "Pat Jones"}, "total": "464.00",
           "due": "2026-11-20", "handoff": "local", "pdf": "/tmp/plugin-data/docket/outbox/INV-1001.pdf"}
    assert not _bare_paths(commands._invoice_text(inv))
    sent = {"number": "Q-1007", "version": 1, "accept_url": "http://127.0.0.1:8787/q/x",
            "outbox": "/tmp/plugin-data/docket/outbox/Q-1007-v1/quote.eml"}
    assert not _bare_paths(commands._send_text(sent))


def test_cli_help_only_names_real_cli_commands(capsys):
    from types import SimpleNamespace
    commands.cli_main(SimpleNamespace(docket_command=None))
    out = capsys.readouterr().out
    cli_lines = [ln.split()[2] for ln in out.splitlines() if ln.strip().startswith("hermes docket ")]
    assert cli_lines and set(cli_lines) <= {"demo", "serve"}
