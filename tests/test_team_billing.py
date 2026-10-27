"""Real PostgreSQL tests; TEST_BILLING_DSN must point to a disposable test server.

Each run owns a new schema. No existing application tables are modified.
"""

from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import io
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
from unittest.mock import patch
from unittest.mock import Mock

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import account_email
import billing
import payments
from hiring_services import BUILTIN_SERVICES
from manage_plans import activate, change_plan, top_up
from auth import AuthError
from plans import PlanError, quote_search
import teams
from test_discovery_runs import ImmediateThread


class FakePayMongo:
    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.on_create = None

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/v2/checkout_sessions"):
            attrs = dict(kwargs["json"]["data"]["attributes"], livemode=False, status="active", payments=[])
            session_id = "cs_" + uuid.uuid4().hex
            attrs["checkout_url"] = f"https://checkout.paymongo.com/{session_id}"
            resource = {"id": session_id, "type": "checkout_session", "attributes": attrs}
            self.sessions[session_id] = resource
            if self.on_create:
                self.on_create(resource)
        else:
            session_id = url.split("/")[-2] if url.endswith("/expire") else url.split("/")[-1]
            resource = self.sessions[session_id]
            if method == "POST":
                if resource["attributes"]["payments"]:
                    return Mock(status_code=400)
                resource["attributes"]["status"] = "expired"
        return Mock(status_code=200, json=Mock(return_value={"data": resource}))

    def pay(self, order_id):
        resource = next(s for s in self.sessions.values()
                        if s["attributes"]["reference_number"] == str(order_id))
        resource["attributes"]["status"] = "active"
        resource["attributes"]["payments"] = [{
            "id": "pay_" + uuid.uuid4().hex, "type": "payment",
            "attributes": {"status": "paid", "amount": resource["attributes"]["line_items"][0]["amount"],
                           "currency": "PHP", "livemode": False, "refunds": [], "disputed": False},
        }]
        return resource


class PlanCalculationTests(unittest.TestCase):
    def test_system_invitations_use_resend_tls_and_fragment_tokens(self):
        with (
            patch.dict(os.environ, {"SYSTEM_SMTP_PASSWORD": "fake-secret",
                                   "SYSTEM_SMTP_FROM": "system@example.test",
                                   "APP_BASE_URL": "https://app.example.test"}),
            patch("teams.smtplib.SMTP_SSL") as smtp,
        ):
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            teams.send_invitation("guest@example.test", "safe-token")
            self.assertEqual(smtp.call_args.args, ("smtp.resend.com", 465))
            connection = smtp.return_value.__enter__.return_value
            connection.login.assert_called_once_with("resend", "fake-secret")
            message = connection.send_message.call_args.args[0]
            self.assertEqual(message["To"], "guest@example.test")
            self.assertIn("https://app.example.test/team-invitation#safe-token", message.get_content())
            self.assertNotIn("fake-secret", str(message))

    def test_local_limits_prices_and_locked_sources(self):
        for plan, limit in [("trial", 1), ("solo", 3), ("small_team", 5), ("enterprise", 10)]:
            data = {"categories": [f"Industry {n}" for n in range(limit)], "sources": ["google"]}
            self.assertEqual(quote_search(plan, True, "local", data), Decimal(10 * limit))
            data["categories"].append("Extra")
            with self.assertRaises(PlanError):
                quote_search(plan, True, "local", data)
        with self.assertRaises(PlanError):
            quote_search("solo", True, "local", {"categories": ["Law"], "sources": ["google", "facebook"]})
        self.assertEqual(quote_search("solo", True, "local",
                                     {"categories": ["Law", "Law"], "sources": ["google", "yelp"]}), 10)
        self.assertEqual(quote_search("solo", True, "local", {"categories": ["x" * 60]}), 10)
        with self.assertRaises(PlanError) as raised:
            quote_search("solo", True, "local", {"categories": ["x" * 61]})
        self.assertEqual(raised.exception.status, 400)

    def test_hiring_prices_depth_and_custom_service_restrictions(self):
        body = {"service_types": ["video_editing"], "pages_per_role": 3}
        self.assertEqual(quote_search("small_team", True, "hiring", body, BUILTIN_SERVICES), 180)
        body["pages_per_role"] = 5
        self.assertEqual(quote_search("enterprise", True, "hiring", body, BUILTIN_SERVICES), 300)
        for plan in ["solo", "small_team"]:
            with self.assertRaises(PlanError):
                quote_search(plan, True, "hiring", body, BUILTIN_SERVICES)
        with self.assertRaises(PlanError):
            quote_search("enterprise", False, "hiring", body, BUILTIN_SERVICES)
        with self.assertRaises(PlanError):
            quote_search("small_team", True, "hiring",
                         {"service_types": ["custom_test"], "pages_per_role": 1}, {})


