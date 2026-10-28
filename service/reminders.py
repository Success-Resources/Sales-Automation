"""Email the customer when an installment balance is still open after 10 minutes."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import mail
from sheet import (
    _EMAIL,
    _CURRENCY,
    _PAID,
    _PAYMENT_ID,
    _PLAN,
    _PRICE,
    _PROGRAMME,
    _REMAINING,
    _STATUS,
    append_rows,
    ensure_reminder_tab,
    is_speed_wealth,
    paid_at,
    read_log,
    read_payments,
    sales_tabs,
)

log = logging.getLogger("sales.reminders")
_missing_gmail_logged = False
_disabled_logged = False

_TRACKING = "__tracking_since__"
_NAME = 7


def wait_minutes() -> int:
    raw = os.getenv("REMINDER_MINUTES", "10").strip() or "10"
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


def _money(value: str) -> float | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _when(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def open_balances(tab: str, payments: list[list[str]]) -> list[dict]:
    """One open balance per customer and programme. Later payments reduce what is still owed."""
    grouped: dict[tuple[str, str], list[list[str]]] = {}
    for row in payments:
        if row[_STATUS].strip().lower() == "refunded":
            continue
        email = row[_EMAIL].strip()
        programme = row[_PROGRAMME].strip()
        if not email or not programme or is_speed_wealth(programme):
            continue
        grouped.setdefault((email.lower(), programme), []).append(row)

    found = []
    for (email, programme), rows in grouped.items():
        if not any(row[_PLAN].strip() == "installment" for row in rows):
            continue
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
        outstanding = round(price - paid, 2)
        if outstanding <= 0.009:
            continue
        latest = None
        latest_row = rows[-1]
        for row in rows:
            stamp = _when(row[0])
            if stamp is not None and (latest is None or stamp >= latest):
                latest = stamp
                latest_row = row
        if latest is None:
            continue
        currency = latest_row[_CURRENCY].strip().upper()
        found.append(
            {
                "key": f"{tab}\t{email}\t{programme}",
                "tab": tab,
                "email": latest_row[_EMAIL].strip(),
                "name": latest_row[_NAME].strip(),
                "programme": programme,
                "outstanding": f"{outstanding:.2f}",
                "currency": currency,
                "paid_at": latest,
                "payment_id": latest_row[_PAYMENT_ID].strip(),
            }
        )
    return found


def due_balances(
    balances: list[dict],
    sent: set[str],
    tracking_since: datetime,
    now: datetime,
    minutes: int,
) -> list[dict]:
    cutoff = now - timedelta(minutes=minutes)
    ready = []
    for balance in balances:
        if balance["key"] in sent:
            continue
        paid = balance["paid_at"]
        if paid < tracking_since or paid > cutoff:
            continue
        ready.append(balance)
    return ready


def _sent_keys(rows: list[list[str]]) -> set[str]:
    keys = set()
    for row in rows:
        if not row:
            continue
        if row[0].strip() == _TRACKING:
            continue
        status = row[6].strip().lower() if len(row) > 6 else ""
        if status == "sent":
            keys.add(row[0].strip())
    return keys


def _tracking_since(rows: list[list[str]]) -> datetime | None:
    for row in rows:
        if row and row[0].strip() == _TRACKING and len(row) > 5:
            return _when(row[5])
    return None


def send_due() -> int:
    """Send reminders that are due. Return how many emails were sent."""
    global _missing_gmail_logged, _disabled_logged
    if not mail.sending_enabled():
        if not _disabled_logged:
            log.info("reminders are off")
            _disabled_logged = True
        return 0
    if not mail.configured():
        if not _missing_gmail_logged:
            log.info("reminder skipped, GMAIL_ADDRESS or GMAIL_APP_PASSWORD is not set")
            _missing_gmail_logged = True
        return 0
    now = datetime.now(timezone.utc)
    count = 0
    for tab in sales_tabs():
        log_name = ensure_reminder_tab(tab)
        log_rows = read_log(log_name)
        if not log_rows:
            log_rows = [["Key", "Tab", "Email", "Programme", "Outstanding", "Sent at", "Status"]]
            append_rows(log_name, log_rows)
        started = _tracking_since(log_rows)
        if started is None:
            started = now
            append_rows(
                log_name,
                [[_TRACKING, "", "", "", "", paid_at(int(started.timestamp())), "tracking"]],
            )
            log.info("reminder tracking starts at %s for %s", paid_at(int(started.timestamp())), tab)
        sent = _sent_keys(log_rows)
        for balance in due_balances(open_balances(tab, read_payments(tab)), sent, started, now, wait_minutes()):
            mail.send_outstanding(
                to=balance["email"],
                name=balance["name"],
                programme=balance["programme"],
                outstanding=balance["outstanding"],
                currency=balance["currency"],
            )
            append_rows(
                log_name,
                [[
                    balance["key"],
                    balance["tab"],
                    balance["email"],
                    balance["programme"],
                    f"{balance['outstanding']} {balance['currency']}".strip(),
                    paid_at(int(now.timestamp())),
                    "sent",
                ]],
            )
            sent.add(balance["key"])
            count += 1
            log.info(
                "reminder sent tab=%s programme=%s payment=%s",
                balance["tab"],
                balance["programme"],
                balance["payment_id"],
            )
    return count


def loop() -> None:
    time.sleep(20)
    while True:
        try:
            send_due()
        except Exception:
            log.exception("reminder scan failed")
        time.sleep(60)
