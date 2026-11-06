from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket.intake import intake, parse_words  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.pricing import price, spec_from_args  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")


def codes(result):
    return {i["code"]: i["qty"] for i in result.args["items"]}


def test_water_heater_sentence():
    r = parse_words(BOOK, "Dana Henderson, 14 Maple St 62707, swap the 50 gallon gas water heater, permit and haul away")
    assert r.customer_name == "Dana Henderson"
    assert r.site == "14 Maple St"
    assert r.args["zip"] == "62707"
    assert codes(r) == {"WH50-REPLACE": "1", "PERMIT-WH": "1", "HAULAWAY": "1"}   # "50 gallon" is a size, not a qty
    assert r.args["unpriced"] == []
    q = price(BOOK, spec_from_args(r.args))
    assert str(q.total) == "1972.36"     # same job as test_pricing's hand-computed case


def test_lengths_flags_member():
    r = parse_words(BOOK, "Ray Ortiz, 62701, repipe 38 ft of the bathroom, saturday, he's a member")
    assert codes(r) == {"REPIPE-PEX12": "38"}
    assert r.args["flags"] == ["weekend"] and r.args["customer_tier"] == "member"


def test_counts_and_codes():
    r = parse_words(BOOK, "Kim Lee, 2 x TOILET-RESET, two shutoff valves, emergency")
    assert codes(r) == {"TOILET-RESET": "2", "SHUTOFF-REPLACE": "2"}
    assert r.args["flags"] == ["emergency"]


def test_unknown_work_becomes_unpriced_not_guessed():
    r = parse_words(BOOK, "Pat Jones, install a tankless heater in the attic, clogged drain")
    assert codes(r) == {"DRAIN-CLEAR": "1"}
    assert r.args["unpriced"] == ["install a tankless heater in the attic"]
    q = price(BOOK, spec_from_args(r.args))
    assert not q.complete


class FakeLLM:
    def __init__(self, parsed=None, exc=None):
        self.parsed, self.exc, self.calls = parsed, exc, []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(parsed=self.parsed)


def test_model_codes_are_checked_and_prices_cannot_leak_in():
    llm = FakeLLM({"customer_name": "Dana", "zip": "62707",
                   "items": [{"code": "wh50-replace", "qty": 1}, {"code": "TANKLESS", "qty": 1, "note": "tankless unit"},
                             {"code": "TOILET-RESET", "qty": 1.5}],
                   "flags": ["weekend", "holiday"], "member": True})
    r = intake(BOOK, "whatever", llm)
    assert r.source == "model"
    assert codes(r) == {"WH50-REPLACE": "1", "TOILET-RESET": "2"}
    assert r.args["unpriced"] == ["tankless unit"]
    assert r.args["flags"] == ["weekend"] and r.args["customer_tier"] == "member"
    schema = llm.calls[0]["json_schema"]
    fields = set(schema["properties"]) | set(schema["properties"]["items"]["items"]["properties"])
    assert not fields & {"price", "amount", "total", "cost", "unit_price", "subtotal", "tax"}   # no money fields
    assert "DRAIN-CLEAR" in llm.calls[0]["input"][0]["text"]          # catalog is in the prompt


@pytest.mark.parametrize("llm", [FakeLLM(exc=TimeoutError("slow")), FakeLLM(parsed="not json")])
def test_model_failure_falls_back_to_parser(llm):
    r = intake(BOOK, "Dana Henderson, 62707, water heater, permit", llm)
    assert r.source == "parser"
    assert codes(r) == {"WH50-REPLACE": "1", "PERMIT-WH": "1"}
    assert "built-in parser" in r.notes[0]


CHATTY = ("Hey so Dana Henderson over at 14 Maple St, 62707, her 50 gallon gas water heater is leaking, needs a swap. "
          "She'll need the permit and wants the old one hauled off. She can only do Saturday.")


def test_chatty_sentence_from_the_live_run():
    r = parse_words(BOOK, CHATTY)
    assert r.customer_name == "Dana Henderson"
    assert r.site == "14 Maple St" and r.args["zip"] == "62707"
    assert codes(r) == {"WH50-REPLACE": "1", "PERMIT-WH": "1", "HAULAWAY": "1"}
    assert r.args["flags"] == ["weekend"]
    assert r.args["unpriced"] == []                     # filler like "needs a swap" is not a line item
    assert any("needs a swap" in n for n in r.notes)    # but the owner can see what was not used
    q = price(BOOK, spec_from_args(r.args))
    assert str(q.total) == "2073.61"                    # Saturday labor; matches the rendered card


def test_chatty_variants():
    r = parse_words(BOOK, "Quote for Mike O'Brien at 220 Oak Ave 62712 - kitchen faucet and a new disposal, after hours")
    assert r.customer_name == "Mike O'Brien" and r.site == "220 Oak Ave"
    assert codes(r) == {"FAUCET-KITCHEN": "1", "DISPOSAL-INSTALL": "1"}
    assert r.args["flags"] == ["after_hours"]
    r = parse_words(BOOK, "toilet's rocking at the Garcias, 62701, pull and reset it")
    assert codes(r) == {"TOILET-RESET": "1"} and r.args["unpriced"] == []
