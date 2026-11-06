"""Pricing engine tests. Expected amounts are worked out by hand from demo_data/pricebook.yaml
(shown in comments) so the engine is checked against independent arithmetic, not itself."""

from __future__ import annotations

import itertools
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from docket.pricebook import load_pricebook, parse_pricebook  # noqa: E402
from docket.pricing import JobSpec, SpecError, SpecItem, price, render_text, spec_from_args  # noqa: E402

BOOK_PATH = ROOT / "demo_data" / "pricebook.yaml"


@pytest.fixture(scope="module")
def book():
    b, errors = load_pricebook(BOOK_PATH)
    assert errors == [], [str(e) for e in errors]
    return b


def amounts(q):
    return {l.code: l.amount for l in q.lines}


def assert_ties_out(q):
    assert q.subtotal == sum((l.amount for l in q.lines), D(0))
    assert q.total == q.subtotal + q.tax
    for l in q.lines:
        assert l.amount == l.amount.quantize(D("0.01"))


def test_water_heater_replacement_zone_b(book):
    q = price(book, spec_from_args({
        "items": [{"code": "WH50-REPLACE", "qty": 1}, {"code": "PERMIT-WH", "qty": 1}, {"code": "HAULAWAY", "qty": 1}],
        "zip": "62707"}))
    a = amounts(q)
    assert a["SERVICE-CALL"] == D("89.00")
    assert a["ZONE-B"] == D("35.00")
    # labor 3.0 h x 135.00 = 405.00; WH50G 780 x 1.35 = 1053.00; WHKIT 65 x 1.5 = 97.50; EXPTANK 48 x 1.5 = 72.00
    assert a["WH50-REPLACE"] == D("1627.50")
    assert a["PERMIT-WH"] == D("85.00") and a["HAULAWAY"] == D("35.00")
    assert q.subtotal == D("1871.50")
    assert q.taxable == D("1222.50")
    assert q.tax == D("100.86")          # 1222.50 x 0.0825 = 100.85625
    assert q.total == D("1972.36")
    assert q.complete
    assert_ties_out(q)


def test_repipe_weekend_member_rounds_fittings_up(book):
    q = price(book, spec_from_args({"items": [{"code": "REPIPE-PEX12", "qty": 38}], "zip": "62701",
                                    "flags": ["weekend"], "customer_tier": "member"}))
    a = amounts(q)
    assert "ZONE-A" not in a              # zone A is free, so no travel line
    line = next(l for l in q.lines if l.code == "REPIPE-PEX12")
    assert line.labor_hours == D("3.04")  # 0.08 x 38
    assert line.labor == D("513.00")      # 3.04 x 135 x 1.25
    fit = next(c for c in line.components if c["code"] == "PEXFIT")
    assert fit["qty"] == "10"             # 0.25 x 38 = 9.5 -> 10
    assert line.materials == D("95.60")   # 38 x 1.20 + 10 x 5.00
    assert a["DISC-MEMBER"] == D("-51.30")
    assert q.subtotal == D("646.30")
    assert q.tax == D("7.89")             # 95.60 x 0.0825 = 7.887
    assert q.total == D("654.19")
    assert_ties_out(q)


def test_highest_multiplier_wins_and_minimum_labor_top_up(book):
    q = price(book, spec_from_args({"items": [{"code": "SHUTOFF-REPLACE", "qty": 1}], "flags": ["weekend", "emergency"]}))
    assert q.multiplier_flag == "emergency" and q.multiplier == D("2.0")
    a = amounts(q)
    assert a["SHUTOFF-REPLACE"] == D("156.00")   # 0.5 h x 135 x 2 = 135.00 + valve 21.00
    assert a["LABOR-MIN"] == D("135.00")         # 0.5 h top-up x 135 x 2
    assert q.subtotal == D("380.00")
    assert q.tax == D("1.73")                    # 21.00 x 0.0825 = 1.7325
    assert q.total == D("381.73")
    assert_ties_out(q)


def test_unknown_items_become_needs_price_and_block_sending(book):
    q = price(book, spec_from_args({"items": [{"code": "TANKLESS-INSTALL", "qty": 1}], "zip": "99999",
                                    "unpriced": ["Patch drywall behind heater"]}))
    assert not q.complete
    assert len(q.needs_price) == 3
    kinds = [l.kind for l in q.lines]
    assert kinds.count("needs_price") == 3
    assert all(l.amount == 0 for l in q.lines if l.kind == "needs_price")
    assert "Cannot send yet" in render_text(q)


def test_whole_units_reject_fractional_quantities(book):
    q = price(book, spec_from_args({"items": [{"code": "TOILET-REPLACE", "qty": "1.5"}]}))
    assert not q.complete
    assert "whole" in q.needs_price[0]


def test_unknown_flag_and_tier_warn_without_changing_price(book):
    base = price(book, spec_from_args({"items": [{"code": "DRAIN-CLEAR", "qty": 1}]}))
    odd = price(book, spec_from_args({"items": [{"code": "DRAIN-CLEAR", "qty": 1}], "flags": ["holiday"],
                                      "customer_tier": "vip"}))
    assert odd.total == base.total
    assert len(odd.warnings) == 2


def test_bad_quantities_are_rejected(book):
    for bad in (0, -2, "two", None, "nan"):
        with pytest.raises(SpecError):
            spec_from_args({"items": [{"code": "DRAIN-CLEAR", "qty": bad}]})


def test_every_assembly_ties_out_under_every_flag_combo(book):
    flag_sets = [()] + [c for n in (1, 2, 3) for c in itertools.combinations(("after_hours", "weekend", "emergency"), n)]
    for code, asm in book.assemblies.items():
        qtys = [D(1), D(3)] if asm.unit == "each" else [D("1"), D("7.5"), D(38), D(120)]
        for qty, flags, tier in itertools.product(qtys, flag_sets, ("", "member")):
            q = price(book, JobSpec([SpecItem(code, qty)], flags=flags, customer_tier=tier))
            assert q.complete, (code, qty, flags)
            assert_ties_out(q)
            assert q.total > 0


def test_validator_reports_paths_and_lines():
    text = BOOK_PATH.read_text()
    broken = (text.replace('rate: "0.0825"', 'rate: "8.25"')
                  .replace('cost: "0.95"', 'cost: "0,95"')
                  .replace("{ code: WHKIT, qty: \"1\" }", "{ code: WHKIT2, qty: \"1\" }"))
    book, errors = parse_pricebook(broken)
    assert book is None
    messages = [str(e) for e in errors]
    assert any("tax.rate" in m and "fraction" in m for m in messages), messages
    assert any("materials.PEX34.cost" in m for m in messages), messages
    assert any("WHKIT2 is not in materials" in m for m in messages), messages
    assert all(e.line for e in errors), messages


def test_demo_price_book_loads_clean(book):
    assert book.currency == "USD"
    assert len(book.assemblies) == 8 and len(book.materials) == 13 and len(book.fees) == 3
