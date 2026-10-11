

def test_duplicate_headers_first_wins_in_both_paths_and_bom_is_ignored():
    from b4p import sources
    csv_text = "﻿Name,Quantity,Name\nfirst,3,second\n"
    grid = [["Name", "Quantity", "Name"], ["first", "3", "second"]]
    warns: list[str] = []
    a = sources.parse_sheet_csv(csv_text, warns)
    b = sources.values_to_rows(grid)
    assert a == b and a[0][1]["Name"] == "first"
    assert warns and "repeated" in warns[0]


def test_fetch_inventory_reports_repeated_headers(monkeypatch):
    from b4p import sources
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    class R:
        text = "Name,Quantity,Name\nfirst,3,second\n"
    monkeypatch.setattr(sources, "_get", lambda url: R)
    warns: list[str] = []
    src, rows = sources.fetch_inventory(warns)
    assert rows[0][1]["Name"] == "first" and any("repeated" in w for w in warns)
