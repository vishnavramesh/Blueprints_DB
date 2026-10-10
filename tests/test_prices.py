"""`python -m b4p prices` / `unpriced`: matching, provenance, manual prices, API adapter."""
import csv
import io
import json

import pytest

import pricing_api_stub
from b4p import cli, prices
from b4p.sync import load_rules, run_sync

URL_A = "https://drive.google.com/file/d/A/view"


def sheet_row(name, mfr="", qty="10", url="", sub="Needles & Syringes"):
    return {"Name": name, "Manufacturer Name": mfr, "Quantity": qty, "Image URL": url,
            "Date of Expiration": "", "Pallet Number": "1", "Box Number": "2", "Category": "ST/N",
            "Subcategory": sub, "General": "", "Review": "No"}


def priced(name, mfr, price, url=""):
    return sheet_row(name, mfr, url=url) | {"Per-Unit Price": price}


INVENTORY = [
    sheet_row("BD 10ml Syringe Luer-Lok Tip", "Becton, Dickinson and Company", "100"),
    sheet_row("Monoject 12 mL Oral/Enteral Fluids Syringe", "Monoject", "20"),
    sheet_row("Webcol Alcohol Prep 2 Ply Medium", "Cardinal Health", "60"),
    sheet_row("BD 5ml Syringe Luer-Lok Tip", "BD", "30"),                    # only a 10 ml price exists
    sheet_row("Extended Cuff Exam Gloves - Medium", "Medline", "40", sub="Gloves"),  # only Large priced
    sheet_row("Mystery widget", "", "5", url=URL_A),                          # priced via its photo
    sheet_row("Gauze Sponges 4 x 4 in", "Medline", "50", sub="Wound Care & Bandages"),
]

PRICED = [
    priced("BD 10ml Syringe Luer-Lok™ Tip", "BD", "$0.40"),
    priced("bd 10ml syringe luer-lok tip", "Becton Dickinson", "0.60"),
    priced("Monoject Oral/Enteral Syringe 12 mL", "MONOJECT", "0.48"),       # similarity 0.83 -> suggested
    priced("Webcol™ Alcohol Prep - 2 Ply, Medium", "Cardinal Health", "0.05"),  # similarity 1.0 -> applied
    priced("Extended Cuff Exam Gloves - Large", "Medline", "0.43"),
    priced("something else entirely", "", "7", url=URL_A),
    priced("Gauze Sponges 4 x 4 in", "Medline", "NEED HUMAN OVERSIGHT"),
    priced("shoe covers", "", "119900"),
]

