"""Stripe webhook: event-sales payments into the SR Event Sales sheet.

Product names look like: MMI2610ROT | Quantum Leap 2027
The text before the bar is the sheet tab. The text after the bar is the programme.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from products import parse_product_name
from sheet import (
    ROTTERDAM_TAB,
    amount_major,
    configured as sheet_configured,
    ensure_tab,
    mark_refunded,
    paid_at,
    record_payment,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sales")

app = FastAPI(title="SR Event Sales")


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


def _first_matching_product(lines: list) -> tuple[str, str, tuple[str, str]] | None:
    for line in lines:
        line = _as_dict(line)
        price = line.get("price") or {}
        name, product_id = _product_from_price(price)
        if not name:
            name = str(line.get("description") or "")
        parsed = parse_product_name(name)
        if parsed:
            return name, product_id, parsed
    return None


def _row(
    *,
    when: int,
    programme: str,
    plan: str,
    amount: int,
    currency: str,
    customer_name: str,
    customer_email: str,
    phone: str,
    product_name: str,
    product_id: str,
    payment_intent: str,
    invoice_id: str,
) -> list[str]:
    return [
        paid_at(when),
        programme,
        plan,
        "",
        amount_major(amount, currency),
        (currency or "").upper(),
        customer_name,
        customer_email,
        phone,
        product_name,
        product_id,
        payment_intent,
        invoice_id,
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
    product_name, product_id, (tab, programme) = found
    details = loaded.get("customer_details") or {}
    plan = "installment" if loaded.get("mode") == "subscription" else "full"
    # The first subscription invoice is recorded from invoice.paid, not here.
    if plan == "installment":
        log.info("checkout subscription deferred to invoice.paid session=%s", session.get("id"))
        return None
    return tab, _row(
        when=int(event.get("created") or loaded.get("created") or 0),
        programme=programme,
        plan=plan,
        amount=int(loaded.get("amount_total") or 0),
        currency=str(loaded.get("currency") or ""),
        customer_name=str(details.get("name") or ""),
        customer_email=str(details.get("email") or loaded.get("customer_email") or ""),
        phone=str(details.get("phone") or ""),
        product_name=product_name,
        product_id=product_id,
        payment_intent=_id(loaded.get("payment_intent")),
        invoice_id=_id(loaded.get("invoice")),
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
    product_name, product_id, (tab, programme) = found
    subscription_id = _id(loaded.get("subscription"))
    return tab, _row(
        when=int(
            event.get("created")
            or (loaded.get("status_transitions") or {}).get("paid_at")
            or loaded.get("created")
            or 0
        ),
        programme=programme,
        plan="installment" if subscription_id else "full",
        amount=int(loaded.get("amount_paid") or 0),
        currency=str(loaded.get("currency") or ""),
        customer_name=str(loaded.get("customer_name") or ""),
        customer_email=str(loaded.get("customer_email") or ""),
        phone=str(loaded.get("customer_phone") or ""),
        product_name=product_name,
        product_id=product_id,
        payment_intent=_id(loaded.get("payment_intent")),
        invoice_id=str(loaded.get("id") or ""),
    )


@app.on_event("startup")
def _prepare_rotterdam_tab() -> None:
    if not sheet_configured():
        log.info("sheet not configured yet")
        return
    try:
        ensure_tab(ROTTERDAM_TAB)
        log.info("sheet tab ready %s", ROTTERDAM_TAB)
    except Exception:
        log.exception("could not prepare sheet tab %s", ROTTERDAM_TAB)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "sheet_configured": sheet_configured(),
        "stripe_configured": _stripe_ready(),
        "rotterdam_tab": ROTTERDAM_TAB,
    }


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
