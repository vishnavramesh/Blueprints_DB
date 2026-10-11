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
