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
    "ports labeled 'Tandem', 'Patient', and 'Vacuum'", "Lot 2024-05-01", "REF 305-555", "12 pt font",
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
    sql = conn.execute("SELECT %s ~ b4p_patient_id_pattern() OR %s ~ b4p_subject_id_pattern()",
                       (text, text)).fetchone()[0]
    assert N.has_patient_id(text) == sql


def test_python_and_sql_agree_on_the_real_fixture(conn, sheet_rows):
    for _, r in sheet_rows:
        for col in ("Name", "Notes", "General"):
            t = r.get(col, "")
            sql = conn.execute("SELECT %s ~ b4p_patient_id_pattern() OR %s ~ b4p_subject_id_pattern()",
                               (t, t)).fetchone()[0]
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
    assert img4 is not None  # General-only mention: text redacted, photo of a normal row kept
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
