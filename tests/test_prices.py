"""`python -m b4p prices` / `unpriced`: matching, provenance, manual prices, API adapter."""
import csv
import io

import pytest

import pricing_api_stub
from b4p import cli, prices
from b4p.sync import load_rules, run_sync

URL_A = "https://drive.google.com/file/d/A/view"


def sheet_row(name, mfr="", qty="10", url="", sub="Needles & Syringes"):
    return {"Name": name, "Manufacturer Name": mfr, "Quantity": qty, "Image URL": url,
            "Date of Expiration": "", "Pallet Number": "1", "Box Number": "2", "Category": "ST/N",
            "Subcategory": sub, "General": "", "Review": "No"}


INVENTORY = [
    sheet_row("BD 10ml Syringe Luer-Lok Tip", "Becton, Dickinson and Company", "100"),
    sheet_row("Monoject 12 mL Oral/Enteral Fluids Syringe", "Monoject", "20"),
    sheet_row("BD 5ml Syringe Luer-Lok Tip", "BD", "30"),                    # only a 10 ml price exists
    sheet_row("Extended Cuff Exam Gloves - Medium", "Medline", "40", sub="Gloves"),  # only Large priced
    sheet_row("Mystery widget", "", "5", url=URL_A),                          # priced via its photo
    sheet_row("Gauze Sponges 4 x 4 in", "Medline", "50", sub="Wound Care & Bandages"),
]

PRICED = [
    sheet_row("BD 10ml Syringe Luer-Lok™ Tip", "BD") | {"Per-Unit Price": "$0.40"},
    sheet_row("bd 10ml syringe luer-lok tip", "Becton Dickinson") | {"Per-Unit Price": "0.60"},
    sheet_row("Monoject Oral/Enteral Syringe 12 mL", "MONOJECT") | {"Per-Unit Price": "0.48"},
    sheet_row("Extended Cuff Exam Gloves - Large", "Medline") | {"Per-Unit Price": "0.43"},
    sheet_row("something else entirely", "", url=URL_A) | {"Per-Unit Price": "7"},
    sheet_row("Gauze Sponges 4 x 4 in", "Medline") | {"Per-Unit Price": "NEED HUMAN OVERSIGHT"},
    sheet_row("shoe covers", "") | {"Per-Unit Price": "119900"},
]


@pytest.fixture
def loaded(conn):
    run_sync(conn, list(enumerate(INVENTORY, start=2)), [], source="test")
    return conn


def supply_price(conn, name):
    return conn.execute("SELECT unit_value_usd::FLOAT8 FROM supply WHERE name = %s", (name,)).fetchone()[0]


def run_sheet(conn, rows=PRICED, **kw):
    with conn.cursor() as cur:
        priced, skipped = prices.read_priced_rows(list(enumerate(rows, start=2)),
                                                  load_rules(cur)["manufacturer"])
        matches = prices.match_sheet(cur, priced, **kw)
        stats = prices.apply_prices(cur, matches, prices.SHEET)
    conn.commit()
    return matches, skipped, stats


def test_norm_name_and_signature():
    assert prices.norm_name(" BD Insyte™ Autoguard™  22 GA ") == "bd insyte autoguard 22 ga"
    assert prices.norm_name("{sterile wipe}") == "sterile wipe"
    sig = prices.signature
    assert sig("bd 10ml syringe") != sig("bd 5ml syringe")
    assert sig("gloves medium") != sig("gloves large")
    assert sig("tube 08fr") == sig("tube 8fr")
    assert sig("a 5,0 cm") == sig("a 5.0 cm")


def test_sheet_prices_match_conservatively(loaded):
    matches, skipped, stats = run_sheet(loaded)
    assert skipped == {"no price": 1, "price above 10000": 1}
    # exact name + manufacturer (aliases resolved), median of the two rows
    assert supply_price(loaded, "BD 10ml Syringe Luer-Lok Tip") == pytest.approx(0.50)
    # fuzzy: same words reordered, same numbers
    assert supply_price(loaded, "Monoject 12 mL Oral/Enteral Fluids Syringe") == pytest.approx(0.48)
    # numbers / sizes must match
    assert supply_price(loaded, "BD 5ml Syringe Luer-Lok Tip") is None
    assert supply_price(loaded, "Extended Cuff Exam Gloves - Medium") is None
    # same photo
    assert supply_price(loaded, "Mystery widget") == pytest.approx(7)
    assert supply_price(loaded, "Gauze Sponges 4 x 4 in") is None
    how = {r[0]: r[1:] for r in loaded.execute(
        "SELECT s.name, p.source, p.matched_by, p.score FROM supply_price p JOIN supply s USING (supply_id)")}
    assert how["BD 10ml Syringe Luer-Lok Tip"] == ("sheet", "name+manufacturer", 1.0)
    assert how["Mystery widget"][1] == "image_url"
    assert how["Monoject 12 mL Oral/Enteral Fluids Syringe"][1] == "trgm"
    assert stats == {"set": 3, "kept_manual": 0}


def test_threshold_is_respected(loaded):
    run_sheet(loaded, threshold=0.99)
    assert supply_price(loaded, "Monoject 12 mL Oral/Enteral Fluids Syringe") is None


def test_dashboards_show_prices(loaded):
    run_sheet(loaded)
    row = loaded.execute("SELECT quantity, unit_value, total_value FROM dash_items "
                         "WHERE name = 'BD 10ml Syringe Luer-Lok Tip'").fetchone()
    assert row == (100, 0.5, 50.0)
    assert loaded.execute("SELECT total_value::FLOAT8 FROM v_kpis").fetchone()[0] == pytest.approx(50 + 20 * 0.48 + 5 * 7)


