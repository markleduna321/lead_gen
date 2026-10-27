"""PayMongo QR Ph checkout, durable orders, and exactly-once wallet fulfillment."""

from decimal import Decimal
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import requests

import billing
from plans import PLANS, PlanError
from teams import seat_usage

LOGGER = logging.getLogger(__name__)
PRICES = {
    "solo": {1: 99900, 12: 999000, "topup": 49900, "pack": 350},
    "small_team": {1: 249900, 12: 2499000, "topup": 79900, "pack": 750},
    "enterprise": {1: 599900, 12: 5999000, "topup": 149900, "pack": 2000},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS purchase_orders (
    id UUID PRIMARY KEY,
    wallet_id BIGINT NOT NULL REFERENCES team_wallets(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    request_key UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('plan','topup')),
    plan_id TEXT NOT NULL CHECK (plan_id IN ('solo','small_team','enterprise')),
    months INTEGER NOT NULL CHECK (months IN (1,12)),
    amount INTEGER NOT NULL CHECK (amount>0),
    credits INTEGER NOT NULL CHECK (credits>0),
    livemode BOOLEAN NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('creating','pending','unknown','failed','expired','paid','review')),
    session_id TEXT UNIQUE,
    checkout_url TEXT,
    payment_id TEXT UNIQUE,
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paid_at TIMESTAMPTZ,
    fulfilled_at TIMESTAMPTZ,
    UNIQUE(user_id,request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS purchase_one_open_wallet ON purchase_orders(wallet_id)
    WHERE status IN ('creating','pending','unknown','review');
CREATE INDEX IF NOT EXISTS purchase_wallet_history ON purchase_orders(wallet_id,created_at DESC);
"""


def configuration():
    key = os.getenv("PAYMONGO_SECRET_KEY", "").strip()
    secret = os.getenv("PAYMONGO_WEBHOOK_SECRET", "").strip()
    base = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    origin = urlsplit(base)
    if (not key.startswith(("sk_test_", "sk_live_")) or not secret
            or origin.scheme != "https" or not origin.hostname or origin.username
            or origin.password or origin.query or origin.fragment or origin.path):
        raise PlanError("Payments are not configured. Contact support before purchasing.",
                        "payments_unavailable", 503)
    return key, secret, base


def checkout_ready():
    try:
        key, _, _ = configuration()
    except PlanError:
        return False, False
    return True, key.startswith("sk_live_")


def owner_wallet(cur, user_id):
    wallet = billing.lock_wallet(cur, user_id)
    if wallet["owner_id"] != user_id:
        raise PlanError("Only the account owner can manage billing and purchases.", "billing_owner_required")
    return wallet


def assert_no_open_order(cur, wallet_id, plans_only=False):
    cur.execute("""SELECT 1 FROM purchase_orders WHERE wallet_id=%s
                   AND status IN ('creating','pending','unknown','review')
                   AND (%s = FALSE OR kind='plan')""", (wallet_id, plans_only))
    if cur.fetchone():
        raise PlanError("Finish or cancel the pending purchase in Billing before continuing.",
                        "purchase_pending", 409)


def provider_request(method, path, attributes=None):
    key, _, _ = configuration()
    try:
        response = requests.request(method, "https://api.paymongo.com" + path,
                                    auth=(key, ""), json={"data": {"attributes": attributes}} if attributes else None,
                                    timeout=(5, 25), allow_redirects=False)
    except requests.RequestException as error:
        LOGGER.warning("PayMongo request unavailable (%s)", type(error).__name__)
        raise PlanError("PayMongo could not be reached. Check the purchase status before trying again.",
                        "gateway_unavailable", 503) from error
    if not 200 <= response.status_code < 300:
        LOGGER.warning("PayMongo request rejected (HTTP %s)", response.status_code)
        raise PlanError("PayMongo did not confirm the request. Recheck the order or contact support.",
                        "gateway_rejected", 502)
    try:
        resource = response.json()["data"]
        if (not isinstance(resource, dict) or not isinstance(resource.get("id"), str)
                or not isinstance(resource.get("attributes"), dict)):
            raise ValueError("Invalid resource")
        return resource
    except (ValueError, KeyError, TypeError) as error:
        raise PlanError("PayMongo returned an invalid response. Contact support with the order reference.",
                        "gateway_invalid", 502) from error


def valid_session_id(value):
    return isinstance(value, str) and re.fullmatch(r"cs_[A-Za-z0-9]+", value) is not None


def safe_checkout_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.netloc == "checkout.paymongo.com"
            and not parsed.username and not parsed.password)


def public_order(order):
    return {key: order[key] for key in (
        "id", "kind", "plan_id", "months", "amount", "credits", "livemode", "status",
        "checkout_url", "created_at", "paid_at", "fulfilled_at", "message")}


def overview(dsn, user_id):
    with billing.transaction(dsn) as cur:
        wallet = owner_wallet(cur, user_id)
        can_start_trial = (billing.trial_eligible(cur, wallet, user_id)
                           and billing.email_verified(cur, user_id)
                           and billing.trial_network_free(cur, user_id))
        cur.execute("SELECT * FROM purchase_orders WHERE wallet_id=%s ORDER BY created_at DESC LIMIT 25",
                    (wallet["id"],))
        orders = [public_order(row) for row in cur.fetchall()]
    summary = billing.wallet_summary(dsn, user_id)
    ready, live = checkout_ready()
    return {"account": summary, "orders": orders, "ready": ready, "livemode": live,
            "can_start_trial": can_start_trial}


def create_checkout(dsn, user_id, payload, request_key):
    key, _, base = configuration()
    try:
        request_key = str(uuid.UUID(str(request_key)))
    except (ValueError, TypeError, AttributeError) as error:
        raise PlanError("A valid purchase Idempotency-Key UUID is required.", "invalid_request_key", 400) from error
    if not isinstance(payload, dict):
        raise PlanError("Choose a plan or credit pack.", "invalid_purchase", 400)
    kind, plan_id, months = payload.get("kind"), payload.get("plan_id"), payload.get("months", 1)
    if (kind not in ("plan", "topup") or not isinstance(plan_id, str) or plan_id not in PRICES
            or type(months) is not int or months not in (1, 12) or (kind == "topup" and months != 1)):
        raise PlanError("Choose a valid plan, billing period, or top-up.", "invalid_purchase", 400)
    with billing.transaction(dsn) as cur:
        wallet = owner_wallet(cur, user_id)
        cur.execute("SELECT * FROM purchase_orders WHERE user_id=%s AND request_key=%s", (user_id, request_key))
        previous = cur.fetchone()
        if previous:
            if (previous["kind"], previous["plan_id"], previous["months"]) != (kind, plan_id, months):
                raise PlanError("This request key belongs to a different purchase.", "request_conflict", 409)
            return public_order(previous)
        assert_no_open_order(cur, wallet["id"])
        billing.refresh_wallet(cur, wallet)
        active = billing.is_active(wallet)
        cur.execute("SELECT 1 FROM paid_discovery_runs WHERE wallet_id=%s AND settled_at IS NULL", (wallet["id"],))
        if cur.fetchone():
            raise PlanError("Wait for searches to finish before starting a purchase.", "discovery_busy", 409)
        if kind == "topup":
            if not active or wallet["plan_id"] != plan_id or plan_id not in PRICES:
                raise PlanError("Top-ups require your current active paid plan.", "topup_unavailable")
        else:
            if active and wallet["plan_id"] != "trial":
                raise PlanError("Active paid-plan changes require support. Renew after expiry or buy a top-up.",
                                "active_subscription", 409)
            usage = seat_usage(cur, wallet["id"])
            if usage["members"] + usage["pending"] > PLANS[plan_id].seats:
                raise PlanError("Remove excess members and pending invitations before purchasing this plan.",
                                "seat_limit", 409)
        order_id = str(uuid.uuid4())
        amount = PRICES[plan_id]["topup" if kind == "topup" else months]
        credits = PRICES[plan_id]["pack"] if kind == "topup" else PLANS[plan_id].credits
        cur.execute("""INSERT INTO purchase_orders
                       (id,wallet_id,user_id,request_key,kind,plan_id,months,amount,credits,livemode,status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'creating') RETURNING *""",
                    (order_id, wallet["id"], user_id, request_key, kind, plan_id, months,
                     amount, credits, key.startswith("sk_live_")))
        order = dict(cur.fetchone())
    label = (f"{PLANS[plan_id].name} - {credits:,} search credits" if kind == "topup"
             else f"{PLANS[plan_id].name} - {'Annual' if months == 12 else 'Monthly'} plan")
    try:
        resource = provider_request("POST", "/v2/checkout_sessions", {
            "line_items": [{"name": label, "amount": amount, "currency": "PHP", "quantity": 1}],
            "payment_method_types": ["qrph"], "reference_number": order_id,
            "metadata": {"order_id": order_id}, "send_email_receipt": True,
            "show_line_items": True, "show_description": True, "pass_on_fees": False,
            "description": "One-time payment. No automatic renewal.",
            "success_url": f"{base}/billing?order={order_id}&checkout=success",
            "cancel_url": f"{base}/billing?order={order_id}&checkout=cancel",
        })
        attrs = resource["attributes"]
        if (not valid_session_id(resource["id"]) or not safe_checkout_url(attrs.get("checkout_url"))
                or attrs.get("livemode") is not order["livemode"]):
            raise PlanError("PayMongo checkout details did not match. Contact support before paying.",
                            "gateway_invalid", 502)
        with billing.transaction(dsn) as cur:
            cur.execute("""UPDATE purchase_orders SET session_id=COALESCE(session_id,%s),checkout_url=%s,
                           status=CASE WHEN status='creating' THEN 'pending' ELSE status END
                           WHERE id=%s AND (session_id IS NULL OR session_id=%s) RETURNING *""",
                        (resource["id"], attrs["checkout_url"], order_id, resource["id"]))
            saved = cur.fetchone()
            if not saved:
                raise PlanError("Checkout reference conflict. Contact support.", "order_conflict", 409)
            return public_order(saved)
    except PlanError:
        # A timed-out POST may have created a payable session. Never blindly retry it.
        with billing.transaction(dsn) as cur:
            cur.execute("""UPDATE purchase_orders SET status='unknown',
                           message='Checkout creation was not confirmed. Support must reconcile this reference before another purchase.'
                           WHERE id=%s AND status='creating'""", (order_id,))
        raise


def owned_order(dsn, user_id, order_id):
    with billing.transaction(dsn) as cur:
        wallet = owner_wallet(cur, user_id)
        cur.execute("SELECT * FROM purchase_orders WHERE id=%s AND wallet_id=%s", (str(order_id), wallet["id"]))
        order = cur.fetchone()
        if not order:
            raise PlanError("Purchase not found.", "not_found", 404)
        return dict(order)


def validate_session(order, resource):
    if not isinstance(resource, dict) or not isinstance(resource.get("attributes"), dict):
        raise PlanError("Invalid checkout confirmation.", "payment_mismatch", 409)
    attrs = resource["attributes"]
    if (not valid_session_id(resource.get("id")) or resource.get("type") != "checkout_session"
            or (order["session_id"] and resource["id"] != order["session_id"])
            or attrs.get("reference_number") != str(order["id"])
            or attrs.get("livemode") is not order["livemode"]):
        raise PlanError("Payment reference or mode did not match this order.", "payment_mismatch", 409)
    items = attrs.get("line_items")
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or type(items[0].get("amount")) is not int or items[0]["amount"] != order["amount"]
            or items[0].get("currency") != "PHP" or type(items[0].get("quantity")) is not int
            or items[0]["quantity"] != 1):
        raise PlanError("Payment amount or currency did not match this order.", "payment_mismatch", 409)
    return attrs


def apply_session(dsn, order_id, resource):
    with billing.transaction(dsn) as cur:
        cur.execute("SELECT wallet_id FROM purchase_orders WHERE id=%s", (str(order_id),))
        found = cur.fetchone()
        if not found:
            raise PlanError("Purchase not found.", "not_found", 404)
        cur.execute("SELECT * FROM team_wallets WHERE id=%s FOR UPDATE", (found["wallet_id"],))
        wallet = dict(cur.fetchone())
        cur.execute("SELECT * FROM purchase_orders WHERE id=%s FOR UPDATE", (str(order_id),))
        order = dict(cur.fetchone())
        attrs = validate_session(order, resource)
        if order["status"] in ("paid", "review"):
            return public_order(order)
        payments = attrs.get("payments", [])
        if not isinstance(payments, list):
            raise PlanError("Invalid payment confirmation.", "payment_mismatch", 409)
        paid = [p for p in payments if isinstance(p, dict)
                and isinstance(p.get("attributes"), dict) and p["attributes"].get("status") == "paid"]
        if not paid:
            if attrs.get("status") == "expired":
                cur.execute("UPDATE purchase_orders SET status='expired',message=NULL WHERE id=%s RETURNING *",
                            (str(order_id),))
                return public_order(cur.fetchone())
            return public_order(order)
        if len(paid) != 1:
            raise PlanError("Unexpected payment count. Contact support.", "payment_mismatch", 409)
        payment, details = paid[0], paid[0]["attributes"]
        if (not isinstance(payment.get("id"), str) or not payment["id"].startswith("pay_")
                or type(details.get("amount")) is not int or details["amount"] != order["amount"]
                or details.get("currency") != "PHP" or details.get("livemode") is not order["livemode"]
                or details.get("refunded", False) or details.get("refunds")
                or details.get("disputed", False)):
            raise PlanError("Payment amount, mode or state did not match.", "payment_mismatch", 409)
        review = None
        active = billing.is_active(wallet)
        if order["kind"] == "plan":
            cur.execute("SELECT 1 FROM paid_discovery_runs WHERE wallet_id=%s AND settled_at IS NULL", (wallet["id"],))
            if cur.fetchone():
                raise PlanError("Payment received; waiting for discovery settlement. Recheck shortly.",
                                "billing_busy", 503)
            usage = seat_usage(cur, wallet["id"])
            if active and wallet["plan_id"] != "trial":
                review = "Payment received, but another paid plan is active. Contact support for reconciliation."
            elif usage["members"] + usage["pending"] > PLANS[order["plan_id"]].seats:
                review = "Payment received, but team seats exceed the purchased plan. Contact support."
        if not review:
            event = f"purchase:{order['id']}"
            if order["kind"] == "topup":
                billing.refresh_wallet(cur, wallet)
                billing.ledger(cur, wallet, order["user_id"], event, Decimal(order["credits"]),
                               "PayMongo QR Ph credit top-up")
            else:
                billing.activate_wallet(cur, wallet, order["user_id"], order["plan_id"], order["months"],
                                        f"activate:{event}", "PayMongo QR Ph plan purchase", discard_trial=True)
        cur.execute("""UPDATE purchase_orders SET session_id=%s,payment_id=%s,paid_at=NOW(),
                       fulfilled_at=CASE WHEN %s THEN NOW() ELSE NULL END,status=%s,message=%s
                       WHERE id=%s RETURNING *""",
                    (resource["id"], payment["id"], not review, "review" if review else "paid",
                     review, str(order_id)))
        return public_order(cur.fetchone())


def recheck(dsn, user_id, order_id, cancel=False):
    order = owned_order(dsn, user_id, order_id)
    if order["status"] in ("paid", "review", "expired", "failed"):
        return public_order(order)
    if not order["session_id"]:
        raise PlanError("Checkout reference is not yet available. Contact support with the order ID.",
                        "purchase_unknown", 409)
    path = f"/v1/checkout_sessions/{order['session_id']}"
    if cancel:
        # Never release the local slot until PayMongo confirms expiry; a return URL is not cancellation.
        provider_request("POST", path + "/expire")
    return apply_session(dsn, order_id, provider_request("GET", path))


def verify_webhook(raw, signature):
    key, secret, _ = configuration()
    try:
        parts = dict(part.strip().split("=", 1) for part in signature.split(","))
        timestamp = parts["t"]
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError("Stale signature")
        digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, parts["li" if key.startswith("sk_live_") else "te"]):
            raise ValueError("Invalid signature")
        event = json.loads(raw)["data"]
        if not isinstance(event, dict) or not isinstance(event.get("attributes"), dict):
            raise ValueError("Invalid event")
        attrs = event["attributes"]
        if attrs.get("livemode") is not key.startswith("sk_live_"):
            raise ValueError("Mode mismatch")
        return attrs
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise PlanError("Webhook signature or event is invalid.", "invalid_webhook", 400) from error


def receive_webhook(dsn, raw, signature):
    event = verify_webhook(raw, signature)
    if event.get("type") != "checkout_session.payment.paid":
        return
    resource = event.get("data")
    if not isinstance(resource, dict) or not valid_session_id(resource.get("id")):
        raise PlanError("Invalid checkout event.", "invalid_webhook", 400)
    attrs = resource.get("attributes")
    if not isinstance(attrs, dict):
        raise PlanError("Invalid checkout event.", "invalid_webhook", 400)
    try:
        order_id = str(uuid.UUID(str(attrs.get("reference_number"))))
    except (ValueError, TypeError, AttributeError):
        LOGGER.info("Ignoring PayMongo checkout outside this application's order references")
        return
    with billing.transaction(dsn) as cur:
        cur.execute("SELECT 1 FROM purchase_orders WHERE id=%s", (order_id,))
        if not cur.fetchone():
            LOGGER.info("Ignoring PayMongo checkout without a matching local order")
            return
    # Retrieve from the authenticated API too; never grant from redirect/query data.
    confirmed = provider_request("GET", f"/v1/checkout_sessions/{resource['id']}")
    if confirmed["id"] != resource["id"]:
        raise PlanError("Checkout session did not match the notification.", "payment_mismatch", 409)
    result = apply_session(dsn, order_id, confirmed)
    if result["status"] not in ("paid", "review"):
        raise PlanError("Payment is not confirmed yet. Retry notification.", "payment_pending", 503)
