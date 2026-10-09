"""Fetch the live data feeds.

Inventory rows come from the Google Sheet the inventorying app writes to.
Sources, in order of preference:
  1. the sheet itself via a Google service account (read-only), if
     GOOGLE_SERVICE_ACCOUNT_JSON (key JSON text) or GOOGLE_APPLICATION_CREDENTIALS
     (path to the key file) is set and the account has been shared on the sheet;
  2. the sheet's public CSV export (only works if the sheet is link-shared);
  3. the external dashboard's Render feed, which serves the same tab.
All three must point at the SAME tab (INVENTORY_WORKSHEET_GID), otherwise the
sync would see rows appear/disappear depending on which source answered.

Requests come from the requests tab (service account) or the Render /requests
feed, and are only ever INSERTED into the database (see sync.py).
"""
from __future__ import annotations

import csv
import io
import json
import os

import logging

import httpx

log = logging.getLogger("b4p.sources")

SHEET_ID = "1OiT1QBBnlazfRCv0MgwE_LXvIVJNAVQha1Ez7M4Vc88"
# 'Final Inventorying' -- the tab the external dashboard (Render /api/supplies)
# serves. The internal dashboard used gid 384285232 ('[AVAILABLE] Final
# Inventorying'), a different, smaller set of rows.
RENDER_TAB_GID = "1019735220"
SUPPLIES_URL = "https://b4p-external-view-w26-backend.onrender.com/api/supplies"
REQUESTS_URL = "https://b4p-external-view-w26-backend.onrender.com/requests"
TIMEOUT = 120  # Render free tier can take ~1 min to wake up


class FeedError(RuntimeError):
    pass


def _get(url: str) -> httpx.Response:
    try:
        r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        return r
    except httpx.HTTPError as e:
        raise FeedError(f"{url}: {e}") from e


def _clean_headers(row: dict) -> dict:
    """Drop blank / pandas 'Unnamed_N' columns; keep every cell as text."""
    return {str(k).strip(): "" if v is None else str(v)
            for k, v in row.items()
            if k is not None and str(k).strip() and not str(k).startswith("Unnamed_")}


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
    """/api/supplies payload -> [(sheet_row, {header: cell})]."""
    if not isinstance(data, list):
        raise FeedError("/api/supplies did not return a list")
    rows = []
    for i, rec in enumerate(data, start=2):
        rec = dict(rec)
        sheet_row = int(rec.pop("_sheet_row", i))
        rows.append((sheet_row, _clean_headers(rec)))
    return rows


def _settings() -> dict:
    gid = os.environ.get("INVENTORY_WORKSHEET_GID", RENDER_TAB_GID).strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", SHEET_ID).strip()
    return {
        "sheet_id": sheet_id,
        "gid": gid,
        "csv_url": os.environ.get(
            "SHEET_CSV_URL", f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"),
        # the Render feed only serves 'Final Inventorying'; don't mix tabs
        "supplies_url": os.environ.get("SUPPLIES_URL", SUPPLIES_URL if gid == RENDER_TAB_GID else ""),
        "requests_sheet_id": os.environ.get("REQUESTS_SHEET_ID", sheet_id).strip(),
        "requests_tab": os.environ.get("ORG_REQUESTS_WORKSHEET_TITLE", "Requests").strip(),
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
    except (ValueError, OSError) as e:  # never echo the key itself
        raise FeedError(f"service-account key unusable ({type(e).__name__})") from None
    return gspread.authorize(creds)


def values_to_rows(values: list[list[str]]) -> list[tuple[int, dict]]:
    """get_all_values() grid -> [(sheet_row, {header: cell})]; header is row 1."""
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    rows = []
    for i, cells in enumerate(values[1:], start=2):
        rec = {}
        for j, h in enumerate(header):
            if h and h not in rec:
                rec[h] = cells[j] if j < len(cells) else ""
        rows.append((i, rec))
    return rows


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
    """Returns (source description, rows): service account -> public CSV -> Render feed."""
    cfg = _settings()
    errors = []
    try:
        client = _gspread_client()
        if client is not None:
            title, values = _read_worksheet(client, cfg["sheet_id"], gid=cfg["gid"])
            rows = values_to_rows(values)
            if not any("Name" in r for _, r in rows[:1]):
                raise FeedError(f"tab {title!r} has no 'Name' column")
            return f"service-account:{title}", rows
    except FeedError as e:
        errors.append(f"service account: {e}")
    for e in errors:
        log.warning("inventory: %s; falling back", e)
    if cfg["csv_url"]:
        try:
            return f"sheet-csv:gid={cfg['gid']}", parse_sheet_csv(_get(cfg["csv_url"]).text)
        except FeedError as e:
            errors.append(f"csv: {str(e)[:200]}")
    if cfg["supplies_url"]:
        try:
            return "render-api-supplies", parse_supplies_json(_get(cfg["supplies_url"]).json())
        except (FeedError, ValueError) as e:
            errors.append(f"render: {str(e)[:200]}")
    raise FeedError("no inventory source reachable: " + " | ".join(errors))


def fetch_requests() -> tuple[str, list[dict]]:
    """Returns (source description, request rows): requests tab -> Render /requests."""
    cfg = _settings()
    errors = []
    try:
        client = _gspread_client()
        if client is not None:
            title, values = _read_worksheet(client, cfg["requests_sheet_id"], title=cfg["requests_tab"])
            if values and values[0] and values[0][0].strip() == "Request ID":
                return (f"service-account:{title}",
                        [r for _, r in values_to_rows(values) if any(v.strip() for v in r.values())])
            raise FeedError(f"tab {title!r} does not start with a 'Request ID' header")
    except FeedError as e:
        errors.append(f"service account: {e}")
    for e in errors:
        log.warning("requests: %s; falling back to Render", e)
    try:
        data = _get(os.environ.get("REQUESTS_URL", REQUESTS_URL)).json()
    except (FeedError, ValueError) as e:
        raise FeedError(" | ".join(errors + [f"render: {e}"])) from None
    if not isinstance(data, list):
        raise FeedError("/requests did not return a list")
    return "render-requests", data
