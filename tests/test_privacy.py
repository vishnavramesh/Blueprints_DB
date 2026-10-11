"""Patient identifiers never reach a dashboard view. All identifiers here are FAKE."""
import copy

import pytest

from b4p import normalize as N
from b4p.sync import run_sync

# (text, redacted text) -- every number below is made up
CASES = [
    ("SM SERUM MARKER PRE Pt. ID " "0000000000-0 XX00", "SM SERUM MARKER PRE Pt. ID [redacted]"),
    ("SPEP FROZEN Pt. ID " "1111111111-1 QQ11", "SPEP FROZEN Pt. ID [redacted]"),
    ("BM BLOOD MRD PROCESSING Pt. ID " "2222222222-2 CFDB", "BM BLOOD MRD PROCESSING Pt. ID [redacted] CFDB"),
    ("URINE FROZEN Pt. ZZ 3333333333 4444", "URINE FROZEN Pt. ID [redacted]"),
    ("labeled with Patient AB 5555 6666 and a barcode", "labeled with Pt. ID [redacted] and a barcode"),
    ("It also shows the patient ID: 7777777777-7.", "It also shows the Pt. ID [redacted]."),
    ("EOT, Pt. ID, " "8888888888-8 YY88", "EOT, Pt. ID [redacted]"),
    ("pt # 12345", "Pt. ID [redacted]"),
    ("BMRK SERUM 1 PRE Q00-000 E Z0Z0", "BMRK SERUM 1 PRE [id redacted] E Z0Z0"),
    ("MRN: " "00012345 on label", "MRN [redacted] on label"),
    ("Med Rec No. A-99999", "MRN [redacted]"),
    ("DOB 01/02/1960 written", "DOB [redacted] written"),
    ("call 555-010-0199 or (555) 010-0199", "call [phone redacted] or [phone redacted]"),
    ("contact nurse.jane@example.org", "contact [email redacted]"),
]

# must stay exactly as they are
KEEP = [
    "Patient Return Electrode", "PATIENT SLIPPERS", "single patient use only", "Patient preoperative skin preparation",
    "It includes a patient ID and a code", "with a patient ID number.", "a space for Pt. ID.", "99 HOUR URINE FROZEN Pt. ID",
    "PleurX Patient Starter Kit 1000 ml (Cat. 50-7500 Part 1 of 2)", "PT-CARE 3", "BD 10ml Syringe 21G x 1",
    "ports labeled 'Tandem', 'Patient', and 'Vacuum'", "Lot 2024-05-01", "REF 305-555", "12 pt font", "1 pt bottle", "pt bottle", "2 pt. bottle",
]


@pytest.mark.parametrize("text,expected", CASES)
def test_redact_phi(conn, text, expected):
    assert conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0] == expected


@pytest.mark.parametrize("text", KEEP)
def test_redact_phi_leaves_ordinary_text_alone(conn, text):
    assert conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0] == text


def test_redact_phi_is_immutable_and_null_safe(conn):
    vol = conn.execute("SELECT provolatile FROM pg_proc WHERE proname = 'redact_phi'").fetchone()[0]
    assert vol == "i"
    assert conn.execute("SELECT redact_phi(NULL)").fetchone()[0] is None


@pytest.mark.parametrize("text", [c[0] for c in CASES[:9]] + KEEP)
def test_python_and_sql_agree_on_patient_ids(conn, text):
    sql = conn.execute("SELECT %s ~ b4p_patient_id_pattern() OR %s ~ b4p_subject_id_pattern() "
                       "OR %s ~ b4p_patient_name_pattern() OR %s ~ b4p_patient_surname_pattern() "
                       "OR %s ~ b4p_dob_pattern()", (text,) * 5).fetchone()[0]
    assert N.has_patient_id(text) == sql


def test_python_and_sql_agree_on_the_real_fixture(conn, sheet_rows):
    for _, r in sheet_rows:
        for col in ("Name", "Notes", "General"):
            t = r.get(col, "")
            sql = conn.execute("SELECT %s ~ b4p_patient_id_pattern() OR %s ~ b4p_subject_id_pattern() "
                               "OR %s ~ b4p_patient_name_pattern() OR %s ~ b4p_patient_surname_pattern() "
                               "OR %s ~ b4p_dob_pattern()", (t,) * 5).fetchone()[0]
            assert N.has_patient_id(t) == sql, col


FAKE_NAME = "SM SERUM MARKER PRE Pt. ID " "0000000000-0 XX00"
FAKE_NOTE = "Tube label shows Pt. ID " "9999999999-9 ZZ99 and a barcode; call 555-010-0199."


