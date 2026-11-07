from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docket.intake import parse_words  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.spoken import normalize  # noqa: E402

BOOK, _ = load_pricebook(Path(__file__).resolve().parents[1] / "demo_data" / "pricebook.yaml")


@pytest.mark.parametrize("said, written", [
    ("six two seven oh three", "62703"),
    ("zip is 62 707", "zip is 62707"),
    ("Irene is 627-01", "Irene is 62701"),
    ("Monoray S62711, new faucet", "Monoray S 62711, new faucet"),
    ("forty two Linden Avenue", "42 Linden Avenue"),
    ("Fifty gallon gas water heater", "50 gallon gas water heater"),
    ("one hundred twenty feet", "120 feet"),
    ("an extra hour and a half of labor", "1.5 extra hours of labor"),
    ("one and a half hours of labor", "1.5 hours of labor"),
    ("two and a half hours", "2.5 hours"),
    ("half an hour", "0.5 hours"),
    ("an hour of labor", "1 hour of labor"),
])
def test_spoken_numbers(said, written):
    assert normalize(said) == written


@pytest.mark.parametrize("text", [
    "put a new one in, haul the old one", "Replace toilet x2", "cost $1,250.00 and 45 feet", "14 Maple St 62707",
    "Q-1007 v2", "WH50G and PEX34", "2 70 degrees", "call (555) 010-4477", "after hours tonight",
])
def test_everything_else_is_left_alone(text):
    assert normalize(text) == text


def test_a_spoken_job_parses_like_a_typed_one():
    said = parse_words(BOOK, "Dana Henderson, fourteen Maple Street, six two seven oh seven, fifty gallon water heater, permit")
    typed = parse_words(BOOK, "Dana Henderson, 14 Maple Street 62707, 50 gallon water heater, permit")
    assert said.args == typed.args and said.args["zip"] == "62707"