# The debugger's attack rows: each priced row is a DIFFERENT product than the
# supply it resembles, or carries a bad price. None may be applied.
ATTACK_INVENTORY = [
    sheet_row("Ultra(TM) 4-Way Stopcock w/Swivel Male Luer Lock MX2341L", "Smiths Medical ASD, Inc"),
    sheet_row("Male Urinal Bottle 1000 mL / 32 oz", ""),
    sheet_row("BD PureWick Male External Catheter", "BD"),
    sheet_row("COVER MAX SURGICAL CAP Blue Universal Size", "HALYARD"),
    sheet_row("Sterile Specimen Container", "Medline Industries, Inc."),
    sheet_row("Sage 2% Chlorhexidine Gluconate* Cloth Patient preoperative skin preparation Non-sterile", "stryker"),
    sheet_row("ENCORE Latex Ortho DERMASHIELD Donning Aid Technology Size 6 1/2", "Ansell"),
    sheet_row("Ambu Disposable Face Mask Adult L", "Ambu"),
    sheet_row("MEDRAD Stellant FLEX 150mL Sterile Disposable Syringes with Small Contrast Spikes", "Bayer / MEDRAD"),
    sheet_row("62697 TELFA PACK UNIVERSITY OF MICHIGAN HOSP", "Cardinal Health"),
    sheet_row("Blueprints For Pangaea Embroidered Sweatshirt", "Blueprints For Pangaea"),
    sheet_row("DURA-CUF Adult Long Blood Pressure Cuff 23-33 CM", "CRITIKON"),
]
ATTACK_PRICED = [
    priced("Ultra(TM) 4-Way Stopcock w/Swivel Female Luer Lock MX2341L", "Smiths Medical ASD, Inc", "4.10"),
    priced("Female Urinal Bottle 1000 mL / 32 oz", "", "9.99"),
    priced("BD PureWick Female External Catheter", "BD", "18.00"),
    priced("COVER MAX SURGICAL CAP Purple Universal Size", "HALYARD", "0.70"),
    priced("Non-Sterile Specimen Container", "Medline Industries, Inc.", "0.20"),
    priced("Sage 2% Chlorhexidine Gluconate Cloth Patient preoperative skin preparation Sterile", "stryker", "3.00"),
    priced("ENCORE Latex-Free Ortho DERMASHIELD Donning Aid Technology Size 6 1/2", "Ansell", "2.50"),
    priced("Ambu Disposable Face Mask Adult L (Case)", "Ambu", "95.00"),
    priced("MEDRAD Stellant FLEX 150mL Sterile Disposable Syringes with Small Contrast Spikes", "Bayer / MEDRAD",
           "-12.00"),
    priced("62697 TELFA PACK UNIVERSITY OF MICHIGAN HOSP", "Cardinal Health", "0.45"),
    priced("62697 TELFA PACK UNIVERSITY OF MICHIGAN HOSP", "Cardinal Health", "45.00"),
    priced("Blueprints For Pangaea Embroidered Sweatshirt", "Blueprints For Pangaea", "1e9"),
    priced("DURA-CUF Adult Long Blood Pressure Cuff 23-33 CM", "CRITIKON", "€5,50"),
]


@pytest.fixture
def loaded(conn):
    run_sync(conn, list(enumerate(INVENTORY, start=2)), [], source="test")
    return conn


def supply_price(conn, name):
    return conn.execute("SELECT unit_value_usd::FLOAT8 FROM supply WHERE name = %s", (name,)).fetchone()[0]


def provenance(conn, name, source="sheet"):
    return conn.execute("SELECT status, price::FLOAT8, matched_by, score FROM supply_price p "
                        "JOIN supply s USING (supply_id) WHERE s.name = %s AND p.source = %s",
                        (name, source)).fetchone()


def run_sheet(conn, rows=PRICED, **kw):
    with conn.cursor() as cur:
        good, rejected = prices.read_priced_rows(list(enumerate(rows, start=2)), load_rules(cur)["manufacturer"])
        matches = prices.match_sheet(cur, good, **kw)
        stats = prices.apply_prices(cur, matches, prices.SHEET)
    conn.commit()
    return matches, rejected, stats


def test_norm_name_and_signature():
    assert prices.norm_name(" BD Insyte™ Autoguard™  22 GA ") == "bd insyte autoguard 22 ga"
    assert prices.norm_name("{sterile wipe}") == "sterile wipe"
    sig = lambda s: prices.signature(prices.norm_name(s))
    assert sig("bd 10ml syringe") != sig("bd 5ml syringe")
    assert sig("gloves medium") != sig("gloves large")
    assert sig("sterile container") != sig("non-sterile container")
    assert sig("latex gloves") != sig("latex-free gloves")
    assert sig("male urinal") != sig("female urinal")
    assert sig("cap blue") != sig("cap purple")
    assert sig("mask adult l") != sig("mask adult l (case)")
    assert sig("needle 22 GA") == sig("needle 22G")
    assert sig("tube 08fr") == sig("tube 8 Fr")
    assert sig("a 5,0 cm") == sig("a 5.0 cm")
    assert sig("bottle 1,000 mL") == sig("bottle 1000 ml")