@pytest.fixture
def phi_loaded(conn, sheet_rows):
    rows = copy.deepcopy(sheet_rows)
    rows[0][1]["Name"] = FAKE_NAME
    rows[1][1]["Notes"] = FAKE_NOTE
    rows[2][1]["General"] = "sample for patient AB 1234"
    run_sync(conn, rows, [{"Request ID": "REQ-1-001", "Org Name": "C", "Org Email": "c@example.org",
                           "Item Name": FAKE_NAME, "Quantity Requested": "1", "Status": "Under Review"}])
    return conn


def _all_text(conn, sql):
    return "\n".join(str(v) for row in conn.execute(sql).fetchall() for v in row)


@pytest.mark.parametrize("view", ["dash_items", "dash_supplies", "dash_requests", "v_inventory",
                                  "v_review_queue", "v_expiring"])
def test_no_view_exposes_the_identifiers(phi_loaded, view):
    text = _all_text(phi_loaded, f"SELECT * FROM {view}")
    for secret in ("0000000000-0", "9999999999-9", "XX00", "ZZ99", "555-010-0199", "AB 1234"):
        assert secret not in text, (view, secret)


def test_raw_tables_keep_the_original_for_hq(phi_loaded):
    assert phi_loaded.execute("SELECT count(*) FROM inventory_item WHERE raw_name = %s",
                              (FAKE_NAME,)).fetchone()[0] == 1


def test_phi_rows_hide_the_photo_and_are_flagged(phi_loaded):
    got = phi_loaded.execute(
        "SELECT d.sheet_row, d.name, d.image_url, s.\"Image URL\", i.flagged, i.review_reason "
        "FROM dash_items d JOIN inventory_item i ON i.item_id::text = d.id "
        "JOIN dash_supplies s ON s.\"_sheet_row\" = d.sheet_row WHERE d.sheet_row IN (2, 3, 4) "
        "ORDER BY d.sheet_row").fetchall()
    (r2, n2, img2, simg2, f2, why2), (r3, _, img3, simg3, f3, why3), (r4, _, img4, _, f4, why4) = got
    assert n2 == "SM SERUM MARKER PRE Pt. ID [redacted]"
    assert img2 is None and simg2 == "" and img3 is None and simg3 == ""   # photo shows the same label
    assert f2 and "contains patient identifier" in why2
    assert f3 and "contains patient identifier" in why3
    assert img4 is None and f4 and "contains patient identifier" in why4  # General column counts too
    queue = phi_loaded.execute("SELECT review_reason FROM v_review_queue WHERE sheet_row = 2").fetchone()[0]
    assert "contains patient identifier" in queue


def test_rules_version_bump_reprocesses_rows(conn, sheet_rows, monkeypatch):
    run_sync(conn, sheet_rows, [])
    monkeypatch.setattr(N, "RULES_VERSION", N.RULES_VERSION + 1)
    s = run_sync(conn, sheet_rows, [])
    assert s["items_updated"] == 1136 and s["items_added"] == 0
    assert run_sync(conn, sheet_rows, [])["items_updated"] == 0


# ---- fixtures must be free of real patient identifiers ----
import csv
import gzip
import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
# specimen/subject numbers on tube labels (65xxxxxxxx[-n]) and "Pt. ID" followed by digits;
# ordinary 10-digit catalog/lot numbers are fine
LEAK_RE = re.compile(r"\b65\d{8}(?:-\w+)?\b|(?i:\bp(?:t|l|atient)\.? ?id\b[:,]? *\d{5,})")


def _strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


def _fixture_texts():
    for p in sorted(FIXTURES.iterdir()):
        if p.suffix == ".gz":
            yield p.name, "\n".join(_strings(json.loads(gzip.decompress(p.read_bytes()))))
        elif p.suffix == ".json":
            yield p.name, "\n".join(_strings(json.loads(p.read_text())))
        elif p.suffix == ".csv":
            yield p.name, p.read_text()


@pytest.mark.parametrize("name,text", list(_fixture_texts()), ids=lambda v: v if len(str(v)) < 40 else "")
def test_fixtures_contain_no_patient_identifiers(name, text):
    leaks = [m.group() for m in LEAK_RE.finditer(text)]
    assert not leaks, f"{name}: {sorted(set(leaks))[:5]}"


# ---- bug-hunter round: every column, invisible characters, names, units ----
ZW = "​"