@unittest.skipUnless(os.getenv("TEST_BILLING_DSN"), "Set TEST_BILLING_DSN for isolated PostgreSQL integration tests")
class TeamBillingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        patch.stopall()
        cls.root_dsn = os.environ["TEST_BILLING_DSN"]
        cls.schema = "team_billing_test_" + uuid.uuid4().hex
        with psycopg2.connect(cls.root_dsn) as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.dsn = make_dsn(cls.root_dsn, options=f"-csearch_path={cls.schema}")
        with psycopg2.connect(cls.dsn) as conn, conn.cursor() as cur:
            cur.execute((Path(__file__).resolve().parents[1] / "schema.sql").read_text())
            cur.execute(account_email.SCHEMA)
            cur.execute(account_email.SCHEMA)  # Migration is idempotent.
            cur.execute(billing.SCHEMA)
            cur.execute(payments.SCHEMA)
            cur.execute(payments.SCHEMA)
            cur.execute(billing.SCHEMA)  # Migration is idempotent.
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"DATABASE_URL": "", "APP_ENV": "local"}):
            cls.api = importlib.import_module("control_gate_api")

    @classmethod
    def tearDownClass(cls):
        with psycopg2.connect(cls.root_dsn) as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        patch.stopall()
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        if hasattr(billing.CONTEXT, "run_id"):
            del billing.CONTEXT.run_id
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            for name in ["owner", "member", "other", "fourth", "fifth"]:
                cur.execute("""INSERT INTO users(email,email_key,password_hash,email_verified_at)
                               VALUES (%s,%s,'unused',NOW()) RETURNING id""", (f"{name}@example.test", f"{name}@example.test"))
                setattr(self, name, cur.fetchone()[0])
        patch.object(self.api, "DB_DSN", self.dsn).start()
        patch.dict(self.api.app.config, {"BILLING_DSN": self.dsn}).start()
        patch.dict(os.environ, {"SYSTEM_SMTP_PASSWORD": "fake-key", "SYSTEM_SMTP_FROM": "system@example.test",
                               "APP_BASE_URL": "https://app.example.test"}).start()
        patch("requests.get", side_effect=AssertionError("Live provider request forbidden")).start()
        self.gateway = FakePayMongo()
        patch("requests.request", self.gateway).start()
        patch.dict(os.environ, {"PAYMONGO_SECRET_KEY": "sk_test_offline",
                               "PAYMONGO_WEBHOOK_SECRET": "offline-hook"}).start()
        self.mail = patch("teams.send_invitation").start()
        self.addCleanup(patch.stopall)

    def client(self, user_id):
        client = self.api.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["csrf_token"] = "test-csrf"
        client.environ_base["HTTP_X_CSRF_TOKEN"] = "test-csrf"
        client.environ_base["HTTP_IDEMPOTENCY_KEY"] = str(uuid.uuid4())
        return client

    def team(self, plan="small_team"):
        activate(self.dsn, "owner@example.test", plan)
        return billing.wallet_summary(self.dsn, self.owner)

    def join(self, member="member"):
        teams.invite_member(self.dsn, self.owner, f"{member}@example.test")
        token = self.mail.call_args.args[1]
        teams.accept_invitation(self.dsn, getattr(self, member), token)
        return token

    def reserve(self, user_id=None, mode="local", body=None, key=None):
        return billing.reserve(self.dsn, user_id or self.owner, mode,
                               body or {"categories": ["Law Firm"], "sources": ["google"]},
                               key or str(uuid.uuid4()))

    def buy(self, plan_id="solo", months=1, kind="plan", key=None):
        return payments.create_checkout(self.dsn, self.owner,
                                        {"kind": kind, "plan_id": plan_id, "months": months},
                                        key or str(uuid.uuid4()))

    def notify(self, resource):
        raw = json.dumps({"data": {"id": "evt_test", "type": "event", "attributes": {
            "type": "checkout_session.payment.paid", "livemode": False, "data": resource,
        }}}).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new(b"offline-hook", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return self.api.app.test_client().post("/api/paymongo/webhook", data=raw,
            content_type="application/json", headers={"Paymongo-Signature": f"t={timestamp},te={digest},li="})

    def test_purchase_page_owner_access_and_unconfigured_checkout(self):
        client = self.client(self.owner)
        response = client.get("/billing")
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("PayMongo test mode", response.text)
        self.assertIn("QR Ph", response.text)
        self.assertIn('data-buy-plan="solo"', response.text)
        with patch.dict(os.environ, {"PAYMONGO_SECRET_KEY": ""}):
            self.assertIn("Payments are not configured", client.get("/billing").text)
            result = client.post("/api/billing/checkout", json={"kind": "plan", "plan_id": "solo"})
            self.assertEqual(result.status_code, 503)
        self.assertFalse(self.gateway.calls)
        self.assertEqual(self.api.app.test_client().get("/api/billing").status_code, 401)
        self.assertEqual(client.post("/api/billing/checkout", json={},
                                    headers={"X-CSRF-Token": "bad"}).status_code, 403)

    def test_purchase_prices_qrph_only_return_urls_and_idempotent_creation(self):
        key = str(uuid.uuid4())
        first = self.buy(key=key)
        second = self.buy(key=key)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["amount"], 99900)
        self.assertEqual(len(self.gateway.calls), 1)
        method, url, kwargs = self.gateway.calls[0]
        self.assertTrue(url.endswith("/v2/checkout_sessions"))
        attrs = kwargs["json"]["data"]["attributes"]
        self.assertEqual(attrs["payment_method_types"], ["qrph"])
        self.assertFalse(attrs["pass_on_fees"])
        self.assertEqual(attrs["line_items"][0]["amount"], 99900)
        self.assertEqual(attrs["success_url"], f"https://app.example.test/billing?order={first['id']}&checkout=success")
        with self.assertRaises(PlanError):
            self.buy("enterprise", key=key)
        with self.assertRaises(PlanError):
            self.buy()
        client = self.client(self.owner)
        self.assertEqual(client.get(f"/billing?order={first['id']}&checkout=success").status_code, 200)
        self.assertFalse(billing.wallet_summary(self.dsn, self.owner)["plan"]["active"])

    def test_paid_trial_upgrade_webhook_and_concurrent_replays_grant_exactly_once(self):
        activate(self.dsn, "owner@example.test", "trial")
        order = self.buy("small_team")
        with self.assertRaises(PlanError) as raised:
            self.reserve()
        self.assertEqual(raised.exception.code, "purchase_pending")
        resource = self.gateway.pay(order["id"])
        response = self.notify(resource)
        self.assertEqual(response.status_code, 200, response.json)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: payments.apply_session(self.dsn, order["id"], resource), range(4)))
        self.assertTrue(all(result["status"] == "paid" for result in results))
        self.assertEqual(self.notify(resource).status_code, 200)
        summary = billing.wallet_summary(self.dsn, self.owner)
        self.assertEqual(summary["balance"], "2500.00")
        self.assertEqual(summary["plan"]["id"], "small_team")
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM credit_ledger WHERE event_key=%s",
                        (f"activate:purchase:{order['id']}",))
            self.assertEqual(cur.fetchone()["n"], 1)

    def test_annual_purchase_grants_monthly_and_topup_is_shared_once(self):
        order = self.buy("small_team", months=12)
        self.assertEqual(order["amount"], 2499000)
        self.assertEqual(self.notify(self.gateway.pay(order["id"])).status_code, 200)
        summary = billing.wallet_summary(self.dsn, self.owner)
        self.assertEqual(summary["balance"], "2500.00")
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT starts_at,ends_at,next_grant_at FROM team_wallets WHERE owner_id=%s", (self.owner,))
            wallet = cur.fetchone()
            self.assertEqual(wallet["ends_at"].year, wallet["starts_at"].year + 1)
            self.assertLess(wallet["next_grant_at"], wallet["ends_at"])
        self.join()
        topup = self.buy("small_team", kind="topup")
        self.assertEqual(topup["amount"], 79900)
        resource = self.gateway.pay(topup["id"])
        self.assertEqual(self.notify(resource).status_code, 200)
        self.assertEqual(self.notify(resource).status_code, 200)
        self.assertEqual(billing.wallet_summary(self.dsn, self.member)["balance"], "3250.00")

    def test_member_billing_bypasses_and_active_plan_changes_are_blocked(self):
        self.team()
        self.join()
        client = self.client(self.member)
        self.assertEqual(client.get("/billing").status_code, 403)
        self.assertEqual(client.get("/api/billing").status_code, 403)
        response = client.post("/api/billing/checkout", json={"kind": "topup", "plan_id": "small_team"})
        self.assertEqual(response.status_code, 403)
        for plan_id in ("solo", "small_team", "enterprise"):
            with self.assertRaises(PlanError):
                self.buy(plan_id)
        with self.assertRaises(PlanError):
            self.buy("solo", kind="topup")
        self.assertFalse(self.gateway.calls)
        order = self.buy("small_team", kind="topup")
        self.assertEqual(client.post(f"/api/billing/orders/{order['id']}/recheck").status_code, 403)
        self.assertEqual(self.client(self.other).post(f"/api/billing/orders/{order['id']}/cancel").status_code, 404)

    def test_payment_mismatches_and_unsigned_notifications_never_activate(self):
        order = self.buy()
        resource = self.gateway.pay(order["id"])
        unsigned = self.api.app.test_client().post("/api/paymongo/webhook", json={"data": {}})
        self.assertEqual(unsigned.status_code, 400)
        attrs = resource["attributes"]["payments"][0]["attributes"]
        for field, invalid in [("amount", 1), ("currency", "USD"), ("livemode", True),
                               ("refunds", [{"id": "ref_test"}]), ("disputed", True)]:
            original = attrs[field]
            attrs[field] = invalid
            response = self.notify(resource)
            self.assertEqual(response.status_code, 409, response.json)
            self.assertFalse(billing.wallet_summary(self.dsn, self.owner)["plan"]["active"])
            attrs[field] = original
        self.assertEqual(self.notify(resource).status_code, 200)

    def test_cancel_requires_remote_expiry_and_paid_cancel_race_rechecks_safely(self):
        order = self.buy()
        result = payments.recheck(self.dsn, self.owner, order["id"], cancel=True)
        self.assertEqual(result["status"], "expired")
        self.assertFalse(billing.wallet_summary(self.dsn, self.owner)["plan"]["active"])
        order = self.buy()
        self.gateway.pay(order["id"])
        with self.assertRaises(PlanError):
            payments.recheck(self.dsn, self.owner, order["id"], cancel=True)
        self.assertEqual(payments.recheck(self.dsn, self.owner, order["id"])["status"], "paid")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "750.00")

    def test_unknown_creation_is_not_reposted_and_operator_recovers_verified_session(self):
        from manage_payments import reconcile, close_uncreated
        import requests
        key = str(uuid.uuid4())
        def timeout(resource):
            raise requests.Timeout("Lost checkout response")
        self.gateway.on_create = timeout
        with self.assertRaises(PlanError):
            self.buy(key=key)
        previous = self.buy(key=key)
        self.assertEqual(previous["status"], "unknown")
        self.assertEqual(len(self.gateway.calls), 1)
        with self.assertRaises(PlanError):
            self.buy()
        session_id = next(iter(self.gateway.sessions))
        self.gateway.on_create = None
        restored = reconcile(self.dsn, previous["id"], session_id)
        self.assertEqual(restored["status"], "pending")
        with self.assertRaises(PlanError):
            close_uncreated(self.dsn, previous["id"])
        self.assertEqual(self.notify(self.gateway.pay(previous["id"])).status_code, 200)

    def test_webhook_can_arrive_before_checkout_creation_response(self):
        statuses = []
        def webhook(resource):
            paid = self.gateway.pay(resource["attributes"]["reference_number"])
            statuses.append(self.notify(paid).status_code)
        self.gateway.on_create = webhook
        result = self.buy()
        self.assertEqual(statuses, [200])
        self.assertEqual(result["status"], "paid")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "750.00")

    def test_pending_plan_checkout_blocks_manual_activation_and_team_join(self):
        activate(self.dsn, "other@example.test", "small_team")
        order = self.buy()
        with self.assertRaises(PlanError):
            activate(self.dsn, "owner@example.test", "solo")
        teams.invite_member(self.dsn, self.other, "owner@example.test")
        token = self.mail.call_args.args[1]
        with self.assertRaises(PlanError):
            teams.accept_invitation(self.dsn, self.owner, token)
        payments.recheck(self.dsn, self.owner, order["id"], cancel=True)
        teams.accept_invitation(self.dsn, self.owner, token)
        self.assertFalse(billing.wallet_summary(self.dsn, self.owner)["owner"])

    def test_concurrent_checkout_creation_and_malicious_prices_cannot_duplicate_or_change_total(self):
        def create(n):
            try:
                return payments.create_checkout(self.dsn, self.owner, {
                    "kind": "plan", "plan_id": "enterprise", "months": 12,
                    "amount": 1, "credits": 999999, "currency": "USD",
                }, str(uuid.uuid4()))
            except PlanError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(4)))
        accepted = [result for result in results if result]
        self.assertEqual(len(accepted), 1)
        self.assertEqual((accepted[0]["amount"], accepted[0]["credits"]), (5999000, 7500))
        self.assertEqual(len(self.gateway.calls), 1)
        resource = self.gateway.pay(accepted[0]["id"])
        self.assertEqual(self.notify(resource).status_code, 200)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "7500.00")

    def test_renewal_retains_capped_rollover_and_late_topup_payment_is_honored(self):
        self.team("solo")
        topup = self.buy(kind="topup")
        with billing.transaction(self.dsn) as cur:
            cur.execute("UPDATE team_wallets SET ends_at=NOW()-INTERVAL '1 second' WHERE owner_id=%s", (self.owner,))
        self.assertEqual(self.notify(self.gateway.pay(topup["id"])).status_code, 200)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "1100.00")
        renewal = self.buy()
        self.assertEqual(self.notify(self.gateway.pay(renewal["id"])).status_code, 200)
        summary = billing.wallet_summary(self.dsn, self.owner)
        self.assertEqual(summary["balance"], "1100.00")
        self.assertEqual(summary["usage"]["next_event"], "expiry")
        self.assertTrue(summary["plan"]["active"])

    def test_signed_unrelated_checkout_is_acknowledged_without_fulfillment_or_provider_call(self):
        resource = {"id": "cs_other", "type": "checkout_session",
                    "attributes": {"reference_number": "other-app-reference", "livemode": False}}
        self.assertEqual(self.notify(resource).status_code, 200)
        resource["attributes"]["reference_number"] = str(uuid.uuid4())
        self.assertEqual(self.notify(resource).status_code, 200)
        self.assertFalse(self.gateway.calls)
        self.assertFalse(billing.wallet_summary(self.dsn, self.owner)["plan"]["active"])

    def test_paid_conflict_is_preserved_as_review_without_double_allowance(self):
        order = self.buy()
        # Simulate an out-of-band subscription change; normal operator commands are guarded.
        with billing.transaction(self.dsn) as cur:
            cur.execute("""UPDATE team_wallets SET plan_id='small_team',balance=2500,
                           starts_at=NOW(),ends_at=NOW()+INTERVAL '1 month'
                           WHERE owner_id=%s""", (self.owner,))
        resource = self.gateway.pay(order["id"])
        self.assertEqual(self.notify(resource).status_code, 200)
        purchase = payments.owned_order(self.dsn, self.owner, order["id"])
        self.assertEqual(purchase["status"], "review")
        self.assertTrue(purchase["message"])
        self.assertIsNotNone(purchase["payment_id"])
        self.assertIsNone(purchase["fulfilled_at"])
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2500.00")
        from manage_payments import reconcile
        self.assertEqual(reconcile(self.dsn, order["id"])["message"], purchase["message"])

    def test_existing_unassigned_keeps_leads_but_cannot_search(self):
        account = billing.wallet_summary(self.dsn, self.owner)
        self.assertFalse(account["plan"]["active"])
        self.assertEqual(account["usage"]["percent"], "0.0")
        self.assertIsNone(account["usage"]["next_date"])
        client = self.client(self.owner)
        self.assertEqual(client.get("/prospects").status_code, 200)
        with patch.object(self.api, "LOCAL_DISCOVERY_RUNS") as runs:
            response = client.post("/api/trigger/discover", json={
                "location": "Toronto", "categories": ["Law Firm"], "sources": ["yelp"],
            })
        self.assertEqual(response.status_code, 403)
        runs.start.assert_not_called()
        self.assertEqual(client.get("/user-management").status_code, 403)

    def register_and_verify(self, address, network=None):
        from auth import create_user
        account = create_user(self.dsn, address, "LocalPreviewOnly123!", "Trial", network_hash=network)
        with patch("system_mail.send") as send:
            self.assertTrue(account_email.send_verification(self.dsn, account["id"], "https://app.example.test"))
        body = send.call_args.args[2]
        self.assertIn("https://app.example.test/verify-email#", body)
        return account, body.split("/verify-email#", 1)[1].split()[0]

    def test_registration_waits_for_verification_then_grants_seven_day_trial_once(self):
        account, token = self.register_and_verify("newtrial@example.test")
        summary = billing.wallet_summary(self.dsn, account["id"])
        self.assertEqual(summary["balance"], "0.00")
        with self.assertRaises(PlanError) as raised:
            billing.claim_trial(self.dsn, account["id"])
        self.assertEqual((raised.exception.code, raised.exception.status), ("email_unverified", 403))
        self.assertEqual(self.client(account["id"]).post("/api/billing/trial").status_code, 403)
        self.assertEqual(account_email.verify_email(self.dsn, token), "started")
        with self.assertRaises(AuthError):
            account_email.verify_email(self.dsn, token)
        summary = billing.wallet_summary(self.dsn, account["id"])
        self.assertEqual(summary["plan"]["id"], "trial")
        self.assertEqual(summary["balance"], "30.00")
        self.assertEqual(summary["plan"]["sources"], ("google",))
        self.assertFalse(account_email.send_verification(self.dsn, account["id"], "https://x.test"))
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT ends_at-starts_at AS duration FROM team_wallets WHERE owner_id=%s", (account["id"],))
            self.assertEqual(cur.fetchone()["duration"], timedelta(days=7))
        with self.assertRaises(PlanError):
            billing.claim_trial(self.dsn, account["id"])
        self.team()
        teams.invite_member(self.dsn, self.owner, "newtrial@example.test")
        teams.accept_invitation(self.dsn, account["id"], self.mail.call_args.args[1])
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2500.00")
        teams.remove_member(self.dsn, self.owner, account["id"])
        self.assertEqual(billing.wallet_summary(self.dsn, account["id"])["balance"], "0.00")
        with self.assertRaises(PlanError):
            billing.claim_trial(self.dsn, account["id"])

    def test_one_trial_per_network_including_concurrent_verifications(self):
        first, first_token = self.register_and_verify("first@example.test", "net-a")
        second, second_token = self.register_and_verify("second@example.test", "net-a")
        other, other_token = self.register_and_verify("elsewhere@example.test", "net-b")
        self.assertEqual(account_email.verify_email(self.dsn, first_token), "started")
        self.assertEqual(account_email.verify_email(self.dsn, second_token), "network_used")
        self.assertEqual(account_email.verify_email(self.dsn, other_token), "started")
        self.assertEqual(billing.wallet_summary(self.dsn, second["id"])["balance"], "0.00")
        with self.assertRaises(PlanError) as raised:
            billing.claim_trial(self.dsn, second["id"])
        self.assertEqual((raised.exception.code, raised.exception.status), ("trial_network_used", 409))
        self.assertFalse(self.client(second["id"]).get("/api/billing").json["billing"]["can_start_trial"])
        # Support can still grant one by hand.
        activate(self.dsn, "second@example.test", "trial")
        self.assertEqual(billing.wallet_summary(self.dsn, second["id"])["balance"], "30.00")

        tokens = [self.register_and_verify(f"race{n}@example.test", "net-c")[1] for n in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda token: account_email.verify_email(self.dsn, token), tokens))
        self.assertEqual(sorted(outcomes), ["network_used"] * 3 + ["started"])
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM trial_network_claims WHERE network_hash='net-c'")
            self.assertEqual(cur.fetchone()["n"], 1)

    def test_existing_trial_claim_is_concurrent_safe_and_requires_neither_payment_nor_gateway(self):
        client = self.client(self.owner)
        with patch.dict(os.environ, {"PAYMONGO_SECRET_KEY": "", "PAYMONGO_WEBHOOK_SECRET": ""}):
            self.assertTrue(client.get("/api/billing").json["billing"]["can_start_trial"])
            def claim(_):
                try:
                    billing.claim_trial(self.dsn, self.owner)
                    return True
                except PlanError:
                    return False
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(sum(pool.map(claim, range(4))), 1)
            self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "30.00")
            self.assertFalse(client.get("/api/billing").json["billing"]["can_start_trial"])
            self.assertEqual(client.post("/api/billing/trial").status_code, 409)
            self.assertEqual(self.client(self.other).post("/api/billing/trial").status_code, 200)
        self.assertFalse(self.gateway.calls)

    def test_registration_wallet_failure_rolls_back_user_account(self):
        from auth import create_user
        with patch("billing.lock_wallet", side_effect=RuntimeError("Wallet write failed")):
            with self.assertRaises(RuntimeError):
                create_user(self.dsn, "rollback@example.test", "LocalPreviewOnly123!")
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT 1 FROM users WHERE email_key='rollback@example.test'")
            self.assertIsNone(cur.fetchone())

    def test_trial_claim_blocks_pending_orders_expired_trials_members_and_missing_csrf(self):
        client = self.client(self.owner)
        self.assertEqual(client.post("/api/billing/trial",
                                     headers={"X-CSRF-Token": "bad"}).status_code, 403)
        order = self.buy()
        self.assertEqual(client.post("/api/billing/trial").status_code, 409)
        payments.recheck(self.dsn, self.owner, order["id"], cancel=True)
        self.assertEqual(client.post("/api/billing/trial").status_code, 200)
        with billing.transaction(self.dsn) as cur:
            cur.execute("UPDATE team_wallets SET ends_at=NOW()-INTERVAL '1 day' WHERE owner_id=%s",
                        (self.owner,))
        self.assertEqual(client.post("/api/billing/trial").status_code, 409)
        self.team()
        self.join()
        self.assertEqual(self.client(self.member).post("/api/billing/trial").status_code, 409)
        self.assertFalse(client.get("/api/billing").json["billing"]["can_start_trial"])

    def test_ai_pitch_drafts_charge_refund_and_lock_by_plan(self):
        def lead(user_id, name="Harbor Dental"):
            with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO prospects(business_name,category,google_place_id,user_id)
                               VALUES (%s,'Dentist',%s,%s) RETURNING id""", (name, uuid.uuid4().hex, user_id))
                return cur.fetchone()[0]

        def pitch_rows(state):
            with billing.transaction(self.dsn) as cur:
                cur.execute("SELECT COUNT(*) AS n FROM ai_pitch_runs WHERE state=%s", (state,))
                return cur.fetchone()["n"]

        lead_id = lead(self.owner)
        billing.claim_trial(self.dsn, self.other)
        with self.assertRaises(PlanError) as raised:
            billing.reserve_pitch(self.dsn, self.other, lead(self.other, "Trial Lead"))
        self.assertEqual((raised.exception.code, raised.exception.status), ("pitch_locked", 403))
        with self.assertRaises(PlanError) as raised:
            billing.reserve_pitch(self.dsn, self.fourth, lead(self.fourth, "No Plan Lead"))
        self.assertEqual(raised.exception.code, "pitch_locked")

        self.team()
        self.join()
        pitch_id, row = billing.reserve_pitch(self.dsn, self.member, lead(self.member, "Member Lead"))
        self.assertEqual(row["business_name"], "Member Lead")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2499.00")
        with self.assertRaises(PlanError) as raised:
            billing.reserve_pitch(self.dsn, self.member, lead_id)
        self.assertEqual(raised.exception.code, "pitch_busy")
        billing.fail_pitch(self.dsn, self.member, pitch_id)
        billing.fail_pitch(self.dsn, self.member, pitch_id)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2500.00")

        with self.assertRaises(PlanError) as raised:
            billing.reserve_pitch(self.dsn, self.member, lead_id)
        self.assertEqual((raised.exception.code, raised.exception.status), ("lead_not_found", 404))

        pitch_id, _ = billing.reserve_pitch(self.dsn, self.owner, lead_id)
        balance = billing.finish_pitch(self.dsn, self.owner, pitch_id, lead_id, "Hi,", "Subject", "[Web Design] x")
        self.assertEqual(balance, Decimal("2499.00"))
        with self.assertRaises(PlanError) as raised:
            billing.finish_pitch(self.dsn, self.owner, pitch_id, lead_id, "Again", "S", "A")
        self.assertEqual(raised.exception.code, "pitch_expired")
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT ai_pitch_draft, subject_line, status FROM prospects WHERE id=%s", (lead_id,))
            self.assertEqual(dict(cur.fetchone()), {"ai_pitch_draft": "Hi,", "subject_line": "Subject",
                                                    "status": "drafted"})
        summary = billing.wallet_summary(self.dsn, self.owner)
        self.assertEqual(Decimal(summary["usage"]["used"]), Decimal("1"))

        stale_id, _ = billing.reserve_pitch(self.dsn, self.owner, lead_id)
        with billing.transaction(self.dsn) as cur:
            cur.execute("UPDATE ai_pitch_runs SET created_at=NOW()-INTERVAL '5 minutes' WHERE id=%s",
                        (stale_id,))
        fresh_id, _ = billing.reserve_pitch(self.dsn, self.owner, lead_id)
        self.assertNotEqual(fresh_id, stale_id)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2498.00")
        with self.assertRaises(PlanError) as raised:
            billing.finish_pitch(self.dsn, self.owner, stale_id, lead_id, "Late", "S", "A")
        self.assertEqual(raised.exception.code, "pitch_expired")
        billing.finish_pitch(self.dsn, self.owner, fresh_id, lead_id, "Hi again,", "S2", "A2")
        self.assertEqual((pitch_rows("done"), pitch_rows("failed"), pitch_rows("pending")), (2, 2, 0))

        with billing.transaction(self.dsn) as cur:
            cur.execute("UPDATE team_wallets SET balance=0.50 WHERE owner_id=%s", (self.owner,))
        with self.assertRaises(PlanError) as raised:
            billing.reserve_pitch(self.dsn, self.owner, lead_id)
        self.assertEqual((raised.exception.code, raised.exception.status), ("insufficient_credits", 402))

    def test_ai_pitch_route_charges_on_success_and_refunds_openai_failures(self):
        self.team("solo")
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO prospects(business_name,category,google_place_id,user_id)
                           VALUES ('Harbor Dental','Dentist','pitch-route',%s) RETURNING id""", (self.owner,))
            lead_id = cur.fetchone()[0]
        start = Decimal(billing.wallet_summary(self.dsn, self.owner)["balance"])
        ok = Mock(status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content": "Hi Harbor,"}}]}
        client = self.client(self.owner)
        with patch.object(self.api, "OPENAI_KEY", "sk-test"), \
                patch("requests.post", side_effect=[Mock(status_code=429, text="rate limited")]), \
                contextlib.redirect_stderr(io.StringIO()):
            failed = client.post(f"/api/trigger/pitch-single/{lead_id}")
        self.assertEqual((failed.status_code, failed.json["code"]), (502, "ai_failed"))
        self.assertEqual(Decimal(billing.wallet_summary(self.dsn, self.owner)["balance"]), start)
        with patch.object(self.api, "OPENAI_KEY", "sk-test"), patch("requests.post", return_value=ok):
            drafted = client.post(f"/api/trigger/pitch-single/{lead_id}")
        self.assertEqual(drafted.status_code, 200, drafted.json)
        self.assertEqual(Decimal(drafted.json["balance"]), start - 1)
        self.assertIn("1 credit used", drafted.json["message"])

    def test_invites_reserve_seats_accept_once_and_share_only_credits(self):
        self.team()
        token = self.join()
        summary = teams.team_summary(self.dsn, self.owner)
        self.assertEqual((len(summary["members"]), summary["available"]), (2, 2))
        with self.assertRaises(PlanError):
            teams.accept_invitation(self.dsn, self.member, token)
        member_account = billing.wallet_summary(self.dsn, self.member)
        self.assertFalse(member_account["can_manage_team"])
        self.assertEqual(member_account["balance"], "2500.00")
        run, _ = self.reserve(self.member)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2490.00")
        billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2497.00")
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO prospects(business_name,category,google_place_id,user_id)
                           VALUES ('Private','Law Firm','private',%s)""", (self.owner,))
        self.assertEqual(self.client(self.member).get("/user-management").status_code, 403)
        owner_response = self.client(self.owner).get("/api/queue")
        self.assertEqual(owner_response.status_code, 200, owner_response.json)
        self.assertIn("Private", str(owner_response.json))
        response = self.client(self.member).get("/api/queue")
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn("Private", str(response.json))

    def test_seats_duplicate_invites_wrong_email_revoke_expiry_and_remove(self):
        self.team()
        first = teams.invite_member(self.dsn, self.owner, "member@example.test")
        token = self.mail.call_args.args[1]
        with self.assertRaises(PlanError):
            teams.accept_invitation(self.dsn, self.other, token)
        with self.assertRaises(PlanError):
            teams.invite_member(self.dsn, self.owner, "MEMBER@example.test")
        teams.invite_member(self.dsn, self.owner, "other@example.test")
        third = teams.invite_member(self.dsn, self.owner, "fourth@example.test")
        with self.assertRaises(PlanError):
            teams.invite_member(self.dsn, self.owner, "fifth@example.test")
        teams.revoke_invitation(self.dsn, self.owner, third)
        self.assertEqual(teams.team_summary(self.dsn, self.owner)["available"], 1)
        teams.accept_invitation(self.dsn, self.member, token)
        with self.assertRaises(PlanError):
            teams.remove_member(self.dsn, self.owner, self.owner)
        teams.remove_member(self.dsn, self.owner, self.member)
        self.assertFalse(billing.wallet_summary(self.dsn, self.member)["plan"]["active"])
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE team_invites SET expires_at=NOW()-INTERVAL '1 day' WHERE status='pending'")
        self.assertEqual(teams.team_summary(self.dsn, self.owner)["available"], 3)

    def test_daily_invitation_cap_counts_revoked_and_failed_invites(self):
        self.team("enterprise")
        for n in range(teams.INVITES_PER_DAY):
            invite = teams.invite_member(self.dsn, self.owner, f"loop{n}@example.test")
            teams.revoke_invitation(self.dsn, self.owner, invite)
        with self.assertRaises(PlanError) as raised:
            teams.invite_member(self.dsn, self.owner, "one-more@example.test")
        self.assertEqual((raised.exception.code, raised.exception.status), ("rate_limited", 429))
        self.assertEqual(self.mail.call_count, teams.INVITES_PER_DAY)
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE team_invites SET created_at = NOW() - INTERVAL '25 hours'")
        teams.invite_member(self.dsn, self.owner, "one-more@example.test")

    def test_password_change_and_sign_out_everywhere_revoke_other_sessions(self):
        import auth
        from werkzeug.security import generate_password_hash
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                        (generate_password_hash("Old-password-1"), self.owner))
        here, laptop = self.client(self.owner), self.client(self.owner)
        self.assertEqual(here.get("/api/session").status_code, 200)  # legacy session adopted
        self.assertEqual(laptop.get("/api/session").status_code, 200)

        wrong = here.post("/api/account/password", json={
            "current_password": "nope", "new_password": "New-password-2", "confirm_password": "New-password-2"})
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(laptop.get("/api/session").status_code, 200)

        changed = here.post("/api/account/password", json={
            "current_password": "Old-password-1", "new_password": "New-password-2",
            "confirm_password": "New-password-2"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(here.get("/api/session").status_code, 200)
        self.assertEqual(laptop.get("/api/session").status_code, 401)
        self.assertEqual(laptop.get("/overview").status_code, 302)
        user = auth.authenticate(self.dsn, "owner@example.test", "New-password-2")
        self.assertEqual(user["session_version"], 2)
        with self.assertRaises(auth.AuthError):
            auth.authenticate(self.dsn, "owner@example.test", "Old-password-1")

        stale_legacy = self.api.app.test_client()
        with stale_legacy.session_transaction() as session:
            session["user_id"] = self.owner  # issued before versioning, never used since
        self.assertEqual(stale_legacy.get("/api/session").status_code, 401)

        ended = here.post("/api/account/sign-out-everywhere")
        self.assertEqual(ended.get_json()["redirect"], "/login")
        self.assertEqual(here.get("/api/session").status_code, 401)
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT session_version FROM users WHERE id=%s", (self.owner,))
            self.assertEqual(cur.fetchone()[0], 3)

    def test_postgres_rate_limit_counter_is_atomic_across_connections(self):
        import rate_limit
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(rate_limit.SCHEMA)
            cur.execute(rate_limit.SCHEMA)  # Migration is idempotent.
        store = rate_limit.PostgresStore(self.dsn)
        expires = time.time() + 60
        with ThreadPoolExecutor(max_workers=8) as pool:
            counts = sorted(pool.map(lambda _: store.increment("bucket", expires), range(24)))
        self.assertEqual(counts, list(range(1, 25)))
        self.assertEqual(store.peek("bucket"), 24)
        store.increment("expired", time.time() - 1)
        self.assertEqual(store.peek("expired"), 0)
        store.clear("bucket")
        self.assertEqual(store.peek("bucket"), 0)
        limiter = rate_limit.Limiter(store)
        for _ in range(rate_limit.SMTP_TEST.max_hits):
            limiter.hit(rate_limit.SMTP_TEST, self.owner)
        with self.assertRaises(rate_limit.RateLimited):
            limiter.hit(rate_limit.SMTP_TEST, self.owner)

    def test_smtp_failure_releases_reserved_seat(self):
        import smtplib
        self.team()
        self.mail.side_effect = smtplib.SMTPException("test failure")
        with self.assertRaises(PlanError) as raised:
            teams.invite_member(self.dsn, self.owner, "member@example.test")
        self.assertEqual(raised.exception.code, "invite_delivery_failed")
        self.assertEqual(teams.team_summary(self.dsn, self.owner)["available"], 3)

    def test_concurrent_invitations_cannot_oversubscribe(self):
        self.team()
        def invite(n):
            try:
                teams.invite_member(self.dsn, self.owner, f"guest{n}@example.test")
                return True
            except PlanError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(invite, range(8))), 3)
        self.assertEqual(teams.team_summary(self.dsn, self.owner)["available"], 0)

    def test_enterprise_owner_can_reserve_exactly_fourteen_member_seats(self):
        self.team("enterprise")
        for n in range(14):
            teams.invite_member(self.dsn, self.owner, f"guest{n}@example.test")
        summary = teams.team_summary(self.dsn, self.owner)
        self.assertEqual((summary["seats"], len(summary["invitations"]), summary["available"]), (15, 14, 0))
        with self.assertRaises(PlanError):
            teams.invite_member(self.dsn, self.owner, "excess@example.test")

    def test_concurrent_member_searches_cannot_overspend_shared_wallet(self):
        self.team()
        self.join("member")
        self.join("other")
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE team_wallets SET balance=10 WHERE owner_id=%s", (self.owner,))
        def start(user):
            try:
                return self.reserve(user)[0]
            except PlanError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, [self.member, self.other]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "0.00")

    def test_idempotent_reservation_settlement_conflict_and_recovery(self):
        self.team()
        key = str(uuid.uuid4())
        run, repeated = self.reserve(key=key)
        same, repeated = self.reserve(key=key)
        self.assertTrue(repeated)
        self.assertEqual(run["id"], same["id"])
        with self.assertRaises(PlanError):
            self.reserve(key=key, body={"categories": ["Other"], "sources": ["google"]})
        billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
        billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2497.00")
        interrupted, _ = self.reserve()
        billing.settle(self.dsn, interrupted["id"], interrupted=True)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2497.00")
        self.assertIsNone(billing.snapshot(self.dsn, self.member, run["id"]))

    def test_sidebar_usage_includes_reservations_refunds_topups_and_monthly_boundaries(self):
        activate(self.dsn, "owner@example.test", "small_team", months=12)
        self.join()
        run, _ = self.reserve(self.member)
        summary = billing.wallet_summary(self.dsn, self.owner)
        self.assertEqual(summary["usage"]["used"], "10.00")
        self.assertEqual(summary["usage"]["total"], "2500.00")
        self.assertEqual(summary["usage"]["percent"], "0.4")
        self.assertEqual(summary["usage"]["next_event"], "refresh")
        billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
        top_up(self.dsn, "owner@example.test", "sidebar-topup")
        summary = billing.wallet_summary(self.dsn, self.member)
        self.assertEqual(summary["usage"]["used"], "3.00")
        self.assertEqual(summary["usage"]["total"], "3250.00")
        self.assertEqual(summary["balance"], "3247.00")
        response = self.client(self.member).get("/api/account-plan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for key in ("used", "total", "percent", "next_event"):
            self.assertEqual(response.json["account"]["usage"][key], summary["usage"][key])
        with billing.transaction(self.dsn) as cur:
            cur.execute("UPDATE team_wallets SET next_grant_at=NOW()-INTERVAL '1 day' WHERE owner_id=%s",
                        (self.owner,))
        summary = billing.wallet_summary(self.dsn, self.owner)
        self.assertEqual(Decimal(summary["usage"]["used"]), 0)
        self.assertEqual(summary["usage"]["total"], "3750.00")
        self.assertEqual(summary["usage"]["percent"], "0.0")

    def test_failed_worker_launch_releases_credits_and_does_not_consume_daily_slot(self):
        self.team()
        from discovery_runs import DiscoveryBusyError
        client = self.client(self.owner)
        with patch.object(self.api, "LOCAL_DISCOVERY_RUNS") as runs:
            runs.start.side_effect = DiscoveryBusyError("All workers busy")
            result = client.post("/api/trigger/discover", json={
                "location": "Toronto", "categories": ["Law Firm"], "sources": ["yelp"],
            })
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2500.00")
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT progress FROM paid_discovery_runs WHERE user_id=%s", (self.owner,))
            self.assertTrue(cur.fetchone()["progress"]["launch_failed"])
        with patch.dict(billing.PLANS, {"small_team": replace(billing.PLANS["small_team"], daily=1)}):
            run, _ = self.reserve()
            billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
            with self.assertRaises(PlanError) as raised:
                self.reserve()
            self.assertEqual(raised.exception.code, "daily_limit")

    def test_fractional_refunds_failed_runs_and_new_insert_vs_update(self):
        self.team()
        body = {"service_types": ["video_editing"], "pages_per_role": 1}
        run, _ = self.reserve(mode="hiring", body=body)
        billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2482.00")
        # A single custom role costs 15; zero-result settlement refunds exactly 10.50.
        from hiring_services import HiringService
        one = HiringService("one", "One", ("Video Editor",), ("video editor",))
        with patch("billing.services_for_payload", return_value={"one": one}):
            run, _ = self.reserve(mode="hiring", body={"service_types": ["one"]})
        billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
        self.assertEqual(billing.snapshot(self.dsn, self.owner, run["id"])["credit_refund"], "10.50")
        for expected_new in [1, 0]:
            run, _ = self.reserve()
            billing.bind_run(run["id"])
            with billing.transaction(self.dsn) as cur:
                billing.insert_and_track(cur, """INSERT INTO prospects
                    (business_name,category,google_place_id,user_id) VALUES %s
                    ON CONFLICT(user_id,google_place_id) DO UPDATE SET business_name=EXCLUDED.business_name;""",
                    [("Law", "Law Firm", "same", self.owner)])
            billing.settle(self.dsn, run["id"], {"successful_tasks": 1})
            state = billing.snapshot(self.dsn, self.owner, run["id"])
            self.assertEqual(state["new_leads"], expected_new)
            self.assertEqual(Decimal(state["credit_refund"]), 0 if expected_new else 7)
        failed, _ = self.reserve()
        billing.settle(self.dsn, failed["id"], {"successful_tasks": 0})
        self.assertEqual(billing.snapshot(self.dsn, self.owner, failed["id"])["credit_refund"], "10.00")

    def test_plan_enforcement_csrf_and_custom_creation_api(self):
        activate(self.dsn, "owner@example.test", "solo")
        client = self.client(self.owner)
        for body in [
            {"location": "Toronto", "categories": ["Law"], "sources": ["facebook"]},
            {"location": "Toronto", "categories": ["A", "B", "C", "D"], "sources": ["yelp"]},
        ]:
            self.assertEqual(client.post("/api/trigger/discover", json=body).status_code, 403)
        self.assertEqual(client.post("/api/trigger/discover-jobs",
                                     json={"service_types": ["video_editing"]}).status_code, 403)
        self.assertEqual(client.post("/api/hiring-services", json={}).status_code, 403)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = "bad"
        self.assertEqual(client.post("/api/team/invitations", json={"email": "member@example.test"}).status_code, 403)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "750.00")

    def test_api_discovery_reserves_and_settles_and_replay_does_not_restart(self):
        self.team()
        client = self.client(self.owner)
        from discovery_runs import LocalDiscoveryRuns
        with (
            patch.object(self.api, "LOCAL_DISCOVERY_RUNS", LocalDiscoveryRuns()),
            patch("discovery_runs.threading.Thread", ImmediateThread),
            patch("discovery_yelp.run_yelp_discovery", return_value=0) as worker,
        ):
            payload = {"location": "Toronto", "categories": ["Law Firm"], "sources": ["yelp"]}
            first = client.post("/api/trigger/discover", json=payload)
            self.assertEqual(first.status_code, 202, first.json)
            again = client.post("/api/trigger/discover", json=payload)
            self.assertEqual(first.json["run_id"], again.json["run_id"])
            self.assertEqual(worker.call_count, 1)
            status = client.get(f"/api/discovery/runs/{first.json['run_id']}").json["run"]
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["credit_refund"], "7.00")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2497.00")

    def test_real_local_worker_counts_inserts_and_repeated_leads_refund(self):
        self.team()
        from discovery_runs import LocalDiscoveryRuns
        import discovery_yelp
        from unittest.mock import Mock
        response = Mock()
        response.json.return_value = {"businesses": [
            {"name": "Private Law Firm", "categories": ["Lawyer"]},
        ]}
        client = self.client(self.member)
        self.join()
        with (
            patch.object(self.api, "LOCAL_DISCOVERY_RUNS", LocalDiscoveryRuns()),
            patch("discovery_runs.threading.Thread", ImmediateThread),
            patch.object(discovery_yelp, "DB_DSN", self.dsn),
            patch.object(discovery_yelp, "SERPAPI_KEY", "fake-key"),
            patch.object(discovery_yelp, "serpapi_fetch", return_value=response),
        ):
            for new in [1, 0]:
                client.environ_base["HTTP_IDEMPOTENCY_KEY"] = str(uuid.uuid4())
                result = client.post("/api/trigger/discover", json={
                    "location": "Toronto", "categories": ["Law Firm"], "sources": ["yelp"],
                    "rating_min": 0, "max_reviews": None,
                })
                self.assertEqual(result.status_code, 202, result.json)
                state = billing.snapshot(self.dsn, self.member, result.json["run_id"])
                self.assertEqual(state["new_leads"], new, state)
                self.assertEqual(Decimal(state["credit_refund"]), 0 if new else 7)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2487.00")
        self.assertNotIn("Private Law Firm", str(self.client(self.owner).get("/api/queue").json))
        self.assertIn("Private Law Firm", str(client.get("/api/queue").json))

    def test_real_hiring_worker_counts_upserts_and_refunds_to_owner(self):
        self.team()
        self.join()
        import discovery_jobs
        from discovery_runs import LocalDiscoveryRuns
        from unittest.mock import Mock
        google, linkedin = Mock(), Mock()
        google.json.return_value = {"jobs_results": [{
            "title": "Video Editor", "company_name": "Private Hiring Studio",
            "location": "Toronto, Canada", "description": "A video editor opportunity",
        }]}
        linkedin.json.return_value = []
        client = self.client(self.member)
        with (
            patch.object(self.api, "HIRING_DISCOVERY_RUNS", LocalDiscoveryRuns("hiring")),
            patch("discovery_runs.threading.Thread", ImmediateThread),
            patch.object(discovery_jobs, "DB_DSN", self.dsn),
            patch.object(discovery_jobs, "SERPAPI_KEY", "fake-key"),
            patch.object(discovery_jobs, "serpapi_fetch", return_value=google),
            patch.object(discovery_jobs, "fetch_linkedin_jobs", return_value=linkedin),
            patch.object(discovery_jobs, "enrich_company_contact",
                         return_value={"website_url": None, "email": None}),
        ):
            for new in [1, 0]:
                client.environ_base["HTTP_IDEMPOTENCY_KEY"] = str(uuid.uuid4())
                result = client.post("/api/trigger/discover-jobs",
                                     json={"service_types": ["video_editing"], "country": "Canada"})
                self.assertEqual(result.status_code, 202, result.json)
                state = billing.snapshot(self.dsn, self.member, result.json["run_id"])
                self.assertEqual(state["new_leads"], new, state)
                self.assertEqual(Decimal(state["credit_refund"]), 0 if new else 42)
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2422.00")
        self.assertNotIn("Private Hiring Studio", str(self.client(self.owner).get("/api/job-leads").json))
        self.assertIn("Private Hiring Studio", str(client.get("/api/job-leads").json))

    def test_shared_daily_limit_and_member_management_bypasses_are_blocked(self):
        self.team()
        self.join()
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO paid_discovery_runs
                (id,wallet_id,user_id,request_key,fingerprint,mode,charge,state,settled_at)
                SELECT gen_random_uuid(),w.id,%s,gen_random_uuid(),'fixture','local',10,'failed',NOW()
                FROM team_wallets w CROSS JOIN generate_series(1,200) WHERE w.owner_id=%s""",
                        (self.owner, self.owner))
        with self.assertRaises(PlanError) as raised:
            self.reserve(self.member)
        self.assertEqual(raised.exception.code, "daily_limit")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2500.00")
        client = self.client(self.member)
        self.assertEqual(client.post("/api/team/invitations", json={"email": "other@example.test"}).status_code, 403)
        self.assertEqual(client.delete(f"/api/team/members/{self.owner}").status_code, 403)
        self.assertEqual(client.get("/api/team").status_code, 403)

    def test_empty_hiring_provider_success_differs_from_wholly_failed_discovery(self):
        self.team()
        import discovery_jobs
        from discovery_runs import LocalDiscoveryRuns
        from unittest.mock import Mock
        google = Mock()
        google.json.return_value = {"jobs_results": []}
        client = self.client(self.owner)
        with (
            patch.object(self.api, "HIRING_DISCOVERY_RUNS", LocalDiscoveryRuns("hiring")),
            patch("discovery_runs.threading.Thread", ImmediateThread),
            patch.object(discovery_jobs, "SERPAPI_KEY", "fake-key"),
            patch.object(discovery_jobs, "serpapi_fetch", return_value=google) as fetch,
            patch.object(discovery_jobs, "fetch_linkedin_jobs", side_effect=RuntimeError("Provider unavailable")),
        ):
            for expected_state, expected_refund in [("succeeded", "42.00"), ("failed", "60.00")]:
                client.environ_base["HTTP_IDEMPOTENCY_KEY"] = str(uuid.uuid4())
                result = client.post("/api/trigger/discover-jobs",
                                     json={"service_types": ["video_editing"], "country": "Canada"})
                self.assertEqual(result.status_code, 202, result.json)
                state = billing.snapshot(self.dsn, self.owner, result.json["run_id"])
                self.assertEqual(state["state"], expected_state, state)
                self.assertEqual(state["credit_refund"], expected_refund)
                self.assertEqual(state["net_charge"], "18.00" if expected_state == "succeeded" else "0.00")
                fetch.side_effect = RuntimeError("Provider unavailable")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "2482.00")

    def test_expired_plan_annual_monthly_rollover_and_idempotent_topup(self):
        activate(self.dsn, "owner@example.test", "small_team", months=12)
        top_up(self.dsn, "owner@example.test", "receipt-1")
        top_up(self.dsn, "owner@example.test", "receipt-1")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "3250.00")
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("""UPDATE team_wallets SET next_grant_at=NOW()-INTERVAL '1 day'
                           WHERE owner_id=%s""", (self.owner,))
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "3750.00")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "3750.00")
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("""UPDATE team_wallets SET next_grant_at=NOW()-INTERVAL '1 day'
                           WHERE owner_id=%s""", (self.owner,))
        top_up(self.dsn, "owner@example.test", "receipt-at-boundary")
        self.assertEqual(billing.wallet_summary(self.dsn, self.owner)["balance"], "4500.00")
        self.join()
        with self.assertRaises(PlanError):
            change_plan(self.dsn, "owner@example.test", "solo")
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE team_wallets SET ends_at=NOW()-INTERVAL '1 second' WHERE owner_id=%s", (self.owner,))
        with self.assertRaises(PlanError):
            self.reserve()


if __name__ == "__main__":
    unittest.main()