@pytest.mark.parametrize("cell, value, ok", [
    ("$1,234.50", 1234.5, True), ("0.1399", 0.1399, True), ("0.7508333333", 0.7508333333, True), ("12", 12, True), ("$ 3", 3, True),
    ("", None, True), ("-12.00", None, False), ("(5.00)", None, False), ("1e9", None, False),
    ("€5,50", None, False), ("12 per box of 100", None, False), ("0", None, False), ("0.00", None, False),
    ("119900", None, False), ("1,23", None, False), ("NEED HUMAN OVERSIGHT", None, False),
])
def test_parse_price(cell, value, ok):
    got, problem = prices.parse_price(cell)
    assert got == (pytest.approx(value) if value is not None else None)
    assert (problem is None) == ok


def test_sheet_prices_match_conservatively(loaded):
    matches, rejected, stats = run_sheet(loaded)
    assert {(r, c) for r, c, _ in rejected} == {(8, "NEED HUMAN OVERSIGHT"), (9, "119900")}
    # exact name + manufacturer (aliases resolved), median of the two rows
    assert supply_price(loaded, "BD 10ml Syringe Luer-Lok Tip") == pytest.approx(0.50)
    assert provenance(loaded, "BD 10ml Syringe Luer-Lok Tip") == ("applied", 0.5, "name+manufacturer", 1.0)
    # fuzzy >= 0.95: applied; fuzzy < 0.95: only suggested
    assert supply_price(loaded, "Webcol Alcohol Prep 2 Ply Medium") == pytest.approx(0.05)
    assert provenance(loaded, "Webcol Alcohol Prep 2 Ply Medium")[:3] == ("applied", 0.05, "trgm")
    assert supply_price(loaded, "Monoject 12 mL Oral/Enteral Fluids Syringe") is None
    assert provenance(loaded, "Monoject 12 mL Oral/Enteral Fluids Syringe")[:3] == ("suggested", 0.48, "trgm")
    # numbers / sizes must match
    assert supply_price(loaded, "BD 5ml Syringe Luer-Lok Tip") is None
    assert supply_price(loaded, "Extended Cuff Exam Gloves - Medium") is None
    # same photo
    assert supply_price(loaded, "Mystery widget") == pytest.approx(7)
    assert provenance(loaded, "Mystery widget")[2] == "image_url"
    assert stats == {"withdrawn": 0, "set": 3, "suggested": 1}


def test_attack_rows_are_never_applied(conn):
    run_sync(conn, list(enumerate(ATTACK_INVENTORY, start=2)), [], source="test")
    matches, rejected, stats = run_sheet(conn, ATTACK_PRICED)
    assert conn.execute("SELECT count(*) FROM supply WHERE unit_value_usd IS NOT NULL").fetchone()[0] == 0
    assert {c for _, c, _ in rejected} == {"-12.00", "1e9", "€5,50"}
    # same name, prices 0.45 and 45.00 (unit vs case of 100) -> conflict, not a median
    assert provenance(conn, "62697 TELFA PACK UNIVERSITY OF MICHIGAN HOSP")[:2] == ("conflict", None)
    assert stats.get("set", 0) == 0


def test_threshold_is_respected(loaded):
    run_sheet(loaded, threshold=0.99)
    assert provenance(loaded, "Monoject 12 mL Oral/Enteral Fluids Syringe") is None


def test_dashboards_show_prices(loaded):
    run_sheet(loaded)
    row = loaded.execute("SELECT quantity, unit_value, total_value FROM dash_items "
                         "WHERE name = 'BD 10ml Syringe Luer-Lok Tip'").fetchone()
    assert row == (100, 0.5, 50.0)
    assert loaded.execute("SELECT total_value::FLOAT8 FROM v_kpis").fetchone()[0] == \
        pytest.approx(50 + 60 * 0.05 + 5 * 7)


def test_manual_price_is_never_overwritten(loaded):
    loaded.execute("UPDATE supply SET unit_value_usd = 9.99 WHERE name = 'BD 10ml Syringe Luer-Lok Tip'")
    loaded.commit()
    _, _, stats = run_sheet(loaded)
    assert supply_price(loaded, "BD 10ml Syringe Luer-Lok Tip") == pytest.approx(9.99)
    assert stats["kept_manual"] == 1


