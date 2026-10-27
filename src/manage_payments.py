"""Reconcile PayMongo orders with authenticated provider data; never blindly grant credits."""

import argparse
import os
import uuid

from dotenv import load_dotenv

import billing
import payments
from plans import PlanError


def reconcile(dsn, order_id, session_id=None, expire=False):
    with billing.transaction(dsn) as cur:
        cur.execute("SELECT * FROM purchase_orders WHERE id=%s", (str(order_id),))
        order = cur.fetchone()
        if not order:
            raise PlanError("Purchase not found.")
        order = dict(order)
    session_id = session_id or order["session_id"]
    if not payments.valid_session_id(session_id):
        raise PlanError("Supply the checkout session ID from PayMongo, matched by the order reference.")
    path = f"/v1/checkout_sessions/{session_id}"
    resource = payments.provider_request("GET", path)
    payments.validate_session(order, resource)
    if expire:
        payments.provider_request("POST", path + "/expire")
        resource = payments.provider_request("GET", path)
    with billing.transaction(dsn) as cur:
        cur.execute("SELECT id FROM team_wallets WHERE id=%s FOR UPDATE", (order["wallet_id"],))
        cur.execute("SELECT * FROM purchase_orders WHERE id=%s FOR UPDATE", (str(order_id),))
        current = dict(cur.fetchone())
        payments.validate_session(current, resource)
        url = resource["attributes"].get("checkout_url")
        if url is not None and not payments.safe_checkout_url(url):
            raise PlanError("Invalid checkout URL from provider.")
        cur.execute("""UPDATE purchase_orders SET session_id=%s,checkout_url=COALESCE(%s,checkout_url),
                       status=CASE WHEN status IN ('creating','unknown','failed') THEN 'pending' ELSE status END,
                       message=CASE WHEN status IN ('creating','unknown','failed') THEN NULL ELSE message END
                       WHERE id=%s""", (session_id, url, str(order_id)))
    return payments.apply_session(dsn, order_id, resource)


def close_uncreated(dsn, order_id):
    with billing.transaction(dsn) as cur:
        cur.execute("""UPDATE purchase_orders SET status='failed',
                       message='Operator verified no payable checkout exists in PayMongo.'
                       WHERE id=%s AND status IN ('creating','unknown') AND session_id IS NULL
                       AND payment_id IS NULL RETURNING id""", (str(order_id),))
        if not cur.fetchone():
            raise PlanError("Only an unconfirmed creation without a session or payment can be closed.")


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    check = subs.add_parser("reconcile")
    check.add_argument("order_id", type=uuid.UUID)
    check.add_argument("--session-id")
    check.add_argument("--expire", action="store_true")
    close = subs.add_parser("close-uncreated")
    close.add_argument("order_id", type=uuid.UUID)
    close.add_argument("--verified-no-payable-session", action="store_true", required=True)
    args = parser.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        parser.error("DATABASE_URL is required.")
    try:
        if args.command == "reconcile":
            result = reconcile(dsn, args.order_id, args.session_id, args.expire)
            print(f"Order {result['id']}: {result['status']}")
        else:
            close_uncreated(dsn, args.order_id)
            print("Uncreated order closed after operator verification.")
    except PlanError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
