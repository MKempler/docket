from __future__ import annotations

import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket import pbedit as pe  # noqa: E402
from docket.owner import perform, pricebook_card, set_owner  # noqa: E402
from docket.pricebook import load_pricebook, parse_pricebook  # noqa: E402
from docket.store import Store  # noqa: E402

DEMO = Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml"
BOOK, _ = load_pricebook(DEMO)
TEXT = DEMO.read_text()


def _plan(words: str, **kw):
    return pe.plan(TEXT, BOOK, pe.read(words, book=BOOK), **kw)


def _book(p):
    book, errors = parse_pricebook(p.text)
    assert not errors
    return book


def test_settings_in_plain_words():
    p = _plan("Labor 150 an hour\nsales tax 7.5%\nservice call $99\nminimum 2 hours")
    b = _book(p)
    assert b.labor_rate == Decimal("150") and b.tax_rate == Decimal("0.075")
    assert b.service_call.amount == Decimal("99") and b.labor_minimum_hours == Decimal("2")
    assert len(p.changes) == 4


def test_updates_keep_the_owners_layout_and_comments():
    p = _plan('3/4" PEX pipe $2.20/ft')
    changed = [(a, b) for a, b in zip(TEXT.splitlines(), p.text.splitlines()) if a != b]
    assert len(changed) == 1 and len(TEXT.splitlines()) == len(p.text.splitlines())
    before, after = changed[0]
    assert before.startswith("  PEX34:    { name:") and after.startswith("  PEX34:    { name:")  # alignment kept
    assert "# price = cost x (1 + markup)" in p.text
    assert _book(p).materials["PEX34"].price == Decimal("2.20")


def test_markup_is_kept_when_it_reproduces_the_price():
    p = _plan("PEX34 2.10")
    m = _book(p).materials["PEX34"]
    assert m.price == Decimal("2.10") and m.markup == Decimal("1.00") and m.cost == Decimal("1.05")


def test_new_items_are_classified_and_priced_exactly():
    p = _plan('- 1" ball valve $24.50 each\n- Water softener install $1,250.00\n- Backflow test $95 flat')
    b = _book(p)
    assert b.materials["1-BALL-VALVE"].price == Decimal("24.50")
    assert b.fees["WATER-SOFTENER-INSTALL"].amount == Decimal("1250.00")
    assert b.fees["BACKFLOW-TEST"].amount == Decimal("95")
    assert [c.action for c in p.changes] == ["add", "add", "add"]


def test_owner_phrasing_is_not_part_of_the_name():
    p = _plan("add sump pumps to my price book at $289 each\nplease add a new backflow test $95 flat")
    assert [c.code for c in p.changes] == ["SUMP-PUMPS", "BACKFLOW-TEST"]
    b = _book(p)
    assert b.materials["SUMP-PUMPS"].name == "sump pumps" and b.fees["BACKFLOW-TEST"].name == "backflow test"


def test_priced_line_prefers_the_part_over_the_job():
    p = _plan("Kitchen faucet 210")
    assert p.changes[0].code == "FAUCETK" and _book(p).materials["FAUCETK"].price == Decimal("210")


def test_job_hours_and_job_price_refusal():
    p = _plan("TOILET-REPLACE 2 hours")
    assert _book(p).assemblies["TOILET-REPLACE"].labor_hours == Decimal("2")
    p = _plan("Toilet replacement $350 flat")
    assert not p.changes and "priced from its labor hours" in p.skipped[0]


def test_unclear_lines_are_reported_never_guessed():
    p = _plan("call mike about fittings\ntoilets are 350 flat and 3/4 pex is 2.20 a foot")
    assert not p.changes and len(p.skipped) == 2


def test_csv_with_cost_and_markup():
    p = _plan("code,name,cost,markup\nWH50G,50-gal gas water heater,800,35%\n,Sump pump,200,40%")
    b = _book(p)
    assert b.materials["WH50G"].price == Decimal("1080.00")
    assert b.materials["SUMP-PUMP"].price == Decimal("280.00")


def test_remove_and_alias():
    p = _plan("", removals=("CAMERA",), aliases=(("PTRAP", "p trap"),))
    b = _book(p)
    assert "CAMERA" not in b.fees and "p trap" in b.materials["PTRAP"].aliases
    with pytest.raises(pe.EditError, match="TOILET-REPLACE"):
        _plan("", removals=("WAXRING",))                      # still used by jobs
    with pytest.raises(pe.EditError, match="already means"):
        _plan("", aliases=(("PTRAP", "water heater"),))


def test_model_prices_must_appear_in_the_owners_words():
    class FakeLLM:
        def complete_structured(self, **kw):
            return SimpleNamespace(parsed={"items": [
                {"name": "toilet install", "price": "350", "unit": "flat", "kind": "flat_rate_job"},
                {"name": "3/4 pex", "price": "2.20", "unit": "ft", "kind": "material", "also_called": ["pex"]},
                {"name": "gas line", "price": "999", "unit": "ft", "kind": "material"}]})   # 999 was never said
    reading = pe.read("toilets are 350 flat and 3/4 pex is 2.20 a foot", FakeLLM(), book=BOOK)
    names = {i.name: i.price for i in reading.items}
    assert names == {"toilet install": Decimal("350"), "3/4 pex": Decimal("2.20")}
    p = pe.plan(TEXT, BOOK, reading)
    b = _book(p)
    assert b.materials["PEX34"].price == Decimal("2.20")        # matched the existing item
    assert b.fees["TOILET-INSTALL"].amount == Decimal("350")


def test_model_failure_falls_back_to_reporting():
    class Broken:
        def complete_structured(self, **kw):
            raise TimeoutError
    reading = pe.read("toilets are 350 flat and pex is 2.20 a foot", Broken(), book=BOOK)
    assert not reading.items and "model unavailable" in reading.skipped[0]


