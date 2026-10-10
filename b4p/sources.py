"""Fetch the live data.

INVENTORY comes from the Google Sheet the inventorying app writes to, tab
INVENTORY_WORKSHEET_GID (default 1019735220, "Final Inventorying"). Sources:
  1. the sheet via a Google service account (read-only), if
     GOOGLE_SERVICE_ACCOUNT_JSON (key JSON text) or GOOGLE_APPLICATION_CREDENTIALS
     (path to the key file) is set and the account is shared on the sheet;
  2. the sheet's public CSV export (only works if the sheet is link-shared).
If neither works the sync fails (and records the error). There is deliberately
no fallback to the external dashboard's /api/supplies: once that dashboard runs
on this database it serves our own data back, which would freeze the sync.

REQUESTS: after the dashboards switched to this database, the database is the
source of truth for requests. Importing old requests is OFF unless you opt in:
  * REQUESTS_URL=<url>                 -> import from that JSON feed, or
  * ORG_REQUESTS_WORKSHEET_TITLE=<tab> -> import from that tab (service account,
                                          spreadsheet REQUESTS_SHEET_ID).
Imports are insert-only (see sync.py).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os

import httpx

from b4p.normalize import sanitize_row

log = logging.getLogger("b4p.sources")

SHEET_ID = "1OiT1QBBnlazfRCv0MgwE_LXvIVJNAVQha1Ez7M4Vc88"
# 'Final Inventorying'. (The internal dashboard used to read gid 384285232,
# '[AVAILABLE] Final Inventorying', which holds a different set of rows.)
DEFAULT_TAB_GID = "1019735220"
TIMEOUT = 120


class FeedError(RuntimeError):
    pass


def _get(url: str) -> httpx.Response:
    try:
        r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        return r
    except httpx.HTTPError as e:
        raise FeedError(f"{url.split('?')[0]}: {type(e).__name__} {str(e)[:120]}") from None


def _clean_headers(row: dict) -> dict:
    """Drop blank / pandas 'Unnamed_N' columns; keep every cell as text."""
    row = sanitize_row({k: v for k, v in row.items() if k is not None})
    return {k.strip(): v for k, v in row.items() if k.strip() and not k.startswith("Unnamed_")}


def parse_sheet_csv(text: str) -> list[tuple[int, dict]]:
    """CSV text -> [(sheet_row, {header: cell})]; header is row 1."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Name" not in [h.strip() for h in reader.fieldnames]:
        raise FeedError("sheet CSV has no 'Name' header (not public / not CSV?)")
    rows = []
    for i, row in enumerate(reader, start=2):
        row.pop(None, None)  # overflow cells
        rows.append((i, _clean_headers(row)))
    return rows


def parse_supplies_json(data: list[dict]) -> list[tuple[int, dict]]:
    """Old /api/supplies JSON snapshot -> [(sheet_row, {header: cell})].
    Only used for saved snapshots (tests); never as a live source."""
    if not isinstance(data, list):
        raise FeedError("supplies snapshot is not a list")
    rows = []
    for i, rec in enumerate(data, start=2):
        rec = dict(rec)
        sheet_row = int(rec.pop("_sheet_row", i))
        rows.append((sheet_row, _clean_headers(rec)))
    return rows


def values_to_rows(values: list[list[str]]) -> list[tuple[int, dict]]:
    """get_all_values() grid -> [(sheet_row, {header: cell})]; header is row 1."""
    if not values:
        return []
    header = [str(h).replace("\x00", "").strip() for h in values[0]]
    rows = []
    for i, cells in enumerate(values[1:], start=2):
        rec = {}
        for j, h in enumerate(header):
            if h and h not in rec:
                rec[h] = cells[j] if j < len(cells) else ""
        rows.append((i, sanitize_row(rec)))
    return rows


def _settings() -> dict:
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", SHEET_ID).strip()
    gid = os.environ.get("INVENTORY_WORKSHEET_GID", DEFAULT_TAB_GID).strip()
    return {
        "sheet_id": sheet_id,
        "gid": gid,
        "csv_url": os.environ.get(
            "SHEET_CSV_URL", f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"),
        "requests_url": os.environ.get("REQUESTS_URL", "").strip(),
        "requests_sheet_id": os.environ.get("REQUESTS_SHEET_ID", sheet_id).strip(),
        "requests_tab": os.environ.get("ORG_REQUESTS_WORKSHEET_TITLE", "").strip(),
    }


def _gspread_client():
    """Read-only gspread client, or None if no service-account key is configured."""
    info = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not info and not path:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    try:
        creds = (Credentials.from_service_account_info(json.loads(info), scopes=scopes) if info
                 else Credentials.from_service_account_file(path, scopes=scopes))
    except (ValueError, OSError, KeyError) as e:  # never echo the key itself
        raise FeedError(f"service-account key unusable ({type(e).__name__})") from None
    return gspread.authorize(creds)


def _read_worksheet(client, sheet_id: str, gid: str | None = None, title: str | None = None):
    import gspread
    try:
        book = client.open_by_key(sheet_id)
        ws = book.get_worksheet_by_id(int(gid)) if gid else book.worksheet(title)
        return ws.title, ws.get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        raise FeedError(f"worksheet {gid or title!r} not found") from None
    except gspread.exceptions.APIError as e:
        raise FeedError(f"Google API error {getattr(e, 'code', '')}: no access to the sheet?") from None
    except Exception as e:  # network etc.
        raise FeedError(f"Google Sheets: {type(e).__name__}: {str(e)[:150]}") from None


def fetch_inventory() -> tuple[str, list[tuple[int, dict]]]:
    """Returns (source description, rows): service account, else public CSV."""
    cfg = _settings()
    errors = []
    try:
        client = _gspread_client()
        if client is not None:
            title, values = _read_worksheet(client, cfg["sheet_id"], gid=cfg["gid"])
            return f"service-account:{title}", values_to_rows(values)
        errors.append("service account: no key configured")
    except FeedError as e:
        errors.append(f"service account: {e}")
    for e in errors:
        log.info("inventory: %s; trying the public CSV", e)
    try:
        return f"sheet-csv:gid={cfg['gid']}", parse_sheet_csv(_get(cfg["csv_url"]).text)
    except FeedError as e:
        errors.append(f"csv: {e}")
    raise FeedError("no inventory source reachable: " + " | ".join(errors))


def fetch_requests() -> tuple[str, list | None]:
    """Returns (source description, request rows), or ('off', None) when
    importing requests is not enabled (the default)."""
    cfg = _settings()
    if cfg["requests_tab"]:
        client = _gspread_client()
        if client is None:
            raise FeedError("ORG_REQUESTS_WORKSHEET_TITLE is set but no service-account key is configured")
        title, values = _read_worksheet(client, cfg["requests_sheet_id"], title=cfg["requests_tab"])
        if not (values and values[0] and values[0][0].strip() == "Request ID"):
            raise FeedError(f"tab {title!r} does not start with a 'Request ID' header")
        return f"service-account:{title}", [r for _, r in values_to_rows(values) if any(v.strip() for v in r.values())]
    if cfg["requests_url"]:
        try:
            data = _get(cfg["requests_url"]).json()
        except ValueError:
            raise FeedError("requests feed is not JSON") from None
        if not isinstance(data, list):
            raise FeedError("requests feed did not return a list")
        return "requests-url", data
    return "off", None
