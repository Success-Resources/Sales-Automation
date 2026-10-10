"""Write Stripe payments to the SR Event Sales spreadsheet, one block per programme."""

from __future__ import annotations

import json
import os
import re
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
    "Phone number",
    "Stripe product name",
    "Stripe product id",
    "Payment ID",
    "Invoice id",
    "Status",
]
_PAYMENT_ID = 11
_INVOICE_ID = 12
_STATUS = 13
_COLUMNS = len(HEADERS)
_GAP = 3
PLACEHOLDER_TAB = "SR Event Sales"
ROTTERDAM_TAB = "MMI2610ROT"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# title, keyword, exact short name, fill, font
_SECTIONS = (
    ("Speed Wealth Book", "speedwealth", "swb", "D9EAD3", "000000"),
    ("Never Work Again", "neverworkagain", "nwa", "D9D2E9", "000000"),
    ("GBI", "guerrillabusiness", "gbi", "274E13", "FFFFFF"),
    ("TTT", "trainthetrainer", "ttt", "FF9900", "000000"),
    ("EWC", "enlightenedwarrior", "ewc", "990000", "FFFFFF"),
    ("QL", "quantumleap", "ql", "D9D9D9", "000000"),
)
_TITLES = {title for title, *_rest in _SECTIONS} | {"Refunded", "Other"}
_REFUNDED_FILL = "F4CCCC"


def _rgb(value: str) -> dict:
    return {
        "red": int(value[0:2], 16) / 255,
        "green": int(value[2:4], 16) / 255,
        "blue": int(value[4:6], 16) / 255,
    }


_BLACK = _rgb("000000")
_WHITE = _rgb("FFFFFF")


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
        .get(spreadsheetId=_sheet_id(), range=f"'{tab}'!A:N")
        .execute()
    )
    return result.get("values") or []


def _pad(row: list) -> list[str]:
    cells = [str(cell) for cell in row[:_COLUMNS]]
    return cells + [""] * (_COLUMNS - len(cells))


def _is_header(row: list[str]) -> bool:
    return row[:_COLUMNS] == HEADERS


def _is_blank(row: list[str]) -> bool:
    return not any(cell.strip() for cell in row)


def _is_title(row: list[str]) -> bool:
    return row[0].strip() in _TITLES and not any(cell.strip() for cell in row[1:])


def _payment_rows(rows: list[list]) -> list[list[str]]:
    payments = []
    for raw in rows:
        row = _pad(raw)
        if _is_blank(row) or _is_header(row) or _is_title(row):
            continue
        payments.append(row)
    return payments


def _programme_key(programme: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (programme or "").lower())


def _section_for(programme: str) -> int | None:
    key = _programme_key(programme)
    if not key:
        return None
    for index, (_title, word, alias, _fill, _font) in enumerate(_SECTIONS):
        if word in key or key == alias:
            return index
    return None


def _is_refunded(row: list[str]) -> bool:
    return row[_STATUS].strip().lower() == "refunded"


def _layout(payments: list[list[str]]) -> tuple[list[list[str]], list[tuple[int, int, str, str]]]:
    grouped: list[list[list[str]]] = [[] for _ in _SECTIONS]
    refunded: list[list[str]] = []
    other: list[list[str]] = []
    for row in payments:
        if _is_refunded(row):
            refunded.append(row)
            continue
        section = _section_for(row[1])
        if section is None:
            other.append(row)
        else:
            grouped[section].append(row)

    sections = list(_SECTIONS) + [("Refunded", "", "", _REFUNDED_FILL, "000000")]
    buckets = grouped + [refunded]
    if other:
        sections = sections + [("Other", "", "", "FFFFFF", "000000")]
        buckets = buckets + [other]

    values: list[list[str]] = []
    blocks: list[tuple[int, int, str, str]] = []
    for index, ((title, _word, _alias, fill, font), bucket) in enumerate(zip(sections, buckets)):
        if index:
            values.extend([_pad([]) for _ in range(_GAP)])
        start = len(values)
        values.append(_pad([title]))
        values.append(list(HEADERS))
        values.extend(bucket)
        blocks.append((start, len(values), fill, font))
    return values, blocks