def test_manual_price_is_never_overwritten(loaded):
    loaded.execute("UPDATE supply SET unit_value_usd = 9.99 WHERE name = 'BD 10ml Syringe Luer-Lok Tip'")
    loaded.commit()
    _, _, stats = run_sheet(loaded)
    assert supply_price(loaded, "BD 10ml Syringe Luer-Lok Tip") == pytest.approx(9.99)
    assert stats["kept_manual"] == 1


def test_automatic_price_follows_the_sheet(loaded):
    run_sheet(loaded)
    changed = [dict(r) for r in PRICED]
    changed[2]["Per-Unit Price"] = "0.55"
    run_sheet(loaded, changed)
    assert supply_price(loaded, "Monoject 12 mL Oral/Enteral Fluids Syringe") == pytest.approx(0.55)


def test_unpriced_export_and_manual_import(loaded):
    run_sheet(loaded)
    out = io.StringIO()
    with loaded.cursor() as cur:
        n = prices.export_unpriced(cur, out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert n == len(rows) == 3
    assert list(rows[0]) == prices.EXPORT_COLUMNS
    assert rows[0]["name"] == "Gauze Sponges 4 x 4 in" and rows[0]["units"] == "50"  # most units first
    assert rows[0]["category"] == "Wound Care & Bandages"
    rows[0]["unit_value_usd"] = "$0.09"
    rows[1]["unit_value_usd"] = "abc"
    filled = io.StringIO()
    w = csv.DictWriter(filled, prices.EXPORT_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    matches, problems = prices.read_manual_csv(io.StringIO(filled.getvalue()))
    assert len(problems) == 1 and "abc" in problems[0]
    with loaded.cursor() as cur:
        prices.apply_prices(cur, matches, prices.MANUAL)
        # a later sheet run does not touch the manual price
        loaded.commit()
    assert supply_price(loaded, "Gauze Sponges 4 x 4 in") == pytest.approx(0.09)
    run_sheet(loaded, PRICED + [sheet_row("Gauze Sponges 4 x 4 in", "Medline") | {"Per-Unit Price": "1"}])
    assert supply_price(loaded, "Gauze Sponges 4 x 4 in") == pytest.approx(0.09)
    assert loaded.execute("SELECT count(*) FROM v_unpriced").fetchone()[0] == 2


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
                            "Gauze Sponges 4 x 4 in"]
    assert all(len(b["items"]) <= 2 for b in server.requests)
    assert stats["sent"] == 3 and stats["priced"] == 1 and stats["rejected"] == 2 and stats["error"] is None
    assert applied == {"set": 1, "kept_manual": 0}
    assert supply_price(loaded, "BD 5ml Syringe Luer-Lok Tip") == pytest.approx(0.3)
    assert supply_price(loaded, "Extended Cuff Exam Gloves - Medium") is None  # needs_review -> not used
    assert supply_price(loaded, "Mystery widget") == pytest.approx(7)          # already priced, not sent
    src = loaded.execute("SELECT source, matched_by, score FROM supply_price p JOIN supply s USING (supply_id) "
                         "WHERE s.name = 'BD 5ml Syringe Luer-Lok Tip'").fetchone()
    assert src == ("f25-api", "f25:cache", pytest.approx(0.9))


def test_enrich_via_api_errors_are_reported(loaded, api):
    _, url = api
    with loaded.cursor() as cur:
        matches, stats = prices.enrich_via_api(cur, url, "wrong-key")
    assert matches == {} and "401" in stats["error"]
    with loaded.cursor() as cur:  # batch too large -> 413, stop
        matches, stats = prices.enrich_via_api(cur, url, "test-key", batch_size=5)
    assert matches == {} and "413" in stats["error"]


def test_cli_prices_and_unpriced(loaded, api, tmp_path, monkeypatch, capsys):
    _, url = api
    priced_csv = tmp_path / "priced.csv"
    with open(priced_csv, "w", newline="") as f:
        w = csv.DictWriter(f, list(PRICED[0]))
        w.writeheader()
        w.writerows(PRICED)
    assert cli.main(["prices", "--csv", str(priced_csv), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "matched 3 supplies" in out and "dry run" in out
    assert supply_price(loaded, "Mystery widget") is None
    assert cli.main(["prices", "--enrich-url", url, "--skip-sheet"]) == 2  # no key configured
    monkeypatch.setenv("PRICING_API_KEY", "test-key")
    assert cli.main(["prices", "--csv", str(priced_csv), "--enrich-url", url, "--batch-size", "2"]) == 0
    out = capsys.readouterr().out
    assert "f25-api: sent 3, priced 1" in out and "priced: 4/6 supplies" in out
    export = tmp_path / "unpriced.csv"
    assert cli.main(["unpriced", "-o", str(export)]) == 0
    rows = list(csv.DictReader(open(export)))
    assert {r["name"] for r in rows} == {"Extended Cuff Exam Gloves - Medium", "Gauze Sponges 4 x 4 in"}
    rows[0]["unit_value_usd"] = "1.25"
    with open(export, "w", newline="") as f:
        w = csv.DictWriter(f, prices.EXPORT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    assert cli.main(["prices", "--import-csv", str(export)]) == 0
    assert "manual: 1 prices set" in capsys.readouterr().out
    assert supply_price(loaded, rows[0]["name"]) == pytest.approx(1.25)
