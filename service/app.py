"""Stripe webhook: event-sales payments into the SR Event Sales sheet.

Product names look like: MMI2610ROT | Quantum Leap 2027
The text before the bar is the sheet tab. The text after the bar is the programme.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import mail
import reminders
from products import parse_product_name
from sheet import (
    ROTTERDAM_TAB,
    amount_major,
    configured as sheet_configured,
    is_speed_wealth,
    mark_refunded,
    paid_at,
    record_payment,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sales")


class _HideHealthChecks(logging.Filter):
    """Render probes /health every few seconds. Keep the check, drop the access line."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn logs (client, method, path, version, status) and only
        # builds the "GET /health HTTP/1.1" text later, while formatting.
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = str(args[2]).split("?", 1)[0]
            return path not in {"/", "/health"}
        return True


def _quiet_health_logs() -> None:
    logging.getLogger("uvicorn.access").addFilter(_HideHealthChecks())


_quiet_health_logs()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _quiet_health_logs()
    threading.Thread(target=reminders.loop, name="reminders", daemon=True).start()
    yield


app = FastAPI(title="SR Event Sales", lifespan=_lifespan)


def _stripe_ready() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY", "").strip() and os.getenv("STRIPE_WEBHOOK_SECRET", "").strip())


def _as_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value)


def _id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(getattr(value, "id", "") or "")


def _product_from_price(price) -> tuple[str, str]:
    price = _as_dict(price)
    product = price.get("product")
    if isinstance(product, dict):
        return str(product.get("name") or ""), str(product.get("id") or "")
    if isinstance(product, str):
        loaded = _as_dict(stripe.Product.retrieve(product))
        return str(loaded.get("name") or ""), product
    return "", ""


def _preset_cents(price) -> int | None:
    """Suggested amount on a customer-chooses-the-price Stripe price, in minor units."""
    price = _as_dict(price)
    custom = price.get("custom_unit_amount")
    if not isinstance(custom, dict):
        custom = _as_dict(custom) if custom else {}
    preset = custom.get("preset")
    if preset is None:
        return None
    return int(preset)


def _programme_price_cents(price) -> int | None:
    """Full programme price: the suggested amount, or the fixed Stripe price when there is no suggestion."""
    preset = _preset_cents(price)
    if preset:
        return preset
    unit = _as_dict(price).get("unit_amount")
    if unit is None:
        return None
    return int(unit)


def _plan_for(
    programme: str,
    paid_cents: int,
    preset_cents: int | None,
    price_cents: int | None,
    currency: str,
    subscription: bool,
) -> tuple[str, str, str]:
    """Speed Wealth Book is always full. Other programmes use the Stripe suggested amount.

    A full payment leaves Installment (remaining) at zero. Programme price is the full price.
    """
    price = amount_major(price_cents, currency) if price_cents else ""
    nothing_left = amount_major(0, currency)
    if is_speed_wealth(programme):
        return "full", nothing_left, price or amount_major(paid_cents, currency)
    if preset_cents:
        if paid_cents >= preset_cents:
            return "full", nothing_left, price
        return "installment", amount_major(preset_cents - paid_cents, currency), price
    if subscription:
        return "installment", "", price
    return "full", nothing_left, price or amount_major(paid_cents, currency)


def _first_matching_product(lines: list) -> tuple[str, str, tuple[str, str], int | None, int | None] | None:
    for line in lines:
        line = _as_dict(line)
        price = line.get("price") or {}
        name, product_id = _product_from_price(price)
        if not name:
            name = str(line.get("description") or "")
        parsed = parse_product_name(name)
        if parsed:
            return name, product_id, parsed, _preset_cents(price), _programme_price_cents(price)
    return None


def _row(
    *,
    when: int,
    programme: str,
    programme_price: str,
    plan: str,
    amount: int,
    currency: str,
    customer_name: str,
    customer_email: str,
    phone: str,
    product_name: str,
    product_id: str,
    payment_intent: str,
    installment: str = "",
) -> list[str]:
    return [
        paid_at(when),
        programme,
        programme_price,
        plan,
        installment,
        amount_major(amount, currency),
        (currency or "").upper(),
        customer_name,
        customer_email,
        phone,
        product_name,
        product_id,
        payment_intent,
        "succeeded",
    ]