@pytest.mark.parametrize("text,gone", [
    ("patient 12345" + ZW + "6", "123456"),
    ("pa" + ZW + "tient 12345", "12345"),
    ("Ｐatient 123456", "123456"),                     # full-width P
    ("Pt.‮ ID 7777777", "7777777"),                   # bidi override
    ("Patient: Jane Doe", "Jane"),
    ("Patient John Smith", "Smith"),
    ("Pt. Name: J. Smith", "Smith"),
])
def test_obfuscated_and_named_patients_are_redacted(conn, text, gone):
    out = conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0]
    assert gone not in out and "redacted" in out
    assert N.has_patient_id(text)
    assert conn.execute("SELECT b4p_has_phi(%s)", (text,)).fetchone()[0]


@pytest.mark.parametrize("text", ["Patient Return Electrode", "PATIENT SLIPPERS Youth", "BD SmartSite™ Set",
                                  "Patient Monitoring System", "Patient Use Pediatric"])
def test_names_rule_leaves_products_alone(conn, text):
    assert conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0] == text
    assert not N.has_patient_id(text)


def test_every_text_column_is_redacted_and_hides_the_photo(conn, sheet_rows):
    rows = copy.deepcopy(sheet_rows)
    for (_, r), col, sheet_row in ((rows[0], "Lot Number", 2), (rows[1], "Manufacturer Name", 3),
                                   (rows[2], "Review", 4), (rows[3], "Subcategory", 5),
                                   (rows[4], "General", 6)):
        r[col] = "Pt. ID " "4040404040"
    run_sync(conn, rows, [])
    for view in ("dash_supplies", "dash_items", "v_inventory"):
        text = _all_text(conn, f"SELECT * FROM {view} WHERE " + ("\"_sheet_row\"" if view == "dash_supplies" else "sheet_row")
                         + " BETWEEN 2 AND 6")
        assert "4040404040" not in text, view
    imgs = conn.execute('SELECT "Image URL" FROM dash_supplies WHERE "_sheet_row" BETWEEN 2 AND 6').fetchall()
    assert all(i[0] == "" for i in imgs)
    flagged = conn.execute("SELECT count(*) FROM inventory_item WHERE sheet_row BETWEEN 2 AND 6 AND flagged "
                           "AND review_reason LIKE '%patient identifier%'").fetchone()[0]
    assert flagged == 5


def test_dash_supplies_emits_parsed_quantity_and_date(conn, sheet_rows):
    rows = copy.deepcopy(sheet_rows)
    rows[0][1]["Quantity"] = "20 (pens)"
    rows[0][1]["Date of Expiration"] = "01/2030"
    run_sync(conn, rows, [])
    q, d = conn.execute('SELECT "Quantity", "Date of Expiration" FROM dash_supplies WHERE "_sheet_row" = %s',
                        (rows[0][0],)).fetchone()
    assert q == "20" and d[:4] == "2030" and len(d) == 10


def test_money_and_quantity_edge_cases():
    assert N.parse_money("$-4") is None and N.parse_money("-4") is None and N.parse_money("$4") == 4.0
    base = {"Name": "x", "Quantity": "1e3"}
    assert any("ambiguous" in p for p in N.read_inventory_row(base)["problems"])
    assert any("ambiguous" in p for p in N.read_inventory_row({"Name": "x", "Quantity": "5-10"})["problems"])
    assert not any("ambiguous" in p for p in N.read_inventory_row({"Name": "x", "Quantity": "12"})["problems"])


# ---- end-to-end: PHI-laden synthetic rows never survive in any dashboard view ----
SECRETS = ["8675309111", "MRN-" "4455667", "03/04/1961", "jane.doe@example.org", "555-010-0142",
           "Jane Doe", "Smith", "A11-222", "5551110000"]
# (column, value-with-secret) -- every free-text column of the sheet
PHI_CELLS = {
    "Name": "SERUM TUBE Pt. ID " "8675309111 QQ11",
    "General": "Patient: Jane Doe, MRN: " "MRN-" "4455667 as written",
    "Notes": "DOB " "03/04/1961, call 555-010-0142 or mail jane.doe@example.org",
    "Lot Number": "Pt. ID " "5551110000",
    "Manufacturer Name": "patient John Smith",
    "Category": "ST/N",
    "Subcategory": "Gloves",
    "Review": "Pt. ID " "8675309111",
    "Box Number": "3",
}


