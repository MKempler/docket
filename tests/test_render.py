from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import service as svc  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.render import render_card_png, render_pdf  # noqa: E402
from docket.store import Store  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")
OUT = Path(__file__).resolve().parents[1] / ".render-preview"


@pytest.fixture
def drafted():
    store = Store(sqlite3.connect(":memory:", check_same_thread=False))
    d = svc.draft_from_words(store, BOOK, "Dana Henderson, 14 Maple St 62707, swap the 50 gallon gas water heater, "
                                          "permit and haul away, saturday")
    return store, d.record


def test_pdf_has_the_engine_numbers(drafted):
    store, rec = drafted
    pdf = render_pdf(BOOK, rec, svc.accept_url(store, rec))
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000
    from pypdf import PdfReader
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    for needle in ("Copperline Plumbing & Drain", rec.number, "Dana Henderson", f"${float(rec.total):,.2f}",
                   "Accept this quote"):
        assert needle in text, needle
    OUT.mkdir(exist_ok=True)
    (OUT / "quote.pdf").write_bytes(pdf)


def test_card_png_renders(drafted):
    from PIL import Image
    _, rec = drafted
    png = render_card_png(BOOK, rec)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.width == 1080 and 600 < img.height < 4000
    OUT.mkdir(exist_ok=True)
    (OUT / "card.png").write_bytes(png)


def test_send_attaches_pdf(drafted, tmp_path):
    store, rec = drafted
    result = svc.send_quote(store, BOOK, rec.id, outbox_root=tmp_path, actor="owner", render_pdf=render_pdf)
    assert (Path(result["outbox"]) / f"{rec.number}-v1.pdf").exists()
