"""Normalizer rules, pinned against real values seen in the sheet."""
import json
from datetime import date
from pathlib import Path

import pytest

from b4p import normalize as N

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "normalize_golden.json").read_text())

# Old rules had extra ids 13 'Medical Supplies' and 99 'Unspecified'; the new
# schema folds both into 'Misc Non-Medical', exactly like the internal dashboard.
OLD_CATEGORY_IDS = {1: "IV Supplies", 2: "Misc Surgical", 3: "Wound Care & Bandages",
                    4: "Needles & Syringes", 5: "Catheters & Briefs", 6: "Airway & Oxygen",
                    7: "Vitals & Home Health", 8: "Gloves", 9: "Apparel (Gowns, Shoe Covers, etc.)",
                    10: "Masks & Face Shields", 11: "Sutures", 12: "Misc Non-Medical",
                    13: "Misc Non-Medical", 99: "Misc Non-Medical"}


@pytest.mark.parametrize("raw,old_id", sorted(GOLDEN["norm_category"].items()))
def test_category_golden(raw, old_id):
    assert N.norm_category(raw) == OLD_CATEGORY_IDS[old_id]


@pytest.mark.parametrize("raw,expected", [
    ("Wound Care & Bandages", "Wound Care & Bandages"),
    ("  gloves ", "Gloves"),
    ("Misc Non‐Medical", "Misc Non-Medical"),   # U+2010 hyphen
    ("Misc Non‑Medical", "Misc Non-Medical"),   # non-breaking hyphen
    ("Misc Non–Medical", "Misc Non-Medical"),   # en dash
    ("Misc Nonâ\u0080\u0090Medical", "Misc Non-Medical"),  # mojibake seen by the dashboard
    ("ST/N", "Misc Non-Medical"),
    ("", "Misc Non-Medical"),
    (" ", "Misc Non-Medical"),
])
def test_category_edge_cases(raw, expected):
    assert N.norm_category(raw) == expected


@pytest.mark.parametrize("raw,expected", sorted(GOLDEN["norm_mfr"].items()))
def test_manufacturer_golden(raw, expected):
    assert N.norm_manufacturer(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("becton dickinson", "BD"), ("Medline Industries, LP", "Medline"), ("Unspecified", None),
    ("unknown", None), ("N/A", None), ("  ", None), (" Smiths Medical ", "Smiths Medical"),
])
def test_manufacturer_edge_cases(raw, expected):
    assert N.norm_manufacturer(raw) == expected


@pytest.mark.parametrize("raw,expected", sorted(GOLDEN["parse_qty"].items()))
def test_qty_golden(raw, expected):
    assert N.parse_qty(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1 (full box)", 1), ("271 (0.68 lbs)", 271), ("-", 0), ("", 0), (None, 0), ("4x5", 4),
    ("99999999999", 999_999), ("1,200", 1200),
])
def test_qty_edge_cases(raw, expected):
    assert N.parse_qty(raw) == expected


@pytest.mark.parametrize("raw,expected", sorted(GOLDEN["parse_pallet"].items()))
def test_pallet_golden(raw, expected):
    assert N.parse_pallet(raw) == expected


@pytest.mark.parametrize("raw,expected", sorted(GOLDEN["parse_date"].items()))
def test_date_golden(raw, expected):
    d = N.parse_date(raw)
    assert (d.isoformat() if d else None) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2027-03-15", "2027-03-15"), ("3/15/2027", "2027-03-15"), ("3/15/27", "2027-03-15"),
    ("3/15/99", "1999-03-15"), ("3/15/68", "2068-03-15"), ("11/2026", "2026-11-01"),
    ("2028", "2028-01-01"), ("2/29/2024", "2024-02-29"), ("2/29/2023", None),
    ("13/1/2024", None), ("soon", None), ("", None),
])
def test_date_edge_cases(raw, expected):
    d = N.parse_date(raw)
    assert (d.isoformat() if d else None) == expected


@pytest.mark.parametrize("raw,expected", sorted(GOLDEN["parse_box"].items()))
def test_box_label_golden(raw, expected):
    got = N.parse_box_label(raw)
    assert (list(got) if got else None) == expected


def test_box_label_edge_cases():
    assert N.parse_box_label("0") is None
    assert N.parse_box_label("99999999999") is None
    assert N.parse_box_label("tcrc 2") == ("TC/RC", 2)
    assert N.norm_box_type(" stn ") == "ST/N"
    assert N.norm_box_type("Tubing & Catheters") == "Tubing & Catheters"
    assert N.norm_box_type("") is None


@pytest.mark.parametrize("raw,expected", [
    ("Under Review", "Under Review"), ("under review", "Under Review"), ("Pending", "Under Review"),
    ("APPROVED", "Approved"), ("Shipped", "Shipped"), ("delivered", "Delivered"),
    ("Canceled", "Cancelled"), ("Denied", "Denied"), ("lost", None), ("", None),
])
def test_status(raw, expected):
    assert N.norm_status(raw) == expected


def test_timestamp():
    ts = N.parse_timestamp("2026-05-23T05:23:06.638914+00:00")
    assert ts.isoformat() == "2026-05-23T05:23:06.638914+00:00"
    assert N.parse_timestamp("not a date") is None


def test_read_row_current_layout(sheet_rows):
    """Live layout: Category = box label, Subcategory = approved category."""
    _, row = sheet_rows[0]
    f = N.read_inventory_row(row, today=date(2026, 10, 9))
    assert f["category"] == "Wound Care & Bandages"
    assert (f["box_type"], f["box_number"], f["pallet_number"]) == ("ST/N", 4, 1)
    assert f["manufacturer"] == "Cardinal Health"
    assert f["quantity"] == 16
    assert f["expiration_date"] == date(2030, 11, 7)
    assert f["flagged"] is False
    assert f["warehouse_date"] is None


def test_read_row_old_layout():
    row = {"Manufacturer Name": "Becton, Dickinson and Company", "Category": "Misc Non‐Medical",
           "Name": "{syringe}", "Quantity": "2 full kits", "Box Number": "ER #1",
           "Pallet Number": "Wound #1", "Review": "No review needed", "Days in Warehouse": "10"}
    f = N.read_inventory_row(row, today=date(2026, 10, 9))
    assert f["category"] == "Misc Non-Medical"
    assert f["manufacturer"] == "BD"
    assert (f["box_type"], f["box_number"], f["pallet_number"]) == ("ER", 1, 1)
    assert f["quantity"] == 2
    assert f["flagged"] is True  # '{...}' = unreadable scan
    assert f["warehouse_date"] == date(2026, 9, 29)


def test_review_flag_rules():
    assert not N.needs_review("Gauze", "No")
    assert not N.needs_review("Gauze", "No review needed")
    assert N.needs_review("Gauze", "Yes")
    assert N.needs_review("", "No")
    assert N.needs_review("Unknown", "No")


def test_source_key_and_hash(sheet_rows):
    keys = [N.source_key(r) for _, r in sheet_rows]
    assert len(set(keys)) == len(keys)  # every real row has its own identity
    _, row = sheet_rows[0]
    assert N.row_hash(row, 2) == N.row_hash(dict(row), 2)
    assert N.row_hash(row, 2) != N.row_hash(row, 3)
    no_url = {**row, "Image URL": ""}
    assert N.source_key(no_url).startswith("hash:")
