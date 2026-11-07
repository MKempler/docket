from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket.pricebook import load_pricebook, parse_pricebook  # noqa: E402
from docket.voice import make_pre_transcription_hook, vocab_prompt  # noqa: E402

DEMO = Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml"
BOOK, _ = load_pricebook(DEMO)


def test_prompt_names_every_item_and_fits_whisper():
    p = vocab_prompt(BOOK)
    assert len(p) <= 700 and p.startswith("Copperline Plumbing & Drain.")
    import re
    from docket.voice import COMMON
    low = p.lower()
    for entry in (*BOOK.assemblies.values(), *BOOK.fees.values(), *BOOK.materials.values()):
        for word in re.findall(r"[a-z0-9/'-]+", entry.name.lower().replace('"', "")):
            assert word in COMMON or word in low, (entry.name, word)
    assert "angle stop" in p and '"' not in p


def test_prompt_follows_the_owners_book():
    text = DEMO.read_text().replace('aliases: ["camera", "camera inspection", "scope the line"]',
                                    'aliases: ["camera", "hydro jetting"]')
    book, errors = parse_pricebook(text)
    assert not errors and "hydro jetting" in vocab_prompt(book, max_chars=2000)


def test_hook_keeps_the_owners_own_prompt_and_never_raises():
    hook = make_pre_transcription_hook(lambda: (BOOK, [], DEMO, True))
    out = hook(file_path="x.ogg", provider="local", model="base", language="en", prompt="Hermes, Nous", source="gateway")
    assert out["prompt"].startswith("Hermes, Nous Copperline")
    assert hook(prompt=None)["prompt"].startswith("Copperline")
    assert make_pre_transcription_hook(lambda: (None, ["bad"], DEMO, False))(prompt="x") is None
    assert make_pre_transcription_hook(lambda: 1 / 0)(prompt="x") is None
