"""Append Stripe payments to the SR Event Sales spreadsheet."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

HEADERS = [
    "Paid at",
    "Programme",
    "Payment plan",
    "Installment",
    "Amount",
    "Currency",
    "Customer name",
    "Customer email",
    "Stripe product name",
    "Stripe product id",
    "Payment Intent id",
    "Charge id",
    "Checkout Session id",
    "Invoice id",
    "Subscription id",
    "Status",
    "Stripe event id",
]
PLACEHOLDER_TAB = "SR Event Sales"
ROTTERDAM_TAB = "MMI2610ROT"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def configured() -> bool:
    return bool(_sheet_id() and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())


def _sheet_id() -> str:
    return os.getenv("SALES_SHEET_ID", "").strip()


def _api():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _tabs(api) -> dict[str, int]:
    meta = api.spreadsheets().get(spreadsheetId=_sheet_id()).execute()
    return {
        sheet["properties"]["title"]: sheet["properties"]["sheetId"]
        for sheet in meta.get("sheets") or []
    }


def _values(api, tab: str) -> list[list[str]]:
    result = (
        api.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(), range=f"'{tab}'!A:Q")
        .execute()
    )
    return result.get("values") or []


def _write_headers(api, tab: str) -> None:
    api.spreadsheets().values().update(
        spreadsheetId=_sheet_id(),
        range=f"'{tab}'!A1:Q1",
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()


def ensure_tab(tab: str) -> None:
    api = _api()
    titles = _tabs(api)
    if tab not in titles and PLACEHOLDER_TAB in titles and tab == ROTTERDAM_TAB:
        api.spreadsheets().batchUpdate(
            spreadsheetId=_sheet_id(),
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": titles[PLACEHOLDER_TAB], "title": tab},
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()
    elif tab not in titles:
        api.spreadsheets().batchUpdate(
            spreadsheetId=_sheet_id(),
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
    rows = _values(api, tab)
    if not rows or rows[0][:1] != [HEADERS[0]]:
        _write_headers(api, tab)


def _find(rows: list[list[str]], index: int, value: str) -> int | None:
    if not value:
        return None
    for offset, row in enumerate(rows[1:], start=2):
        cell = row[index] if index < len(row) else ""
        if cell == value:
            return offset
    return None


def record_payment(tab: str, row: list[str]) -> str:
    """Append a payment on the event tab. Return written or duplicate."""
    api = _api()
    ensure_tab(tab)
    rows = _values(api, tab)
    if (
        _find(rows, 16, row[16])
        or _find(rows, 10, row[10])
        or _find(rows, 13, row[13])
    ):
        return "duplicate"
    if row[2] == "installment" and row[14]:
        already = sum(1 for existing in rows[1:] if len(existing) > 14 and existing[14] == row[14])
        row[3] = str(already + 1)
    api.spreadsheets().values().append(
        spreadsheetId=_sheet_id(),
        range=f"'{tab}'!A:Q",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return "written"


def mark_refunded(payment_intent: str) -> str:
    if not payment_intent:
        return "no_payment_intent"
    api = _api()
    meta = api.spreadsheets().get(spreadsheetId=_sheet_id()).execute()
    for sheet in meta.get("sheets") or []:
        tab = sheet["properties"]["title"]
        found = _find(_values(api, tab), 10, payment_intent)
        if not found:
            continue
        api.spreadsheets().values().update(
            spreadsheetId=_sheet_id(),
            range=f"'{tab}'!P{found}",
            valueInputOption="RAW",
            body={"values": [["refunded"]]},
        ).execute()
        return "refunded"
    return "not_found"


def paid_at(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def amount_major(cents: int, currency: str) -> str:
    zero = {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
        "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
    if (currency or "").lower() in zero:
        return str(int(cents))
    return f"{int(cents) / 100:.2f}"