def _store():
    return Store(sqlite3.connect(":memory:", check_same_thread=False))


def test_save_writes_backup_and_undo_returns_to_demo(tmp_path):
    store, own = _store(), tmp_path / "pricebook.yaml"
    p = pe.propose(store, DEMO, "labor 150/hr")
    assert not own.exists()                                    # nothing written by a preview
    msg = pe.save(store, own, DEMO, p.token)
    assert "Saved 1 change" in msg and "Labor rate" in msg
    assert load_pricebook(own)[0].labor_rate == Decimal("150")
    p2 = pe.propose(store, own, "PEX34 2.10")
    pe.save(store, own, own, p2.token)
    assert "back to how it was" in pe.undo(store, own)
    assert load_pricebook(own)[0].materials["PEX34"].price == Decimal("1.90")
    assert "demo price book" in pe.undo(store, own) and not own.exists()
    assert pe.undo(store, own) == "Nothing to undo."


def test_stale_or_wrong_previews_never_save(tmp_path):
    store, own = _store(), tmp_path / "pricebook.yaml"
    own.write_text(TEXT)
    p = pe.propose(store, own, "labor 150/hr")
    with pytest.raises(pe.EditError, match="out of date"):
        pe.save(store, own, own, "0000000000")
    own.write_text(TEXT.replace('"135.00"', '"140.00"'))       # hand-edited after the preview
    with pytest.raises(pe.EditError, match="changed since"):
        pe.save(store, own, own, p.token)
    assert load_pricebook(own)[0].labor_rate == Decimal("140.00")
    with pytest.raises(pe.EditError, match="no price book change waiting"):
        pe.save(store, own, own)


def test_old_previews_expire(tmp_path, monkeypatch):
    store, own = _store(), tmp_path / "pricebook.yaml"
    p = pe.propose(store, DEMO, "labor 150/hr")
    assert pe.pending(store) is not None
    later = pe.time.time() + pe.PREVIEW_TTL + 1
    assert pe.pending(store, now=later) is None
    monkeypatch.setattr(pe.time, "time", lambda: later)
    with pytest.raises(pe.EditError, match="expire"):
        pe.save(store, own, DEMO, p.token)
    assert not own.exists()


def test_new_price_book_from_one_sentence(tmp_path):
    store, own = _store(), tmp_path / "pricebook.yaml"
    p = pe.propose(store, DEMO, "Rivera Plumbing, labor $120/hr, tax 7%, service call 79", fresh=True)
    pe.save(store, own, DEMO, p.token)
    b, errors = load_pricebook(own)
    assert not errors and b.business["name"] == "Rivera Plumbing" and b.labor_rate == Decimal("120")
    assert b.tax_rate == Decimal("0.07") and b.service_call.amount == Decimal("79") and not b.materials
    p = pe.propose(store, own, "Sump pump $289 each\nBackflow test $95 flat")   # adds into empty sections
    pe.save(store, own, own, p.token)
    b, errors = load_pricebook(own)
    assert not errors and b.materials["SUMP-PUMP"].price == Decimal("289") and "BACKFLOW-TEST" in b.fees
    with pytest.raises(pe.EditError, match="labor rate"):
        pe.propose(store, own, "Rivera Plumbing", fresh=True)


def test_buttons_save_only_their_own_preview(tmp_path):
    store, own = _store(), tmp_path / "pricebook.yaml"
    set_owner(store, "telegram", chat_id="1", user_id="42")
    p = pe.propose(store, DEMO, "labor 150/hr")
    card = pricebook_card(p)
    save_data, cancel_data = card.buttons[0][0].data, card.buttons[0][1].data
    assert len(save_data.encode()) <= 64                       # Telegram callback limit
    ops = (lambda t: pe.save(store, own, DEMO, t), lambda t: pe.cancel(store, t))
    kw = dict(platform="telegram", store=store, book=BOOK, outbox_root=tmp_path, base_url=lambda: "", pricebook=ops)
    assert not perform(save_data, user_id="7", **kw).ok         # not the owner
    pe.propose(store, DEMO, "labor 160/hr")                     # a newer preview replaces the old one
    assert not perform(save_data, user_id="42", **kw).ok and not own.exists()
    newer = pricebook_card(pe.pending(store))
    res = perform(newer.buttons[0][0].data, user_id="42", **kw)
    assert res.ok and "Saved" in res.card.text and load_pricebook(own)[0].labor_rate == Decimal("160")


def test_gate_and_hint_cover_price_book_saves():
    import docket
    from docket.hints import hint_for
    gate = docket._make_gate(SimpleNamespace(current_cron_execution=lambda: None))
    assert gate(tool_name="docket_pricebook_save", args={})["action"] == "approve"
    from docket import runtime
    store = _store()
    runtime.reset_for_tests(store)
    try:
        p = pe.propose(store, DEMO, "labor 150/hr\nSump pump $289 each")
        msg = gate(tool_name="docket_pricebook_save", args={"preview_id": p.token})["message"]
        assert "Save 2 changes" in msg and "$135.00/h → $150.00/h" in msg and "SUMP-PUMP" in msg
        assert "refused" in gate(tool_name="docket_pricebook_save", args={"preview_id": "nope"})["message"]
    finally:
        runtime.reset_for_tests(None)
    cron = docket._make_gate(SimpleNamespace(current_cron_execution=lambda: object()))
    assert cron(tool_name="docket_pricebook_save", args={})["action"] == "block"
    assert "docket_pricebook_edit" in hint_for("raise my labor rate to 145")
    assert "docket_pricebook_edit" in hint_for("add sump pump $289 to my price book")
    assert "docket_quote" in hint_for("quote Dana a water heater swap")
