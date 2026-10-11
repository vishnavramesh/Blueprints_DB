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
    ("-5", 0), ("- 3", 0), ("0.5", 0), ("2.5", 2),   # negative -> 0 (warned); decimals truncate
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
    ("3/15/99", "2099-03-15"), ("1/1/69", "2069-01-01"),   # two-digit years are always 20xx
    ("11/2026", "2026-11-30"), ("2027-03", "2027-03-31"), ("Mar 2027", "2027-03-31"),
    ("March 2027", "2027-03-31"), ("02/2028", "2028-02-29"),  # month only -> last day
    ("2028", "2028-12-31"), ("2/29/2024", "2024-02-29"), ("2/29/2023", None),
    ("13/1/2024", "2024-01-13"), ("15/03/2026", "2026-03-15"),  # day/month only when month/day is impossible
    ("03/04/2026", "2026-03-04"), ("2027-13", None), ("Foo 2027", None), ("soon", None), ("", None),
    ("02/30/2026", None), ("2027-02-29", None), ("0001", None), ("13/2027", None),
    ("Marchxyz 2027", None), ("Mayo 2027", None), ("Sept 2027", "2027-09-30"), ("Sep. 2027", "2027-09-30"),
    ("2027.03", "2027-03-31"), ("03-2027", "2027-03-31"), ("Feb-2027", "2027-02-28"),
    ("EXP 2027-03", "2027-03-31"), ("Exp: 03/2027", "2027-03-31"), ("2027/03/15", "2027-03-15"),
    ("1900-01-01", None),
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


@pytest.mark.parametrize("raw,expected", [
    ("$1,234.50", 1234.5), ("€5,50", 5.5), ("1.234,50", 1234.5), ("12", 12.0), ("1,200", 1200.0),
    ("", None), ("free", None), ("$123456789", None), ("99999999.99", 99999999.99),
])
def test_money(raw, expected):
    assert N.parse_money(raw) == expected


def test_nul_and_bounds():
    assert N.clean("Gauze\x00pad ") == "Gauzepad"
    assert N.sanitize_row({"Na\x00me": "x\x00", "Q": None}) == {"Name": "x", "Q": ""}
    assert N.bounded_int("12345678901") is None and N.bounded_int("12") == 12
    f = N.read_inventory_row({"Name": "a", "Box Number": "12345678901", "Pallet Number": "99999999999",
                              "Days in Warehouse": "800000", "Price per unit": "$123456789",
                              "Quantity": "-5"}, today=date(2026, 10, 9))
    assert (f["box_number"], f["pallet_number"], f["warehouse_date"], f["unit_value"], f["quantity"]) == \
        (None, None, None, None, 0)
    assert len(f["problems"]) == 5
    assert N.read_inventory_row({"Name": "a", "Box Number": "ER 99999999999"})["box_number"] is None


def test_unreadable_date_is_a_problem_and_flags_the_row():
    assert N.date_problem("") is None and N.date_problem("N/A") is None and N.date_problem("2027-03") is None
    assert "unreadable" in N.date_problem("02/30/2026")
    f = N.read_inventory_row({"Name": "Gauze", "Review": "No", "Date of Expiration": "Marchxyz 2027"})
    assert f["expiration_date"] is None and f["flagged"] is True
    assert any("unreadable Date of Expiration" in p for p in f["problems"])
    assert N.read_inventory_row({"Name": "Gauze", "Review": "No", "Date of Expiration": "Feb-2027"})["flagged"] is False


def test_lone_surrogates_are_replaced():
    row = N.sanitize_row({"Name": "Gauze \ud83d", "Notes": "ok \ud83d\ude00"})
    assert row["Name"] == "Gauze \ufffd"
    assert N.clean("x\udfff") == "x\ufffd"
    N.row_hash({"Name": "Gauze \ud83d"}, 2)  # hashing never raises
    N.content_hash({"Name": "\ud800"})
    assert N.source_key({"Name": "\ud800"}).startswith("hash:")


def test_expiration_sanity_flags():
    from datetime import date
    today = date(2026, 10, 11)
    ok = N.read_inventory_row({"Name": "Gloves", "Date of Expiration": "2028-01-01", "Days in Warehouse": "30"}, today=today)
    assert not ok["flagged"]
    early = N.read_inventory_row({"Name": "Gloves", "Date of Expiration": "2026-01-01", "Days in Warehouse": "30"}, today=today)
    assert "expires before it reached the warehouse" in early["review_reason"]
    far = N.read_inventory_row({"Name": "Gloves", "Date of Expiration": "2040-01-01", "Days in Warehouse": "30"}, today=today)
    assert "more than 10 years" in far["review_reason"]
    no_wh = N.read_inventory_row({"Name": "Gloves", "Date of Expiration": "2026-01-01"}, today=today)
    assert not no_wh["flagged"]
