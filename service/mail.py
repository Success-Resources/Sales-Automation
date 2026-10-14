"""Send the outstanding-balance reminder from the Gmail account on Render."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def configured() -> bool:
    return bool(_address() and _password())


def _address() -> str:
    return os.getenv("GMAIL_ADDRESS", "").strip()


def _password() -> str:
    return os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()


def send_outstanding(
    *,
    to: str,
    name: str,
    programme: str,
    outstanding: str,
    currency: str,
) -> None:
    address = _address()
    first = (name or "").strip().split()
    hello = f"Hello {first[0]}," if first else "Hello,"
    amount = f"{outstanding} {currency}".strip()
    message = EmailMessage()
    message["From"] = address
    message["To"] = to
    message["Subject"] = f"Remaining payment for {programme}"
    message.set_content(
        f"{hello}\n\n"
        f"A remaining amount of {amount} is still outstanding for {programme}.\n\n"
        "Please complete this payment.\n"
    )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(address, _password())
        smtp.send_message(message)
