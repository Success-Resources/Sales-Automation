"""Write Stripe payments to the SR Event Sales spreadsheet, one block per programme."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from google.oauth2 import service_account

# google-api-python-client builds a huge discovery client and was crossing
# Render's 512 MB limit on a single payment. These calls use the Sheets REST API.

HEADERS = [
    "Paid at",
    "Programme",
    "Programme price",
    "Payment plan",
    "Installment (remaining)",
    "Amount paid",
    "Currency",
    "Customer name",
    "Customer email",
    "Phone number",
    "Stripe product name",
    "Stripe product id",
    "Payment ID",
    "Status",
    "transferred to Quantum Leap",
]
_PROGRAMME = 1
_PRICE = 2
_PLAN = 3
_REMAINING = 4
_PAID = 5
_CURRENCY = 6
_NAME = 7
_EMAIL = 8
_PAYMENT_ID = 12
_STATUS = 13
_TRANSFERRED = 14
_COLUMNS = len(HEADERS)
_LAST_COLUMN = chr(ord("A") + _COLUMNS - 1)
_GAP = 3
PLACEHOLDER_TAB = "SR Event Sales"
SUMMARY_TAB = "Sales summary"
ROTTERDAM_TAB = "MMI2610ROT"
_SUMMARY_HEADERS = [
    "",
    "Selling price",
    "Number of sales",
    "Revenue",
    "On-site collection",
    "To be collected",
]
# label, keyword used to match payment rows, fill, font
_SUMMARY_PROGRAMMES = (
    ("Quantum Leap", "quantumleap", "D9D9D9", "000000"),
    ("Guerrilla Business Intensive", "guerrillabusiness", "274E13", "FFFFFF"),
    ("Never Work Again", "neverworkagain", "D9D2E9", "000000"),
    ("Enlightened Warrior Camp", "enlightenedwarrior", "990000", "FFFFFF"),
    ("Train the Trainer", "trainthetrainer", "FF9900", "000000"),
)
_TOTAL_FILL = "A64D79"
_TOTAL_FONT = "FFFFFF"
# Programme lines on the L2 preliminary report. Product rows, deposits, and pending sales are omitted.
_PRELIM_PROGRAMMES = (
    ("Quantum Leap", "quantumleap"),
    ("Train the Trainer", "trainthetrainer"),
    ("Enlightened Warrior Camp", "enlightenedwarrior"),
    ("Guerrilla Business Intensive", "guerrillabusiness"),
    ("Never Work Again", "neverworkagain"),
)
_PRELIM_END = 7
_SKIP_TABS = {PLACEHOLDER_TAB, "Reminders", SUMMARY_TAB}
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


_SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
log = logging.getLogger("sales")
_creds = None


class _AuthResponse:
    def __init__(self, status: int, data: bytes):
        self.status = status
        self.data = data


class _AuthRequest:
    def __call__(self, url, method="GET", body=None, headers=None, timeout=60, **kwargs):
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _AuthResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return _AuthResponse(exc.code, exc.read())


def _token() -> str:
    global _creds
    if _creds is None:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        _creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    if not _creds.valid:
        _creds.refresh(_AuthRequest())
    return _creds.token


def _call(method: str, url: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {_token()}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Google Sheets {method} {exc.code}: {detail}") from exc
    return json.loads(raw) if raw else {}


def _sheet_url(suffix: str = "") -> str:
    return f"{_SHEETS}/{urllib.parse.quote(_sheet_id(), safe='')}{suffix}"


def _tabs() -> dict[str, int]:
    meta = _call(
        "GET",
        _sheet_url("?fields=sheets(properties(title,sheetId,gridProperties(rowCount)))"),
    )
    return {
        sheet["properties"]["title"]: sheet["properties"]["sheetId"]
        for sheet in meta.get("sheets") or []
    }


def _tab_row_count(tab: str) -> tuple[int, int]:
    meta = _call(
        "GET",
        _sheet_url("?fields=sheets(properties(title,sheetId,gridProperties(rowCount)))"),
    )
    for sheet in meta.get("sheets") or []:
        props = sheet["properties"]
        if props["title"] != tab:
            continue
        rows = int((props.get("gridProperties") or {}).get("rowCount") or 1000)
        return props["sheetId"], rows
    raise KeyError(tab)


def _values(tab: str) -> list[list[str]]:
    target = urllib.parse.quote(f"'{tab}'!A:{_LAST_COLUMN}", safe="")
    result = _call("GET", _sheet_url(f"/values/{target}"))
    return result.get("values") or []


def _batch(requests: list[dict]) -> None:
    _call("POST", _sheet_url(":batchUpdate"), {"requests": requests})


def _pad(row: list) -> list[str]:
    """Fit a row to the current columns.

    Rows written before Programme price have Payment plan in column C.
    Rows written before Invoice id was removed have that id between Payment ID and Status.
    """
    cells = [str(cell) for cell in row]
    if len(cells) > 13 and cells[2].strip() in {"full", "installment", "Payment plan"}:
        invoice = cells[12].strip()
        status = cells[13].strip()
        if status in {"succeeded", "refunded", "failed", "Status"} or invoice in {"Invoice id"} or invoice.startswith("in_"):
            cells = cells[:12] + cells[13:]
    if len(cells) >= 3 and cells[2].strip() in {"full", "installment", "Payment plan"}:
        cells = cells[:2] + [""] + cells[2:]
    cells = cells[:_COLUMNS]
    return cells + [""] * (_COLUMNS - len(cells))


def _is_header(row: list[str]) -> bool:
    if row[:_COLUMNS] == HEADERS:
        return True
    # Older sheets used "Installment", and a merged title used to leave only "Paid at".
    first = row[0].strip()
    second = row[1].strip() if len(row) > 1 else ""
    return first == HEADERS[0] and second in {"", HEADERS[1]}


def _is_blank(row: list[str]) -> bool:
    return not any(cell.strip() for cell in row)


def _is_title(row: list[str]) -> bool:
    return row[0].strip() in _TITLES and not any(cell.strip() for cell in row[1:])


def _payment_rows(rows: list[list]) -> list[list[str]]:
    payments = []
    skip_summary = False
    for raw in rows:
        row = _pad(raw)
        if row[0].strip() == "Sales summary" or "L2 Preliminary Sales Report" in row[0]:
            skip_summary = True
            continue
        if skip_summary:
            if _is_title(row):
                skip_summary = False
            else:
                continue
        if _is_blank(row) or _is_header(row) or _is_title(row):
            continue
        payments.append(row)
    return payments


def _programme_key(programme: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (programme or "").lower())


def is_speed_wealth(programme: str) -> bool:
    """Speed Wealth Book is sold in full only."""
    key = _programme_key(programme)
    return "speedwealth" in key or key == "swb"


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


def _is_transferred(row: list[str]) -> bool:
    return len(row) > _TRANSFERRED and row[_TRANSFERRED].strip().casefold() == "yes"


_QL_SECTION = next(index for index, section in enumerate(_SECTIONS) if section[2] == "ql")


def _mark_ql_transfers(payments: list[list[str]]) -> None:
    """Flag source-programme rows whose payer also has a Quantum Leap payment.

    The payment id, amounts, and programme name stay as they are. The flag tells
    the layout to show that row under Quantum Leap, next to the Quantum Leap payment.
    """
    quantum_leap = [
        row
        for row in payments
        if not _is_refunded(row) and _matches_programme(row[_PROGRAMME], "quantumleap")
    ]
    for row in payments:
        while len(row) <= _TRANSFERRED:
            row.append("")
        row[_TRANSFERRED] = ""
        if _is_refunded(row) or not quantum_leap:
            continue
        if not any(_matches_programme(row[_PROGRAMME], keyword) for keyword in _UPGRADE_SOURCES):
            continue
        if (_money(row[_PAID]) or 0) <= 0.009:
            continue
        if any(_same_person(row, [existing]) for existing in quantum_leap):
            row[_TRANSFERRED] = "Yes"


def _pair_with_quantum_leap(rows: list[list[str]]) -> list[list[str]]:
    """Put each transferred programme row immediately before that person's Quantum Leap row."""
    native = [row for row in rows if not _is_transferred(row)]
    moved = [row for row in rows if _is_transferred(row)]
    if not moved:
        return rows
    used: set[int] = set()
    ordered: list[list[str]] = []
    for row in native:
        for source in moved:
            if id(source) in used or not _same_person(source, [row]):
                continue
            ordered.append(source)
            used.add(id(source))
        ordered.append(row)
    ordered.extend(source for source in moved if id(source) not in used)
    return ordered