def test_retyping_the_same_price_by_hand_pins_it(loaded):
    run_sheet(loaded)
    loaded.execute("UPDATE supply SET unit_value_usd = 0.50 WHERE name = 'BD 10ml Syringe Luer-Lok Tip'")
    loaded.commit()
    changed = [dict(r) for r in PRICED]
    changed[0]["Per-Unit Price"] = changed[1]["Per-Unit Price"] = "0.70"
    run_sheet(loaded, changed)
    assert supply_price(loaded, "BD 10ml Syringe Luer-Lok Tip") == pytest.approx(0.50)
    # clearing it by hand removes the pin
    loaded.execute("UPDATE supply SET unit_value_usd = NULL WHERE name = 'BD 10ml Syringe Luer-Lok Tip'")
    loaded.commit()
    run_sheet(loaded, changed)
    assert supply_price(loaded, "BD 10ml Syringe Luer-Lok Tip") == pytest.approx(0.70)


def test_automatic_price_follows_the_sheet(loaded):
    run_sheet(loaded)
    changed = [dict(r) for r in PRICED]
    changed[3]["Per-Unit Price"] = "0.07"
    run_sheet(loaded, changed)
    assert supply_price(loaded, "Webcol Alcohol Prep 2 Ply Medium") == pytest.approx(0.07)
    # a row that disappears from the tab takes its price with it
    _, _, stats = run_sheet(loaded, [r for i, r in enumerate(changed) if i != 3])
    assert supply_price(loaded, "Webcol Alcohol Prep 2 Ply Medium") is None
    assert stats["withdrawn"] == 1 and provenance(loaded, "Webcol Alcohol Prep 2 Ply Medium") is None


def test_one_bad_value_does_not_undo_the_rest(loaded):
    with loaded.cursor() as cur:
        sids = [r[0] for r in cur.execute("SELECT supply_id FROM supply ORDER BY supply_id LIMIT 2")]
        good = {"price": 1.0, "matched_by": "x", "score": None, "detail": None, "status": "applied"}
        stats = prices.apply_prices(cur, {sids[0]: good, sids[1]: good | {"price": 10 ** 12}}, prices.API)
    loaded.commit()
    assert stats == {"set": 1, "error": 1}
    assert loaded.execute("SELECT count(*) FROM supply WHERE unit_value_usd = 1").fetchone()[0] == 1