def _paint(sheet_id: int, height: int, blocks: list[tuple[int, int, str, str]]) -> list[dict]:
    clear_until = max(height + 15, 80)
    span = {
        "sheetId": sheet_id,
        "startRowIndex": 0,
        "endRowIndex": clear_until,
        "startColumnIndex": 0,
        "endColumnIndex": _COLUMNS,
    }
    requests: list[dict] = [
        {"unmergeCells": {"range": span}},
        {
            "repeatCell": {
                "range": span,
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _WHITE,
                        "horizontalAlignment": "LEFT",
                        "textFormat": {"foregroundColor": _BLACK, "bold": False, "fontSize": 10},
                    }
                },
                "fields": (
                    "userEnteredFormat.backgroundColor,"
                    "userEnteredFormat.horizontalAlignment,"
                    "userEnteredFormat.textFormat.foregroundColor,"
                    "userEnteredFormat.textFormat.bold,"
                    "userEnteredFormat.textFormat.fontSize"
                ),
            }
        },
    ]
    for start, end, fill, font in blocks:
        font_color = _rgb(font)
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start,
                        "endRowIndex": end,
                        "startColumnIndex": 0,
                        "endColumnIndex": _COLUMNS,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(fill),
                            "textFormat": {"foregroundColor": font_color, "bold": False, "fontSize": 10},
                        }
                    },
                    "fields": (
                        "userEnteredFormat.backgroundColor,"
                        "userEnteredFormat.textFormat.foregroundColor,"
                        "userEnteredFormat.textFormat.bold,"
                        "userEnteredFormat.textFormat.fontSize"
                    ),
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start,
                        "endRowIndex": start + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": _COLUMNS,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "CENTER",
                            "textFormat": {"foregroundColor": font_color, "bold": True, "fontSize": 14},
                        }
                    },
                    "fields": (
                        "userEnteredFormat.horizontalAlignment,"
                        "userEnteredFormat.textFormat.foregroundColor,"
                        "userEnteredFormat.textFormat.bold,"
                        "userEnteredFormat.textFormat.fontSize"
                    ),
                }
            }
        )
        requests.append(
            {
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start,
                        "endRowIndex": start + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": _COLUMNS,
                    },
                    "mergeType": "MERGE_ALL",
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start + 1,
                        "endRowIndex": start + 2,
                        "startColumnIndex": 0,
                        "endColumnIndex": _COLUMNS,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"foregroundColor": font_color, "bold": True, "fontSize": 10},
                        }
                    },
                    "fields": (
                        "userEnteredFormat.textFormat.foregroundColor,"
                        "userEnteredFormat.textFormat.bold,"
                        "userEnteredFormat.textFormat.fontSize"
                    ),
                }
            }
        )
    return requests


def _ensure_tab_exists(api, tab: str) -> None:
    titles = _tabs(api)
    if tab in titles:
        return
    if PLACEHOLDER_TAB in titles and tab == ROTTERDAM_TAB:
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
        return
    api.spreadsheets().batchUpdate(
        spreadsheetId=_sheet_id(),
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()


def organize_tab(api, tab: str, payments: list[list[str]] | None = None) -> None:
    """Rebuild one event tab: a colored block per programme, three blank rows between blocks."""
    sheet_id = _tabs(api)[tab]
    if payments is None:
        payments = _payment_rows(_values(api, tab))
    values, blocks = _layout(payments)
    api.spreadsheets().values().clear(
        spreadsheetId=_sheet_id(),
        range=f"'{tab}'!A:Q",
    ).execute()
    api.spreadsheets().values().update(
        spreadsheetId=_sheet_id(),
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()
    api.spreadsheets().batchUpdate(
        spreadsheetId=_sheet_id(),
        body={"requests": _paint(sheet_id, len(values), blocks)},
    ).execute()


def ensure_tab(tab: str) -> None:
    api = _api()
    _ensure_tab_exists(api, tab)
    organize_tab(api, tab)


def _duplicate(payments: list[list[str]], row: list[str]) -> bool:
    payment_id = row[_PAYMENT_ID].strip()
    invoice_id = row[_INVOICE_ID].strip()
    for existing in payments:
        if payment_id and existing[_PAYMENT_ID].strip() == payment_id:
            return True
        if invoice_id and existing[_INVOICE_ID].strip() == invoice_id:
            return True
    return False


def record_payment(tab: str, row: list[str], subscription_id: str = "") -> str:
    """Insert a payment into its programme block. Return written or duplicate."""
    api = _api()
    _ensure_tab_exists(api, tab)
    payments = _payment_rows(_values(api, tab))
    row = _pad(row)
    if _duplicate(payments, row):
        organize_tab(api, tab, payments)
        return "duplicate"
    if row[2] == "installment":
        email = row[7]
        programme = row[1]
        already = sum(
            1
            for existing in payments
            if existing[1] == programme
            and existing[2] == "installment"
            and email
            and existing[7] == email
        )
        if subscription_id or email:
            row[3] = str(already + 1)
    payments.append(row)
    organize_tab(api, tab, payments)
    return "written"


def mark_refunded(payment_intent: str) -> str:
    if not payment_intent:
        return "no_payment_intent"
    api = _api()
    for tab in _tabs(api):
        payments = _payment_rows(_values(api, tab))
        for index, existing in enumerate(payments):
            if existing[_PAYMENT_ID] == payment_intent:
                already = _is_refunded(existing)
                existing[_STATUS] = "refunded"
                if not already:
                    payments.append(payments.pop(index))
                organize_tab(api, tab, payments)
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