def test_no_patient_data_survives_in_any_dashboard_view(conn, sheet_rows):
    rows = copy.deepcopy(sheet_rows)[:12]
    for n, (_, r) in enumerate(rows[:len(PHI_CELLS)]):
        col = list(PHI_CELLS)[n]
        r[col] = PHI_CELLS[col]
    rows[-1][1]["Name"] = "tube A11-222 sample"
    run_sync(conn, rows, [{"Request ID": "REQ-1-001", "Org Name": "C", "Org Email": "c@example.org",
                           "Item Name": PHI_CELLS["Name"], "Quantity Requested": "1", "Status": "Under Review"}])
    views = [r[0] for r in conn.execute(
        "SELECT viewname FROM pg_views WHERE schemaname = 'public' AND (viewname LIKE 'dash\\_%' "
        "OR viewname LIKE 'v\\_%')").fetchall()]
    assert {"dash_items", "dash_supplies", "dash_requests", "v_inventory"} <= set(views)
    for view in views:
        cols = [c[0] for c in conn.execute(f"SELECT * FROM {view} LIMIT 0").description]
        keep = [c for c in cols if c != "Org Email"]   # a requester's own contact address is theirs to see
        sel = ", ".join(f'"{c}"' for c in keep)
        text = _all_text(conn, f"SELECT {sel} FROM {view}")
        for secret in SECRETS:
            assert secret not in text, (view, secret)


# ---- round 2: accents, apostrophes, case, short names, DOB words, SSN, look-alikes ----
NAME_CASES = [
    ("Patient: José García", "José"), ("patient: john smith", "john"), ("PATIENT: JOHN SMITH", "JOHN"),
    ("Pt: O'Brien, Mary", "Brien"), ("Patient Johnson", "Johnson"), ("Pt. Kowalski", "Kowalski"),
    ("Pt. Name: J. Smith", "Smith"), ("patient: Łukasz Nowak", "Nowak"),
    ("DOB: March 3 1980", "1980"), ("born on 3rd March 1980", "1980"), ("DOB 3 Mar 1980", "1980"),
    ("SSN 123-45-6789", "6789"),
    ("tube a11 - 222", "222"), ("tube A11–222", "222"), ("tube А11-222", "222"),
]


@pytest.mark.parametrize("text,gone", NAME_CASES)
def test_round2_leaks_are_closed(conn, text, gone):
    out = conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0]
    assert gone not in out, out
    assert N.has_patient_id(text)


@pytest.mark.parametrize("text", [
    "Patient Wound Dressing", "Patient Glove Box", "Patient Lift Sling", "Patient Gown XL", "Patient Kit",
    "Patient Starter Kit", "Patient Return Electrode", "Patient Monitoring Cable",
])
def test_round2_products_survive(conn, text):
    assert conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0] == text
    assert not N.has_patient_id(text)


def _pg(p):
    return p.replace(r"\b", r"\y")


def test_sql_and_python_patterns_are_the_same_text(conn):
    for fn, py in (("b4p_patient_name_pattern", N.PATIENT_NAME_PATTERN),
                   ("b4p_patient_surname_pattern", N.PATIENT_SURNAME_PATTERN),
                   ("b4p_dob_pattern", N.DOB_PATTERN), ("b4p_subject_id_pattern", N.SUBJECT_ID_PATTERN)):
        assert conn.execute(f"SELECT {fn}()").fetchone()[0] == _pg(py), fn


@pytest.mark.parametrize("text", [c[0] for c in NAME_CASES] + ["BD SmartSite™ Set", "Dr. Jones' tray", "Lot 2024-05-01"]
                         + KEEP)
def test_fold_and_detection_agree_between_sql_and_python(conn, text):
    sql_fold = conn.execute("SELECT b4p_fold(%s)", (text,)).fetchone()[0]
    assert sql_fold == N.fold(text)
    sql_hit = conn.execute("SELECT b4p_has_phi(%s)", (text,)).fetchone()[0]
    assert sql_hit == N.has_patient_id(text) or (sql_hit and not N.has_patient_id(text))  # SQL also covers email/phone/MRN


# ---- round 3: dash separators, lowercase surnames, and supply names that start with "Patient" ----
SEPARATOR_CASES = [("Patient - Smith, John", "Smith"), ("patient name - Jane Doe", "Jane"),
                   ("pt. smith", "smith"), ("Pt Kowalski, 3 east", "Kowalski"),
                   ("Patient Johnson DOB 1/2/1980", "Johnson"), ("Patient Nowak 4411", "Nowak")]