def test_review_csv_export_and_manual_import(loaded):
    loaded.execute("UPDATE supply SET name = '=HYPERLINK(\"https://evil.test\")' WHERE name = 'BD 5ml Syringe Luer-Lok Tip'")
    loaded.commit()
    run_sheet(loaded)
    out = io.StringIO()
    with loaded.cursor() as cur:
        n = prices.export_unpriced(cur, out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert n == len(rows) == 4
    assert list(rows[0]) == prices.EXPORT_COLUMNS
    assert rows[0]["name"] == "Gauze Sponges 4 x 4 in" and rows[0]["units"] == "50"  # most units first
    assert rows[0]["category"] == "Wound Care & Bandages"
    by_name = {r["name"]: r for r in rows}
    assert "'=HYPERLINK(\"https://evil.test\")" in by_name               # formula neutralized
    mono = by_name["Monoject 12 mL Oral/Enteral Fluids Syringe"]
    assert mono["suggested_price"] == "0.48" and mono["suggestion"].startswith("suggested (trgm 0.83)")
    # a person accepts the suggestion, prices the gauze, blocks the gloves, and makes two mistakes
    mono["unit_value_usd"] = mono["suggested_price"]
    by_name["Gauze Sponges 4 x 4 in"]["unit_value_usd"] = "$0.09"
    by_name["Extended Cuff Exam Gloves - Medium"]["no_auto_price"] = "yes"
    rows.append({"supply_id": "999999", "unit_value_usd": "3"})
    rows.append({"supply_id": "1", "unit_value_usd": "12 per box of 100"})
    rows.append({"supply_id": "1", "unit_value_usd": "20000"})
    filled = io.StringIO()
    w = csv.DictWriter(filled, prices.EXPORT_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    matches, problems = prices.read_manual_csv(io.StringIO(filled.getvalue()))
    assert len(problems) == 2 and "12 per box" in problems[0] and "max price" in problems[1]
    with loaded.cursor() as cur:
        stats = prices.apply_prices(cur, matches, prices.MANUAL)
    loaded.commit()
    assert stats == {"set": 2, "blocked": 1, "unknown": 1}
    assert supply_price(loaded, "Monoject 12 mL Oral/Enteral Fluids Syringe") == pytest.approx(0.48)
    assert supply_price(loaded, "Gauze Sponges 4 x 4 in") == pytest.approx(0.09)
    # later automatic runs leave manual prices and blocked supplies alone
    run_sheet(loaded, PRICED + [priced("Gauze Sponges 4 x 4 in", "Medline", "1"),
                                priced("Extended Cuff Exam Gloves - Medium", "Medline", "0.5")])
    assert supply_price(loaded, "Gauze Sponges 4 x 4 in") == pytest.approx(0.09)
    assert supply_price(loaded, "Extended Cuff Exam Gloves - Medium") is None
    assert provenance(loaded, "Extended Cuff Exam Gloves - Medium", "manual")[0] == "blocked"


# ---------------------------------------------------------------------------
# B4P Pricing Enrichment API adapter, against a local stub of its contract
# ---------------------------------------------------------------------------
@pytest.fixture
def api():
    server, url = pricing_api_stub.start(
        {"bd 5ml syringe luer-lok tip": 0.3, "mystery widget": 4.0,
         "extended cuff exam gloves - medium": 0.2},
        review={"extended cuff exam gloves - medium"}, max_batch_size=2)
    yield server, url
    server.shutdown()


def test_enrich_via_api(loaded, api):
    server, url = api
    run_sheet(loaded)
    with loaded.cursor() as cur:
        matches, stats = prices.enrich_via_api(cur, url, "test-key", batch_size=2)
        applied = prices.apply_prices(cur, matches, prices.API)
    loaded.commit()
    # only unpriced supplies are sent, in batches no bigger than the server allows
    sent = [i["name"] for body in server.requests for i in body["items"]]
    assert sorted(sent) == ["BD 5ml Syringe Luer-Lok Tip", "Extended Cuff Exam Gloves - Medium",
                            "Gauze Sponges 4 x 4 in", "Monoject 12 mL Oral/Enteral Fluids Syringe"]
    assert all(len(b["items"]) <= 2 for b in server.requests)
    assert stats["sent"] == 4 and stats["priced"] == 1 and stats["rejected"] == 3 and stats["error"] is None
    assert applied == {"set": 1}
    assert supply_price(loaded, "BD 5ml Syringe Luer-Lok Tip") == pytest.approx(0.3)
    assert supply_price(loaded, "Extended Cuff Exam Gloves - Medium") is None  # needs_review -> not used
    assert supply_price(loaded, "Mystery widget") == pytest.approx(7)          # already priced, not sent
    assert provenance(loaded, "BD 5ml Syringe Luer-Lok Tip", "f25-api") == \
        ("applied", 0.3, "f25:cache", pytest.approx(0.9))


def test_enrich_via_api_errors_are_reported(loaded, api):
    _, url = api
    with loaded.cursor() as cur:
        matches, stats = prices.enrich_via_api(cur, url, "wrong-key")
    assert matches == {} and "401" in stats["error"]
    with loaded.cursor() as cur:  # batch too large -> 413, stop
        matches, stats = prices.enrich_via_api(cur, url, "test-key", batch_size=5)
    assert matches == {} and "413" in stats["error"]
    with loaded.cursor() as cur:
        matches, stats = prices.enrich_via_api(cur, url, "test-key", batch_size=2, deadline=0)
    assert matches == {} and "deadline" in stats["error"]


@pytest.mark.parametrize("answer, ok", [
    ({}, True),
    ({"unit_price": True}, False),                 # bool is not a number
    ({"unit_price": "3.5"}, False),
    ({"unit_price": float("nan")}, False),
    ({"currency": None}, False),                   # missing currency -> reject
    ({"currency": "EUR"}, False),
    ({"confidence": "high"}, False),
    ({"confidence": None}, True),
    ({"needs_review": None}, False),
    ({"status": "needs_review"}, False),
    ({"external_id": 7}, False),
    ({"external_id": "999"}, False),               # not something we sent
])
def test_api_answer_validation(answer, ok):
    res = {"external_id": "1", "status": "priced", "needs_review": False, "currency": "USD",
           "unit_price": 3.5, "confidence": 0.8, "source": "cache"} | answer
    assert (prices._api_match(res, {1}, 10_000) is not None) == ok


def test_enrich_response_size_cap(loaded, monkeypatch):
    server, url = pricing_api_stub.start({"bd 5ml syringe luer-lok tip": 0.3})
    monkeypatch.setattr(prices, "API_MAX_RESPONSE", 100)
    try:
        with loaded.cursor() as cur:
            matches, stats = prices.enrich_via_api(cur, url, "test-key")
    finally:
        server.shutdown()
    assert matches == {} and "larger than" in stats["error"]


def test_cli_prices_and_unpriced(loaded, api, tmp_path, monkeypatch, capsys):
    _, url = api
    priced_csv = tmp_path / "priced.csv"
    with open(priced_csv, "w", newline="") as f:
        w = csv.DictWriter(f, list(PRICED[0]))
        w.writeheader()
        w.writerows(PRICED)
    assert cli.main(["prices", "--csv", str(priced_csv), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "matched 4 supplies" in out and "dry run" in out
    assert "rejected price 'NEED HUMAN OVERSIGHT' (not a plain dollar amount) x1: rows 8" in out
    assert supply_price(loaded, "Mystery widget") is None
    assert cli.main(["prices", "--enrich-url", url, "--skip-sheet"]) == 2  # no key configured
    monkeypatch.setenv("PRICING_API_KEY", "test-key")
    assert cli.main(["prices", "--csv", str(priced_csv), "--enrich-url", url, "--batch-size", "2"]) == 0
    out = capsys.readouterr().out
    assert "f25-api: sent 4, priced 1" in out and "priced: 4/7 supplies" in out
    export = tmp_path / "unpriced.csv"
    assert cli.main(["unpriced", "-o", str(export)]) == 0
    rows = list(csv.DictReader(open(export)))
    assert {r["name"] for r in rows} == {"Extended Cuff Exam Gloves - Medium", "Gauze Sponges 4 x 4 in",
                                         "Monoject 12 mL Oral/Enteral Fluids Syringe"}
    rows[0]["unit_value_usd"] = "1.25"
    rows.append({"supply_id": "424242", "unit_value_usd": "1"})
    with open(export, "w", newline="") as f:
        w = csv.DictWriter(f, prices.EXPORT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    assert cli.main(["prices", "--import-csv", str(export)]) == 1   # unknown supply id is a problem
    out = capsys.readouterr().out
    assert "supply_id 424242 does not exist" in out and "set 1" in out
    assert supply_price(loaded, rows[0]["name"]) == pytest.approx(1.25)


def test_stub_matches_f25_schema_field_names():
    """The stub answers with the field names of f25's EnrichedItem."""
    server, url = pricing_api_stub.start({"x": 1.0})
    try:
        import httpx
        r = httpx.post(url + "/v1/enrich/batch", json={"items": [{"external_id": "1", "name": "x"}]},
                       headers={"Authorization": "Bearer test-key"})
    finally:
        server.shutdown()
    item = r.json()["items"][0]
    assert {"external_id", "status", "source", "unit_price", "currency", "needs_review", "confidence",
            "matched_item", "price_url"} <= set(item)
    assert json.loads(r.text)["usage"]["cost_usd"] == 0.0
