"""check --aliases: unmapped spellings with suggested alias rows."""
from b4p import aliasgap
from b4p.sync import run_sync


def _row(i, mfr, box="PPE", sub="Gloves"):
    return (i, {"Name": f"Item {i}", "Quantity": "1", "Manufacturer Name": mfr, "Category": box,
                "Subcategory": sub, "Box Number": "1", "Pallet Number": "1", "Image URL": "",
                  "Date of Expiration": "2030-01-01"})


def test_alias_gaps_suggest_rows(conn):
    rows = [_row(2, "Hollister"), _row(3, "Hollister"), _row(4, "Hollister"), _row(5, "Hollister Incorporated"),
            _row(6, "Medline"), _row(7, "Zebra Labs", sub="Totally Unknown Thing")]
    run_sync(conn, rows, [])
    gaps = aliasgap.alias_gaps(conn)
    assert ("Hollister Incorporated", 1, "Hollister") in gaps["manufacturer"]
    assert not any(g[0] == "Medline" for g in gaps["manufacturer"])
    assert ("PPE", 6, "PH/PPE") in gaps["box_type"]
    sql = "\n".join(aliasgap.suggestions(gaps))
    assert "INSERT INTO manufacturer_alias" in sql and "'hollister incorporated'" in sql
    assert "INSERT INTO box_type_alias" in sql
    # suggested statements are valid SQL
    for stmt in aliasgap.suggestions(gaps):
        conn.execute(stmt)


def test_old_schema_gives_a_hint_not_a_crash(conn, capsys):
    from b4p import cli
    conn.execute("ALTER TABLE manufacturer_alias RENAME TO manufacturer_alias_x")
    conn.commit()
    try:
        assert cli.main(["check", "--aliases"]) == 2
        assert "b4p init" in capsys.readouterr().out
        assert cli.main(["doctor"]) == 1
        assert "manufacturer_alias" in capsys.readouterr().out
    finally:
        conn.execute("ALTER TABLE manufacturer_alias_x RENAME TO manufacturer_alias")
        conn.commit()


def test_price_and_quantity_ambiguity_warns():
    from b4p import normalize as N
    r = N.read_inventory_row({"Name": "x", "Quantity": "5-10", "Unit Value": "1.234"})
    text = " ".join(r["problems"])
    assert "Quantity" in text and "ambiguous" in text and "price" in text
    assert not N.read_inventory_row({"Name": "x", "Unit Value": "12.50"})["problems"]


def test_co_brand_spellings_are_not_suggested_as_inserts():
    gaps = {"category": [], "box_type": [],
            "manufacturer": [("Teleflex / ARROW", 2, "Teleflex"), ("Arrow (Teleflex)", 1, "Teleflex"),
                             ("Teleflex Medical", 1, "Teleflex")]}
    sql = "\n".join(aliasgap.suggestions(gaps))
    assert "teleflex medical" in sql and "arrow" not in sql