def _layout(payments: list[list[str]], include_tail: bool = True) -> tuple[list[list[str]], list[tuple[int, int, str, str]], list[list[str]], list[list[str]]]:
    grouped: list[list[list[str]]] = [[] for _ in _SECTIONS]
    refunded: list[list[str]] = []
    other: list[list[str]] = []
    for row in payments:
        if _is_refunded(row):
            refunded.append(row)
            continue
        section = _QL_SECTION if _is_transferred(row) else _section_for(row[_PROGRAMME])
        if section is None:
            other.append(row)
        else:
            grouped[section].append(row)
    grouped[_QL_SECTION] = _pair_with_quantum_leap(grouped[_QL_SECTION])

    sections = list(_SECTIONS)
    buckets = grouped
    if include_tail:
        sections = sections + [("Refunded", "", "", _REFUNDED_FILL, "000000")]
        buckets = buckets + [refunded]
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
    return values, blocks, refunded, other


def _append_block(
    values: list[list[str]],
    blocks: list[tuple[int, int, str, str]],
    title: str,
    rows: list[list[str]],
    fill: str,
    font: str,
    gap: bool,
) -> None:
    if gap:
        values.extend(_pad([]) for _ in range(_GAP))
    start = len(values)
    values.append(_pad([title]))
    values.append(list(HEADERS))
    values.extend(rows)
    blocks.append((start, len(values), fill, font))