SUPPLY_TAILS = [
    'Lift',
    'Slide Sheet',
    'Restraint',
    'Mattress',
    'Hygiene Wipes',
    'Call Cord',
    'Handling Belt',
    'Alarm',
    'Positioner',
    'Warming Blanket',
    'Thermometer',
    'Specimen Bag',
    'Pack',
    'Shoe Covers',
    'Gown',
    'Monitor',
    'Cart',
    'Chair',
    'Scale',
    'Scrubs',
    'Garments',
    'Bath Basin',
    'Bed',
    'Cushion',
    'Pillow',
    'Towel',
    'Wipes',
    'Tray',
    'Table',
    'Hoist',
    'Walker',
    'Transfer Sling',
    'Lift Sling',
    'Comfort Kit',
    'Care Pack',
    'Isolation Gown',
    'Wash Cloth',
    'Urinal',
    'Bedpan',
    'Hand Wash',
    'Skin Cleanser',
    'Hearing Aid',
    'Mask',
    'Drape',
    'Hair Cap',
    'Slippers',
    'Socks',
    'Bib',
    'Education Folder',
    'Information Sheet',
    'Identification Band',
    'Transport Stretcher',
    'Warmer',
    'Cooling Pad',
    'Oxygen Tubing',
    'Suction Canister',
    'Gait Belt',
    'Fall Mat',
    'Protection Pad',
    'Positioning Wedge',
    'Turn Assist',
    'Valuables Bag',
    'Belongings Bag',
    'Pressure Cuff',
    'Simulator',
    'Lifter',
    'Gauze',
    'Needle',
    'Syringe',
    'Bandage',
    'Tape',
    'Controlled Analgesia Pump',
    'Return Electrode',
    'Starter Kit',
    'Use Resuscitator',
    'Preoperative Skin Prep',
    'Lateral Transfer Device',
    'Safety Vest',
    'Charting Pen',
    'Chart Holder',
    'Stool',
    'Weighing Scale',
    'Privacy Screen',
    'Warming Gown',
    'Support Stocking',
    'Compression Sleeve',
    'Positioning System']


@pytest.mark.parametrize("text,gone", SEPARATOR_CASES)
def test_round3_names_after_dashes_and_lowercase(conn, text, gone):
    out = conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0]
    assert gone not in out, out
    assert N.has_patient_id(text)


@pytest.mark.parametrize("head", ["Patient", "Pt", "Pt.", "PATIENT", "patient", "PT", "pt."])
def test_round3_supply_names_that_start_with_patient_are_untouched(conn, head):
    names = [f"{head} {t}" for t in SUPPLY_TAILS]
    got = conn.execute("SELECT s, redact_phi(s) FROM unnest(%s::text[]) s", (names,)).fetchall()
    assert [a for a, b in got if a != b] == []
    assert [n for n in names if N.has_patient_id(n)] == []


# ---- round 4: names with unexpected tails, initials, particles, non-Latin scripts ----
ROUND4 = [("Pt Smith 3E", "Smith"), ("Patient Smith.", "Smith"), ("Patient Smith, Room 12", "Smith"),
          ("Pt. Smith Rm 4", "Smith"), ("Patient Smith bed 3", "Smith"), ("Patient Smith (3E)", "Smith"),
          ("Patient Smith\nRoom 5", "Smith"), ("Pt Smith-Jones", "Jones"), ("Patient Smith Jr.", "Smith"),
          ("Pt Smith 05/04", "Smith"), ("Patient Smith/Jones", "Jones"), ("Patient J. Smith", "Smith"),
          ("Patient J Smith", "Smith"), ("Patient John", "John"), ("patient John Q Public", "Public"),
          ("Pt Garcia-Lopez", "Lopez"), ("Patient: Smith Room 12", "Smith"), ("Pt O'Neil 3E", "Neil"),
          ("Pt de la Cruz", "Cruz"), ("Pt van der Berg", "Berg"), ("Patient McDonald", "McDonald"),
          ("Patient D'Angelo", "Angelo"), ("PT SMITH", "SMITH"), ("Patient \u65e5\u672c\u8a9e", "\u65e5"),
          ("Patient \u5c71\u7530 \u592a\u90ce", "\u592a"), ("Patient: \u5c71\u7530", "\u5c71"),
          ("Patient: \u5c71\u7530 \u592a\u90ce", "\u592a\u90ce"), ("patient sm1th", "th")]


@pytest.mark.parametrize("text,gone", ROUND4)
def test_round4_names_with_odd_tails(conn, text, gone):
    out = conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0]
    assert gone not in out, out
    assert N.has_patient_id(text)


def test_known_gaps_have_no_keyword_so_rely_on_review_and_photo_hiding(conn):
    for text in ("Name: John Smith", "Mr. Smith patient"):   # documented in docs/CHANGES-2026-10-11.md
        assert conn.execute("SELECT redact_phi(%s)", (text,)).fetchone()[0] == text
