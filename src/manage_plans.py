"""Operator-only overrides. Purchases use verified PayMongo payment fulfillment."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import os

from dotenv import load_dotenv

from auth import email_key
from billing import activate_wallet, is_active, ledger, lock_wallet, refresh_wallet, settle, transaction
from plans import PLANS, PlanError
from teams import seat_usage


def activate(dsn, address, plan_id, months=1):
    if plan_id not in PLANS or plan_id == "unassigned" or months not in (1, 12):
        raise PlanError("Choose trial, solo, small_team, or enterprise; months must be 1 or 12.")
    with transaction(dsn) as cur:
        cur.execute("SELECT id FROM users WHERE email_key=%s AND is_active", (email_key(address),))
        user = cur.fetchone()
        if not user:
            raise PlanError("Account not found.")
        wallet = lock_wallet(cur, user["id"])
        from payments import assert_no_open_order
        assert_no_open_order(cur, wallet["id"])
        if wallet["owner_id"] != user["id"]:
            raise PlanError("A team member cannot activate a separate subscription.")
        if is_active(wallet):
            raise PlanError("The current subscription is active. Use change-plan for an upgrade/downgrade.")
        cur.execute("SELECT 1 FROM paid_discovery_runs WHERE wallet_id=%s AND settled_at IS NULL", (wallet["id"],))
        if cur.fetchone():
            raise PlanError("Settle outstanding runs before activation.")
        usage = seat_usage(cur, wallet["id"])
        if usage["members"] + usage["pending"] > PLANS[plan_id].seats:
            raise PlanError("Remove excess members and revoke pending invitations before changing plans.")
        if plan_id == "trial":
            cur.execute("SELECT 1 FROM credit_ledger WHERE event_key=%s", (f"trial:{wallet['id']}",))
            if cur.fetchone():
                raise PlanError("This owner has already received a trial.")
        key = (f"trial:{wallet['id']}" if plan_id == "trial"
               else f"activate:{wallet['id']}:{datetime.now(timezone.utc).isoformat()}")
        activate_wallet(cur, wallet, user["id"], plan_id, months, key, "Manual plan activation")


def change_plan(dsn, address, plan_id):
    if plan_id not in ("solo", "small_team", "enterprise"):
        raise PlanError("Choose a paid plan.")
    with transaction(dsn) as cur:
        cur.execute("SELECT id FROM users WHERE email_key=%s", (email_key(address),))
        user = cur.fetchone()
        if not user:
            raise PlanError("Account not found.")
        wallet = lock_wallet(cur, user["id"])
        from payments import assert_no_open_order
        assert_no_open_order(cur, wallet["id"])
        if wallet["owner_id"] != user["id"] or not is_active(wallet) or wallet["plan_id"] == "trial":
            raise PlanError("Only an active paid owner can change plan.")
        refresh_wallet(cur, wallet)
        usage = seat_usage(cur, wallet["id"])
        if usage["members"] + usage["pending"] > PLANS[plan_id].seats:
            raise PlanError("Remove excess members and revoke invitations first.")
        cur.execute("SELECT 1 FROM paid_discovery_runs WHERE wallet_id=%s AND settled_at IS NULL", (wallet["id"],))
        if cur.fetchone():
            raise PlanError("Wait for outstanding searches to settle.")
        cur.execute("UPDATE team_wallets SET plan_id=%s WHERE id=%s", (plan_id, wallet["id"]))
        ledger(cur, wallet, user["id"], f"plan-change:{wallet['id']}:{datetime.now(timezone.utc).isoformat()}",
               Decimal(0), f"Plan changed to {plan_id}; balance and billing dates retained")


def top_up(dsn, address, key):
    packs = {"solo": 350, "small_team": 750, "enterprise": 2000}
    with transaction(dsn) as cur:
        cur.execute("SELECT id FROM users WHERE email_key=%s", (email_key(address),))
        user = cur.fetchone()
        if not user:
            raise PlanError("Account not found.")
        wallet = lock_wallet(cur, user["id"])
        from payments import assert_no_open_order
        assert_no_open_order(cur, wallet["id"])
        if wallet["owner_id"] != user["id"] or not is_active(wallet) or wallet["plan_id"] not in packs:
            raise PlanError("Only active paid owners can receive top-ups.")
        refresh_wallet(cur, wallet)
        event = f"topup:{key}"
        cur.execute("SELECT wallet_id FROM credit_ledger WHERE event_key=%s", (event,))
        previous = cur.fetchone()
        if previous:
            if previous["wallet_id"] != wallet["id"]:
                raise PlanError("That payment reference belongs to a different wallet.")
            return
        ledger(cur, wallet, user["id"], event, Decimal(packs[wallet["plan_id"]]), "Confirmed manual top-up")


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("activate", "change-plan"):
        sub = subs.add_parser(name)
        sub.add_argument("email")
        sub.add_argument("plan", choices=["trial", "solo", "small_team", "enterprise"] if name == "activate"
                         else ["solo", "small_team", "enterprise"])
        if name == "activate":
            sub.add_argument("--months", type=int, choices=[1, 12], default=1)
    top = subs.add_parser("top-up")
    top.add_argument("email")
    top.add_argument("--payment-reference", required=True)
    recover = subs.add_parser("recover")
    recover.add_argument("run_id")
    recover.add_argument("--workers-stopped", action="store_true", required=True)
    args = parser.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        parser.error("DATABASE_URL is required.")
    try:
        if args.command == "activate":
            activate(dsn, args.email, args.plan, args.months)
        elif args.command == "change-plan":
            change_plan(dsn, args.email, args.plan)
        elif args.command == "top-up":
            top_up(dsn, args.email, args.payment_reference)
        else:
            settle(dsn, args.run_id, interrupted=True)
    except PlanError as error:
        parser.error(str(error))
    print("Completed. The operation has been persisted.")


if __name__ == "__main__":
    main()