def _paint(sheet_id: int, height: int, blocks: list[tuple[int, int, str, str]]) -> list[dict]:
    clear_until = max(height + 15, 80)
    span = {
        "sheetId": sheet_id,
        "startRowIndex": 0,
        "endRowIndex": clear_until,
        "startColumnIndex": 0,
        "endColumnIndex": 17,
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
        if end > start + 2:
            for column in (_PRICE, _REMAINING, _PAID):
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": start + 2,
                                "endRowIndex": end,
                                "startColumnIndex": column,
                                "endColumnIndex": column + 1,
                            },
                            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                            "fields": "userEnteredFormat.horizontalAlignment",
                        }
                    }
                )
    return requests


def _ensure_tab_exists(tab: str) -> None:
    titles = _tabs()
    if tab in titles:
        return
    if PLACEHOLDER_TAB in titles and tab == ROTTERDAM_TAB:
        _batch(
            [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": titles[PLACEHOLDER_TAB], "title": tab},
                        "fields": "title",
                    }
                }
            ]
        )
        return
    _batch([{"addSheet": {"properties": {"title": tab}}}])


def _unmerge_tab(tab: str) -> int:
    """Drop every merged title before writing. A stale merge keeps only column A."""
    sheet_id, rows = _tab_row_count(tab)
    _batch(
        [
            {
                "unmergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": 17,
                    }
                }
            }
        ]
    )
    return sheet_id


def organize_tab(tab: str, payments: list[list[str]] | None = None) -> None:
    """Rebuild one event tab: a colored block per programme, three blank rows between blocks."""
    sheet_id = _unmerge_tab(tab)
    if payments is None:
        payments = _payment_rows(_values(tab))
    payments = [_pad(row) for row in payments]
    for row in payments:
        _normalize_payment(row)
    _mark_ql_transfers(payments)
    values, blocks, refunded, other = _layout(payments, include_tail=tab != ROTTERDAM_TAB)
    extra_paint: list[dict] = []
    if tab == ROTTERDAM_TAB:
        _delete_summary_tab()
        currencies = {row[_CURRENCY].strip().upper() for row in payments if row[_CURRENCY].strip()}
        reports = _programme_reports(payments)
        summary_rows, kinds = _render_summary(reports, _usd_per_unit(currencies))
        summary_at = len(values) + _GAP
        values.extend(_pad([]) for _ in range(_GAP))
        values.extend(_pad(row) for row in summary_rows)
        prelim_rows, prelim_kinds = preliminary_values(tab, reports)
        prelim_at = len(values) + _GAP
        values.extend(_pad([]) for _ in range(_GAP))
        values.extend(prelim_rows)
        values.extend(_pad([]) for _ in range(_GAP))
        _append_block(values, blocks, "Refunded", refunded, _REFUNDED_FILL, "000000", gap=False)
        if other:
            _append_block(values, blocks, "Other", other, "FFFFFF", "000000", gap=True)
        extra_paint = _paint_summary(sheet_id, summary_rows, kinds, summary_at) + _paint_preliminary(
            sheet_id, prelim_kinds, prelim_at
        )
    target = urllib.parse.quote(f"'{tab}'!A1", safe="")
    _call(
        "PUT",
        _sheet_url(f"/values/{target}?valueInputOption=RAW"),
        {"values": values},
    )
    clear_at = urllib.parse.quote(f"'{tab}'!A{len(values) + 1}:Q", safe="")
    _call("POST", _sheet_url(f"/values/{clear_at}:clear"), {})
    _batch(_paint(sheet_id, len(values), blocks) + extra_paint)


def prepare_tab(tab: str) -> None:
    """Create the event tab if it is missing. Do not rewrite payments on boot."""
    _ensure_tab_exists(tab)


def ensure_tab(tab: str) -> None:
    _ensure_tab_exists(tab)
    organize_tab(tab)


def ensure_log_tab(tab: str) -> None:
    """Create a plain tab. Do not rebuild it as a programme sheet."""
    _ensure_tab_exists(tab)


def sales_tabs() -> list[str]:
    return [name for name in _tabs() if name not in _SKIP_TABS]


def read_payments(tab: str) -> list[list[str]]:
    return _payment_rows(_values(tab))


def read_log(tab: str) -> list[list[str]]:
    return _values(tab)


def append_rows(tab: str, rows: list[list[str]]) -> None:
    target = urllib.parse.quote(f"'{tab}'!A1", safe="")
    _call(
        "POST",
        _sheet_url(f"/values/{target}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"),
        {"values": rows},
    )