def _from_checkout(session: dict, event: dict) -> tuple[str, list[str]] | None:
    if session.get("payment_status") != "paid":
        return None
    loaded = _as_dict(
        stripe.checkout.Session.retrieve(
            session["id"],
            expand=["line_items.data.price.product"],
        )
    )
    lines = (loaded.get("line_items") or {}).get("data") or []
    if not lines:
        listed = stripe.checkout.Session.list_line_items(
            session["id"],
            expand=["data.price.product"],
            limit=10,
        )
        lines = _as_dict(listed).get("data") or []
    found = _first_matching_product(lines)
    if not found:
        log.info("checkout ignored, no event product session=%s", session.get("id"))
        return None
    product_name, product_id, (tab, programme), preset, price_cents = found
    details = loaded.get("customer_details") or {}
    paid = int(loaded.get("amount_total") or 0)
    currency = str(loaded.get("currency") or "")
    subscription = loaded.get("mode") == "subscription"
    if subscription and not is_speed_wealth(programme) and not preset:
        # A Stripe subscription's first invoice is recorded from invoice.paid, not here.
        log.info("checkout subscription deferred to invoice.paid session=%s", session.get("id"))
        return None
    plan, remaining, programme_price = _plan_for(programme, paid, preset, price_cents, currency, subscription)
    log.info(
        "checkout plan=%s price=%s preset=%s paid=%s remaining=%s",
        plan,
        programme_price,
        preset,
        paid,
        remaining,
    )
    return tab, _row(
        when=int(event.get("created") or loaded.get("created") or 0),
        programme=programme,
        programme_price=programme_price,
        plan=plan,
        amount=paid,
        currency=currency,
        customer_name=str(details.get("name") or ""),
        customer_email=str(details.get("email") or loaded.get("customer_email") or ""),
        phone=str(details.get("phone") or ""),
        product_name=product_name,
        product_id=product_id,
        payment_intent=_id(loaded.get("payment_intent")),
        installment=remaining,
    )


def _from_invoice(invoice: dict, event: dict) -> tuple[str, list[str]] | None:
    loaded = _as_dict(
        stripe.Invoice.retrieve(
            invoice["id"],
            expand=["lines.data.price.product"],
        )
    )
    lines = (loaded.get("lines") or {}).get("data") or []
    found = _first_matching_product(lines)
    if not found:
        log.info("invoice ignored, no event product invoice=%s", invoice.get("id"))
        return None
    product_name, product_id, (tab, programme), preset, price_cents = found
    subscription_id = _id(loaded.get("subscription"))
    paid = int(loaded.get("amount_paid") or 0)
    currency = str(loaded.get("currency") or "")
    plan, remaining, programme_price = _plan_for(
        programme, paid, preset, price_cents, currency, bool(subscription_id)
    )
    log.info(
        "invoice plan=%s price=%s preset=%s paid=%s remaining=%s",
        plan,
        programme_price,
        preset,
        paid,
        remaining,
    )
    return tab, _row(
        when=int(
            event.get("created")
            or (loaded.get("status_transitions") or {}).get("paid_at")
            or loaded.get("created")
            or 0
        ),
        programme=programme,
        programme_price=programme_price,
        plan=plan,
        amount=paid,
        currency=currency,
        customer_name=str(loaded.get("customer_name") or ""),
        customer_email=str(loaded.get("customer_email") or ""),
        phone=str(loaded.get("customer_phone") or ""),
        product_name=product_name,
        product_id=product_id,
        payment_intent=_id(loaded.get("payment_intent")),
        installment=remaining,
    )


def _status() -> dict:
    return {
        "ok": True,
        "sheet_configured": sheet_configured(),
        "stripe_configured": _stripe_ready(),
        "gmail_configured": mail.configured(),
        "rotterdam_tab": ROTTERDAM_TAB,
    }


@app.get("/")
def root() -> dict:
    return _status()


@app.get("/health")
def health() -> dict:
    return _status()


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret or not stripe.api_key or not sheet_configured():
        log.error("webhook rejected, missing stripe or sheet configuration")
        return JSONResponse({"ok": False, "error": "not configured"}, status_code=500)
    try:
        event = stripe.Webhook.construct_event(
            payload,
            request.headers.get("stripe-signature", ""),
            secret,
        )
    except Exception as exc:
        log.warning("webhook signature rejected: %s", exc)
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=400)

    event = _as_dict(event)
    kind = str(event.get("type") or "")
    obj = _as_dict((event.get("data") or {}).get("object"))
    log.info("webhook %s id=%s", kind, event.get("id"))
    try:
        if kind == "checkout.session.completed":
            built = _from_checkout(obj, event)
            if not built:
                return JSONResponse({"ok": True, "result": "skipped"})
            tab, row = built
            result = record_payment(tab, row, _id(obj.get("subscription")))
        elif kind == "invoice.paid":
            built = _from_invoice(obj, event)
            if not built:
                return JSONResponse({"ok": True, "result": "skipped"})
            tab, row = built
            result = record_payment(tab, row, _id(obj.get("subscription")))
        elif kind == "charge.refunded":
            result = mark_refunded(_id(obj.get("payment_intent")))
            tab = ""
        else:
            return JSONResponse({"ok": True, "result": "ignored", "type": kind})
    except Exception:
        log.exception("webhook failed type=%s", kind)
        return JSONResponse({"ok": False, "error": "processing failed"}, status_code=500)
    log.info("webhook result=%s tab=%s", result, tab)
    return JSONResponse({"ok": True, "result": result, "tab": tab})