def _duplicate(payments: list[list[str]], row: list[str]) -> bool:
    payment_id = row[_PAYMENT_ID].strip()
    if not payment_id:
        return False
    return any(existing[_PAYMENT_ID].strip() == payment_id for existing in payments)


def record_payment(tab: str, row: list[str], subscription_id: str = "") -> str:
    """Insert a payment into its programme block. Return written or duplicate."""
    _ensure_tab_exists(tab)
    payments = _payment_rows(_values(tab))
    row = _pad(row)
    if _duplicate(payments, row):
        organize_tab(tab, payments)
        return "duplicate"
    if row[_PLAN] == "installment" and not row[_REMAINING].strip():
        email = row[_EMAIL]
        programme = row[_PROGRAMME]
        already = sum(
            1
            for existing in payments
            if existing[_PROGRAMME] == programme
            and existing[_PLAN] == "installment"
            and email
            and existing[_EMAIL] == email
        )
        if subscription_id or email:
            row[_REMAINING] = str(already + 1)
    payments.append(row)
    organize_tab(tab, payments)
    return "written"


def mark_refunded(payment_intent: str) -> str:
    if not payment_intent:
        return "no_payment_intent"
    for tab in sales_tabs():
        payments = _payment_rows(_values(tab))
        for index, existing in enumerate(payments):
            if existing[_PAYMENT_ID] == payment_intent:
                already = _is_refunded(existing)
                existing[_STATUS] = "refunded"
                if not already:
                    payments.append(payments.pop(index))
                organize_tab(tab, payments)
                return "refunded"
    return "not_found"


def paid_at(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _money(value: str) -> float | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_payment(row: list[str]) -> None:
    """A full payment has nothing left to pay. Older rows get a programme price from what was stored."""
    currency = row[_CURRENCY]
    if is_speed_wealth(row[_PROGRAMME]):
        row[_PLAN] = "full"
    if row[_PLAN] == "full":
        row[_REMAINING] = amount_major(0, currency)
        if not row[_PRICE].strip():
            row[_PRICE] = row[_PAID]
    elif not row[_PRICE].strip():
        paid = _money(row[_PAID])
        remaining_text = row[_REMAINING].strip().replace(",", "")
        remaining = _money(row[_REMAINING])
        if paid is not None and remaining is not None:
            # 1, 2, 3 is the old installment count, not a balance.
            if not ("." not in remaining_text and "." in row[_PAID].replace(",", "")):
                total = paid + remaining
                row[_PRICE] = (
                    f"{total:.2f}" if "." in row[_PAID] or "." in remaining_text else str(int(round(total)))
                )
    for index in (_PRICE, _REMAINING, _PAID):
        row[index] = _group_digits(row[index])


def _group_digits(value: str) -> str:
    """Insert thousands commas. Leave text that is not a plain number unchanged."""
    text = (value or "").strip().replace(",", "")
    match = re.fullmatch(r"(-?)(\d+)(?:\.(\d+))?", text)
    if not match:
        return value
    sign, whole, frac = match.groups()
    grouped = f"{int(whole):,}"
    if frac is not None:
        grouped = f"{grouped}.{frac}"
    return f"{sign}{grouped}"


def _format_amount(amount: float, currency: str) -> str:
    zero = {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
        "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
    if (currency or "").lower() in zero:
        return f"{int(round(amount)):,}"
    return f"{amount:,.2f}"


def _owed(rows: list[list[str]]) -> float:
    """What one customer's non-refunded rows still leave unpaid."""
    price = max((_money(row[_PRICE]) or 0) for row in rows)
    paid = sum(_money(row[_PAID]) or 0 for row in rows)
    if price <= 0:
        for row in rows:
            if row[_PLAN].strip() != "installment" or "." not in row[_REMAINING]:
                continue
            remaining = _money(row[_REMAINING])
            amount = _money(row[_PAID])
            if remaining is None or amount is None:
                continue
            price = max(price, amount + remaining)
    return max(0.0, price - paid)


def _summary_currency(rows: list[list[str]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        if _is_refunded(row):
            continue
        code = row[_CURRENCY].strip().upper()
        if code:
            counts[code] = counts.get(code, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def _person_text(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _shares_email(row: list[str], group: list[list[str]]) -> bool:
    email = _person_text(row[_EMAIL])
    if not email:
        return False
    return any(_person_text(existing[_EMAIL]) == email for existing in group)


def _shares_name(row: list[str], group: list[list[str]]) -> bool:
    name = _person_text(row[_NAME])
    if not name:
        return False
    return any(_person_text(existing[_NAME]) == name for existing in group)


def _same_person(row: list[str], group: list[list[str]]) -> bool:
    return _shares_email(row, group) or _shares_name(row, group)


def _people(rows: list[list[str]]) -> tuple[list[list[list[str]]], list[list[list[str]]]]:
    """Split one programme into paid in full and installment.

    A later payment with the same email or the same name is the same person.
    It is added to that person's open installment, and the amount still owed
    falls by what they just paid. The person is paid in full only when nothing remains.
    """
    ordered = sorted(
        (row for row in rows if not _is_refunded(row)),
        key=lambda row: row[0],
    )
    paid_full: list[list[list[str]]] = []
    installment: list[list[list[str]]] = []
    for row in ordered:
        match = next((group for group in installment if _same_person(row, group)), None)
        if match is not None:
            installment.remove(match)
            combined = match + [row]
            if _owed(combined) <= 0.009:
                paid_full.append(combined)
            else:
                installment.append(combined)
            continue
        if _owed([row]) > 0.009:
            installment.append([row])
        else:
            paid_full.append([row])
    return paid_full, installment


def _metrics(
    groups: list[list[list[str]]], credit_for: dict[int, float] | None = None
) -> tuple[int, float, float, float]:
    """Sales count, revenue, on-site collection, and amount still to collect.

    Credit reduces what is still owed. It is not added to the amount paid.
    """
    credits = credit_for or {}
    revenue = 0.0
    onsite = 0.0
    due = 0.0
    for group in groups:
        paid = sum(_money(row[_PAID]) or 0 for row in group)
        owed = _owed(group)
        credit = credits.get(id(group), 0.0)
        if credit:
            owed = max(0.0, owed - credit)
        price = max((_money(row[_PRICE]) or 0) for row in group)
        onsite += paid
        due += owed
        revenue += price if price > 0 else paid + owed
    return len(groups), revenue, onsite, due


def _add_metrics(
    left: tuple[int, float, float, float], right: tuple[int, float, float, float]
) -> tuple[int, float, float, float]:
    return (
        left[0] + right[0],
        left[1] + right[1],
        left[2] + right[2],
        left[3] + right[3],
    )


def _selling_price(rows: list[list[str]]) -> str:
    """Programme price shown in the sales summary. Empty when no price was recorded."""
    price = max((_money(row[_PRICE]) or 0 for row in rows if not _is_refunded(row)), default=0)
    if price <= 0:
        return ""
    return _format_amount(price, _summary_currency(rows))


def _summary_line(
    label: str,
    metrics: tuple[int, float, float, float],
    currency: str,
    selling_price: str = "",
    symbol: str = "€",
) -> list[str]:
    sales, revenue, onsite, due = metrics

    def marked(amount: float) -> str:
        text = _format_amount(amount, currency)
        return f"{symbol}{text}" if symbol and text else text

    return [
        label,
        selling_price,
        f"{sales:,}",
        marked(revenue),
        marked(onsite),
        marked(due),
    ]


def _matches_programme(programme: str, keyword: str) -> bool:
    key = _programme_key(programme)
    return bool(key) and keyword in key


# A later Quantum Leap purchase takes credit from these programmes.
_UPGRADE_SOURCES = (
    "neverworkagain",
    "guerrillabusiness",
    "trainthetrainer",
    "enlightenedwarrior",
)


def _group_keys(group: list[list[str]]) -> tuple[set[str], set[str]]:
    emails = {_person_text(row[_EMAIL]) for row in group if _person_text(row[_EMAIL])}
    names = {_person_text(row[_NAME]) for row in group if _person_text(row[_NAME])}
    return emails, names


def _same_group(left: list[list[str]], right: list[list[str]]) -> bool:
    """Same person when the email matches or the name matches. A blank matches nothing."""
    left_emails, left_names = _group_keys(left)
    right_emails, right_names = _group_keys(right)
    if left_emails & right_emails:
        return True
    return bool(left_names & right_names)


def _programme_reports(payments: list[list[str]]) -> list[dict]:
    """Sales figures from the programme groups after Quantum Leap transfers.

    Rows flagged as transferred sit in the Quantum Leap group, not in Never Work
    Again, Guerrilla Business Intensive, Train the Trainer, or Enlightened Warrior
    Camp. Their amount paid is credit toward Quantum Leap. The Quantum Leap amount
    paid stays the cash paid on the Quantum Leap row.
    """
    rows_by_programme: dict[str, list[list[str]]] = {
        keyword: [] for _label, keyword, _fill, _font in _SUMMARY_PROGRAMMES
    }
    transferred: list[list[str]] = []
    for row in payments:
        if _is_refunded(row):
            continue
        if _is_transferred(row):
            transferred.append(row)
            continue
        for _label, keyword, _fill, _font in _SUMMARY_PROGRAMMES:
            if _matches_programme(row[_PROGRAMME], keyword):
                rows_by_programme[keyword].append(row)
                break

    split = {
        keyword: _people(rows_by_programme[keyword])
        for _label, keyword, _fill, _font in _SUMMARY_PROGRAMMES
    }
    ql_full, ql_installment = split["quantumleap"]
    consumed: set[int] = set()
    upgrade_groups: list[list[list[str]]] = []
    upgrade_credits: dict[int, float] = {}
    for group in ql_full + ql_installment:
        credit = 0.0
        for source in transferred:
            if id(source) in consumed or not _same_group(group, [source]):
                continue
            paid = _money(source[_PAID]) or 0
            if paid <= 0.009:
                continue
            credit += paid
            consumed.add(id(source))
        if credit > 0.009:
            upgrade_groups.append(group)
            upgrade_credits[id(group)] = credit

    reports: list[dict] = []
    for label, keyword, _fill, _font in _SUMMARY_PROGRAMMES:
        programme_rows = rows_by_programme[keyword]
        full, installment = split[keyword]
        if keyword == "quantumleap":
            full = [group for group in full if id(group) not in upgrade_credits]
            installment = [group for group in installment if id(group) not in upgrade_credits]
            upgrade = _metrics(upgrade_groups, upgrade_credits) if upgrade_groups else None
        else:
            upgrade = None
        full_metrics = _metrics(full)
        installment_metrics = _metrics(installment)
        combined = _add_metrics(full_metrics, installment_metrics)
        if upgrade is not None:
            combined = _add_metrics(combined, upgrade)
        reports.append(
            {
                "label": label,
                "keyword": keyword,
                "currency": _summary_currency(programme_rows),
                "price": _selling_price(programme_rows),
                "combined": combined,
                "full": full_metrics,
                "installment": installment_metrics,
                "upgrade": upgrade,
            }
        )
    return reports


def _render_summary(
    reports: list[dict], usd_per_unit: dict[str, float] | None = None
) -> tuple[list[list[str]], list[str]]:
    """Turn programme figures into the sales summary table."""
    values: list[list[str]] = [["Sales summary"] + [""] * (len(_SUMMARY_HEADERS) - 1)]
    kinds = ["title"]
    values.append(list(_SUMMARY_HEADERS))
    kinds.append("header")

    total = (0, 0.0, 0.0, 0.0)
    total_currency = ""
    usd_revenue = 0.0
    usd_onsite = 0.0
    usd_due = 0.0
    usd_known = True
    rates = usd_per_unit or {}
    for report in reports:
        currency = report["currency"]
        combined = report["combined"]
        total = _add_metrics(total, combined)
        if combined[0] and not total_currency:
            total_currency = currency
        rate = rates.get(currency) if currency else None
        if currency == "USD":
            rate = 1.0
        if combined[0] and rate is None:
            usd_known = False
        elif rate is not None:
            usd_revenue += combined[1] * rate
            usd_onsite += combined[2] * rate
            usd_due += combined[3] * rate
        price = report["price"]
        values.append(_summary_line(report["label"], combined, currency, price))
        kinds.append("programme")
        values.append(_summary_line("Paid in full", report["full"], currency, price))
        kinds.append("detail")
        values.append(_summary_line("Installments", report["installment"], currency, price))
        kinds.append("detail")
        if report["upgrade"] is not None:
            values.append(_summary_line("Upgrade", report["upgrade"], currency, price))
            kinds.append("detail")

    if not total_currency:
        for report in reports:
            if report["currency"]:
                total_currency = report["currency"]
                break
    values.append(_summary_line("Total", total, total_currency))
    kinds.append("total")
    if usd_known:
        usd_metrics = (total[0], usd_revenue, usd_onsite, usd_due)
        values.append(_summary_line("Total sales at event (USD)", usd_metrics, "USD", symbol="$"))
    else:
        values.append(["Total sales at event (USD)", "", f"{total[0]:,}", "", "", ""])
    kinds.append("usd")
    return values, kinds


def summary_values(
    by_tab: dict[str, list[list[str]]], usd_per_unit: dict[str, float] | None = None
) -> tuple[list[list[str]], list[str]]:
    """Sales summary rows for one event. The caller leaves three blank rows above this table."""
    payments = [_pad(row) for rows in by_tab.values() for row in rows]
    _mark_ql_transfers(payments)
    return _render_summary(_programme_reports(payments), usd_per_unit)


def _euro(amount: float, currency: str) -> str:
    return f"€{_format_amount(amount, currency)}"


def _prelim_cells(
    label: str,
    quantity: str = "",
    amount: str = "",
    paid: str = "",
    balance: str = "",
) -> list[str]:
    """Label in A–C, then quantity, amount, paid, and balance."""
    return _pad([label, "", "", quantity, amount, paid, balance])


def _prelim_side(label: str, value: str = "") -> list[str]:
    """Attendance and trainer sit on the right, as on the L2 example."""
    return _pad(["", "", "", "", "", label, value])


def _prelim_line(label: str, metrics: tuple[int, float, float, float], currency: str) -> list[str]:
    sales, revenue, onsite, due = metrics
    return _prelim_cells(label, f"{sales:,}", _euro(revenue, currency), _euro(onsite, currency), _euro(due, currency))


def preliminary_values(tab: str, reports: list[dict]) -> tuple[list[list[str]], list[str]]:
    """L2 preliminary report copied from the sales summary figures.

    The caller leaves three blank rows above this table. Venue, attendance, and
    lead trainer stay blank. This does not read payment rows itself.
    """
    by_keyword = {report["keyword"]: report for report in reports}
    zero = (0, 0.0, 0.0, 0.0)
    values: list[list[str]] = []
    kinds: list[str] = []

    def add(row: list[str], kind: str) -> None:
        values.append(row)
        kinds.append(kind)

    add(_pad([f"{tab} L2 Preliminary Sales Report"]), "title")
    add(_pad([""]), "venue")
    for label in ("Attendance (Max):", "Attendance (Average):", "Attendance (Minimum):"):
        add(_prelim_side(label), "attendance")
    add(_prelim_side("Lead Trainer:"), "trainer")
    add(_prelim_cells("Programs", "Quantity", "Amount", "Paid", "Balance"), "header")

    full_total = zero
    installment_total = zero
    upgrade_total = zero
    total_currency = ""
    for _label, keyword in _PRELIM_PROGRAMMES:
        report = by_keyword[keyword]
        currency = report["currency"] or "EUR"
        if report["combined"][0] and not total_currency:
            total_currency = currency
        full_total = _add_metrics(full_total, report["full"])
        installment_total = _add_metrics(installment_total, report["installment"])
        add(_prelim_line(report["label"], report["combined"], currency), "programme")
        add(_prelim_line("Full Payment", report["full"], currency), "detail")
        add(_prelim_line("Installment", report["installment"], currency), "detail")
        if report["upgrade"] is not None:
            upgrade_total = _add_metrics(upgrade_total, report["upgrade"])
            add(_prelim_line("Upgrade", report["upgrade"], currency), "detail")
    if not total_currency:
        total_currency = "EUR"
    programme_total = _add_metrics(_add_metrics(full_total, installment_total), upgrade_total)
    add(_prelim_line("Grand Total", programme_total, total_currency), "grand")
    add(_pad([""]), "separator")
    add(_prelim_cells("Summary", "Total Quantity", "Total Amount", "Total Paid", "Total Balance"), "header")
    add(_prelim_line("Full Payment", full_total, total_currency), "detail")
    add(_prelim_line("Installment", installment_total, total_currency), "detail")
    if upgrade_total[0]:
        add(_prelim_line("Upgrade", upgrade_total, total_currency), "detail")
    add(_prelim_line("Confirmed Sales", programme_total, total_currency), "confirmed")
    add(_prelim_line("Total Sales", programme_total, total_currency), "total")
    return values, kinds


def _usd_per_unit(currencies: set[str]) -> dict[str, float]:
    rates: dict[str, float] = {}
    for code in currencies:
        if not code or code == "USD":
            rates[code] = 1.0
            continue
        url = f"https://api.frankfurter.app/latest?from={urllib.parse.quote(code)}&to=USD"
        request = urllib.request.Request(url, headers={"User-Agent": "sr-event-sales"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode())
            rates[code] = float(payload["rates"]["USD"])
        except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError):
            continue
    return rates


def _delete_summary_tab() -> None:
    titles = _tabs()
    if SUMMARY_TAB not in titles:
        return
    _batch([{"deleteSheet": {"sheetId": titles[SUMMARY_TAB]}}])


def refresh_summary() -> None:
    """Put the Sales summary on the Rotterdam tab and remove the separate tab."""
    if not configured():
        return
    _delete_summary_tab()
    if ROTTERDAM_TAB not in _tabs():
        return
    organize_tab(ROTTERDAM_TAB)
    log.info("sales summary placed on %s", ROTTERDAM_TAB)


def _paint_summary(sheet_id: int, values: list[list[str]], kinds: list[str], start: int) -> list[dict]:
    width = len(_SUMMARY_HEADERS)
    requests: list[dict] = []
    fills = {
        label: (fill, font)
        for label, _keyword, fill, font in _SUMMARY_PROGRAMMES
    }
    for index, kind in enumerate(kinds):
        row_index = start + index
        label = values[index][0] if values[index] else ""
        if kind == "title":
            requests.append(
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": width,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                }
            )
            fill, font, size, align = "FFFFFF", "000000", 14, "CENTER"
            bold = True
        elif kind == "header":
            fill, font, size, align = "FFFFFF", "000000", 10, "LEFT"
            bold = True
        elif kind == "programme" and label in fills:
            fill, font = fills[label]
            size, align, bold = 10, "LEFT", True
        elif kind == "total":
            fill, font, size, align, bold = _TOTAL_FILL, _TOTAL_FONT, 10, "LEFT", True
        else:
            if kind != "title":
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row_index,
                                "endRowIndex": row_index + 1,
                                "startColumnIndex": 1,
                                "endColumnIndex": width,
                            },
                            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                            "fields": "userEnteredFormat.horizontalAlignment",
                        }
                    }
                )
            continue
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": width,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(fill),
                            "horizontalAlignment": align,
                            "textFormat": {
                                "foregroundColor": _rgb(font),
                                "bold": bold,
                                "fontSize": size,
                            },
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
            }
        )
        if kind != "title":
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 1,
                            "endColumnIndex": width,
                        },
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                        "fields": "userEnteredFormat.horizontalAlignment",
                    }
                }
            )
    return requests


def _paint_preliminary(sheet_id: int, kinds: list[str], start: int) -> list[dict]:
    """Style the L2 preliminary block to match the Cologne example."""
    requests: list[dict] = []

    def paint(row: int, c1: int, c2: int, fill: str, font: str, bold: bool, size: int, align: str) -> None:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row,
                        "endRowIndex": row + 1,
                        "startColumnIndex": c1,
                        "endColumnIndex": c2,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(fill),
                            "horizontalAlignment": align,
                            "textFormat": {
                                "foregroundColor": _rgb(font),
                                "bold": bold,
                                "fontSize": size,
                            },
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
            }
        )

    def merge(row: int, c1: int, c2: int) -> None:
        requests.append(
            {
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row,
                        "endRowIndex": row + 1,
                        "startColumnIndex": c1,
                        "endColumnIndex": c2,
                    },
                    "mergeType": "MERGE_ALL",
                }
            }
        )

    for index, kind in enumerate(kinds):
        row = start + index
        if kind == "title":
            merge(row, 0, _PRELIM_END)
            paint(row, 0, _PRELIM_END, "FFFF00", "0000FF", True, 20, "CENTER")
        elif kind == "venue":
            merge(row, 0, _PRELIM_END)
            paint(row, 0, _PRELIM_END, "FFFF00", "FF0000", True, 9, "CENTER")
        elif kind in {"attendance", "trainer"}:
            merge(row, 0, 5)
            paint(row, 0, 5, "FFFFFF", "000000", False, 9, "LEFT")
            paint(row, 5, 6, "FFFFFF", "000000", False, 9, "RIGHT")
            paint(row, 6, 7, "FFFF00", "000000", kind == "trainer", 9, "CENTER")
        elif kind == "separator":
            merge(row, 0, _PRELIM_END)
            paint(row, 0, _PRELIM_END, "000000", "FFFFFF", False, 9, "LEFT")
        elif kind == "header":
            merge(row, 0, 3)
            paint(row, 0, _PRELIM_END, "CFE2F3", "000000", True, 9, "CENTER")
        elif kind == "product_total":
            merge(row, 0, 3)
            paint(row, 0, _PRELIM_END, "000000", "FFFFFF", True, 9, "LEFT")
            paint(row, 3, _PRELIM_END, "000000", "FFFFFF", True, 9, "RIGHT")
        elif kind == "programme":
            merge(row, 0, 3)
            paint(row, 0, _PRELIM_END, "FCE4D6", "000000", True, 9, "LEFT")
            paint(row, 3, _PRELIM_END, "FCE4D6", "000000", True, 9, "RIGHT")
        elif kind == "grand":
            merge(row, 0, 3)
            paint(row, 0, _PRELIM_END, "CFE2F3", "000000", True, 9, "LEFT")
            paint(row, 3, _PRELIM_END, "CFE2F3", "000000", True, 9, "RIGHT")
        elif kind == "confirmed":
            merge(row, 0, 3)
            paint(row, 0, _PRELIM_END, "6D9EEB", "FFFFFF", True, 9, "LEFT")
            paint(row, 3, _PRELIM_END, "6D9EEB", "FFFFFF", True, 9, "RIGHT")
        elif kind == "total":
            merge(row, 0, 3)
            paint(row, 0, _PRELIM_END, "FFFFFF", "000000", True, 9, "LEFT")
            paint(row, 3, _PRELIM_END, "FFFFFF", "000000", True, 9, "RIGHT")
        else:
            merge(row, 0, 3)
            paint(row, 0, _PRELIM_END, "FFFFFF", "000000", False, 9, "LEFT")
            paint(row, 3, _PRELIM_END, "FFFFFF", "000000", False, 9, "RIGHT")

    if kinds:
        border = {"style": "SOLID", "width": 1, "color": _BLACK}
        requests.append(
            {
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start,
                        "endRowIndex": start + len(kinds),
                        "startColumnIndex": 0,
                        "endColumnIndex": _PRELIM_END,
                    },
                    "top": border,
                    "bottom": border,
                    "left": border,
                    "right": border,
                    "innerHorizontal": border,
                    "innerVertical": border,
                }
            }
        )
    return requests


def amount_major(cents: int, currency: str) -> str:
    zero = {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
        "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
    if (currency or "").lower() in zero:
        return str(int(cents))
    return f"{int(cents) / 100:.2f}"
