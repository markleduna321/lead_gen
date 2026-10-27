"""Email verification, password reset and the one-trial-per-network rule."""

import contextlib
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import account_email
import app_env
import billing
import payments
import rate_limit
import system_mail
from auth import AuthError, authenticate

PASSWORD = "LocalPreviewOnly123!"
NEW_PASSWORD = "BrandNewPassword456!"


class NetworkTests(unittest.TestCase):
    def test_network_key_groups_ipv6_by_64_and_unwraps_mapped_ipv4(self):
        self.assertEqual(account_email.network_key("203.0.113.9"), "203.0.113.9")
        self.assertEqual(account_email.network_key("2001:db8:1:2:aaaa::1"), "2001:db8:1:2::/64")
        self.assertEqual(account_email.network_key("2001:db8:1:2:ffff::9"), "2001:db8:1:2::/64")
        self.assertNotEqual(account_email.network_key("2001:db8:1:3::1"), "2001:db8:1:2::/64")
        self.assertEqual(account_email.network_key("::ffff:203.0.113.9"), "203.0.113.9")
        self.assertEqual(account_email.network_key("unknown"), "unknown")

    def test_network_hash_is_keyed_and_skipped_in_development(self):
        hosted = {"APP_ENV": "production", "TRIAL_IP_HASH_SECRET": "secret-one"}
        digest = account_email.network_hash("203.0.113.9", hosted)
        self.assertEqual(len(digest), 64)
        self.assertNotIn("203.0.113", digest)
        self.assertEqual(digest, account_email.network_hash("::ffff:203.0.113.9", hosted))
        self.assertNotEqual(digest, account_email.network_hash(
            "203.0.113.9", dict(hosted, TRIAL_IP_HASH_SECRET="secret-two")))
        for env in ["local", "desktop"]:
            self.assertIsNone(account_email.network_hash("203.0.113.9", {"APP_ENV": env}))
        with self.assertRaises(app_env.StartupConfigError):
            account_email.network_hash("203.0.113.9", {"APP_ENV": "production"})

    def test_hosted_configuration_requires_secret_and_mail(self):
        mail = {"SYSTEM_SMTP_PASSWORD": "fake-key", "SYSTEM_SMTP_FROM": "system@example.test",
                "APP_BASE_URL": "https://app.example.test"}
        account_email.check_hosted_configuration({"APP_ENV": "local"})
        with patch.dict(os.environ, mail, clear=True):
            account_email.check_hosted_configuration({"APP_ENV": "production", "TRIAL_IP_HASH_SECRET": "s"})
            with self.assertRaises(app_env.StartupConfigError):
                account_email.check_hosted_configuration({"APP_ENV": "production"})
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(app_env.StartupConfigError):
            account_email.check_hosted_configuration({"APP_ENV": "staging", "TRIAL_IP_HASH_SECRET": "s"})

    def test_development_prints_mail_when_unconfigured_and_production_refuses(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"APP_ENV": "local"}, clear=True), contextlib.redirect_stderr(stderr):
            system_mail.send("me@example.test", "Subject", "http://127.0.0.1:5000/verify-email#abc")
            self.assertEqual(system_mail.base_url(), "http://127.0.0.1:5000")
        self.assertIn("[DEV MAIL] To: me@example.test", stderr.getvalue())
        self.assertIn("/verify-email#abc", stderr.getvalue())
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            with self.assertRaises(system_mail.SystemMailUnavailable):
                system_mail.send("me@example.test", "Subject", "body")
            with self.assertRaises(system_mail.SystemMailUnavailable):
                system_mail.base_url()


@unittest.skipUnless(os.getenv("TEST_BILLING_DSN"), "Set TEST_BILLING_DSN for isolated PostgreSQL integration tests")
class AccountEmailIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        patch.stopall()
        cls.root_dsn = os.environ["TEST_BILLING_DSN"]
        cls.schema = "account_email_test_" + uuid.uuid4().hex
        with psycopg2.connect(cls.root_dsn) as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.dsn = make_dsn(cls.root_dsn, options=f"-csearch_path={cls.schema}")
        with psycopg2.connect(cls.dsn) as conn, conn.cursor() as cur:
            cur.execute((Path(__file__).resolve().parents[1] / "schema.sql").read_text())
            cur.execute("INSERT INTO users(email,email_key,password_hash) VALUES ('old@example.test','old@example.test','x')")
            cur.execute(account_email.SCHEMA)
            cur.execute(account_email.SCHEMA)
            cur.execute(billing.SCHEMA)
            cur.execute(payments.SCHEMA)
            cur.execute("SELECT email_verified_at IS NOT NULL FROM users WHERE email_key='old@example.test'")
            cls.legacy_verified = cur.fetchone()[0]
        with (patch("dotenv.load_dotenv"), patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
              contextlib.redirect_stderr(io.StringIO())):
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
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
        patch.object(self.api, "DB_DSN", self.dsn).start()
        patch.dict(self.api.app.config, {
            "BILLING_DSN": self.dsn, "RATE_LIMITER": rate_limit.Limiter(rate_limit.MemoryStore())}).start()
        patch.dict(os.environ, {"APP_ENV": "local", "SYSTEM_SMTP_PASSWORD": "", "SYSTEM_SMTP_FROM": "",
                                "APP_BASE_URL": "https://app.example.test"}).start()
        self.mail = patch("system_mail.send").start()
        patch("account_api.run_in_background", side_effect=lambda work: work()).start()
        self.addCleanup(patch.stopall)

    def client(self, user_id=None):
        client = self.api.app.test_client()
        with client.session_transaction() as session:
            if user_id is not None:
                session["user_id"] = user_id
            session["csrf_token"] = "test-csrf"
        client.environ_base["HTTP_X_CSRF_TOKEN"] = "test-csrf"
        return client

    def link_token(self, path):
        body = self.mail.call_args.args[2]
        self.assertIn(f"https://app.example.test/{path}#", body)
        return body.split(f"/{path}#", 1)[1].split()[0]

    def register(self, client, address="new@example.test"):
        return client.post("/register", data={
            "csrf_token": "test-csrf", "email": address, "password": PASSWORD,
            "confirm_password": PASSWORD, "next": "/settings"})

    def user(self, address):
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT * FROM users WHERE email_key=%s", (address,))
            return cur.fetchone()

    def test_existing_accounts_are_backfilled_as_verified(self):
        self.assertTrue(self.legacy_verified)

    def test_sign_up_sends_link_and_verification_starts_trial_once(self):
        client = self.client()
        response = self.register(client)
        self.assertEqual(response.status_code, 302)
        account = self.user("new@example.test")
        self.assertIsNone(account["email_verified_at"])
        self.assertIsNone(account["signup_network_hash"])
        self.assertEqual(self.mail.call_args.args[0], "new@example.test")
        token = self.link_token("verify-email")
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Verify your email to start your free trial.", page)
        self.assertEqual(billing.wallet_summary(self.dsn, account["id"])["balance"], "0.00")

        self.assertEqual(self.client().get("/verify-email").status_code, 200)
        anonymous = self.client()
        verified = anonymous.post("/api/auth/verify-email", json={"token": token})
        self.assertEqual(verified.status_code, 200, verified.get_data(as_text=True))
        self.assertEqual(verified.json["outcome"], "started")
        self.assertIn("free trial has started", verified.json["message"])
        self.assertEqual(billing.wallet_summary(self.dsn, account["id"])["balance"], "30.00")
        again = anonymous.post("/api/auth/verify-email", json={"token": token})
        self.assertEqual((again.status_code, again.json["code"]), (400, "invalid_link"))
        self.assertNotIn("verify-banner", client.get("/settings").get_data(as_text=True))
        for bad in [{}, {"token": "short"}, {"token": 7}, []]:
            self.assertEqual(anonymous.post("/api/auth/verify-email", json=bad).status_code, 400)

    def test_resend_requires_sign_in_and_is_rate_limited(self):
        self.assertEqual(self.client().post("/api/auth/resend-verification").status_code, 401)
        self.register(self.client())
        account = self.user("new@example.test")
        client = self.client(account["id"])
        first_token = self.link_token("verify-email")
        for _ in range(3):
            response = client.post("/api/auth/resend-verification")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["verified"])
        self.assertEqual(client.post("/api/auth/resend-verification").status_code, 429)
        # Earlier links stay valid until they expire.
        self.assertEqual(account_email.verify_email(self.dsn, first_token), "started")
        self.api.app.config["RATE_LIMITER"] = rate_limit.Limiter(rate_limit.MemoryStore())
        self.assertTrue(client.post("/api/auth/resend-verification").json["verified"])

    def test_resend_reports_mail_failure_without_details(self):
        self.register(self.client())
        self.mail.side_effect = OSError("smtp password=secret")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            response = self.client(self.user("new@example.test")["id"]).post("/api/auth/resend-verification")
        self.assertEqual((response.status_code, response.json["code"]), (503, "mail_failed"))
        self.assertNotIn("secret", stderr.getvalue())

    def test_sign_up_still_succeeds_when_mail_fails(self):
        self.mail.side_effect = OSError("down")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.register(self.client()).status_code, 302)
        self.assertIsNotNone(self.user("new@example.test"))

    def test_forgot_password_response_is_identical_for_known_and_unknown_emails(self):
        self.register(self.client())
        self.mail.reset_mock()
        pages = []
        for address in ["new@example.test", "nobody@example.test", "not-an-email"]:
            response = self.client().post("/forgot-password",
                                          data={"csrf_token": "test-csrf", "email": address})
            self.assertEqual(response.status_code, 200)
            pages.append(response.get_data(as_text=True).replace(address, ""))
        self.assertEqual(pages[0], pages[1])
        self.assertIn("If an account exists for that email", pages[0])
        self.assertEqual(self.mail.call_count, 1)
        self.assertEqual(self.mail.call_args.args[0], "new@example.test")
        self.assertIn("Forgot password?", self.client().get("/login").get_data(as_text=True))
        self.assertEqual(self.client().get("/forgot-password").status_code, 200)

    def test_forgot_password_is_rate_limited_per_email(self):
        for _ in range(3):
            self.client().post("/forgot-password", data={"csrf_token": "test-csrf", "email": "x@example.test"})
        response = self.client().post("/forgot-password",
                                      data={"csrf_token": "test-csrf", "email": " X@Example.test "})
        self.assertEqual(response.status_code, 429)

    def test_reset_password_signs_out_everywhere_and_links_work_once(self):
        self.register(self.client())
        account = self.user("new@example.test")
        old_session = self.client(account["id"])
        with old_session.session_transaction() as session:
            session["session_version"] = account["session_version"]
        self.assertEqual(old_session.get("/api/billing").status_code, 200)
        self.client().post("/forgot-password", data={"csrf_token": "test-csrf", "email": "new@example.test"})
        stale = self.link_token("reset-password")
        self.client().post("/forgot-password", data={"csrf_token": "test-csrf", "email": "new@example.test"})
        token = self.link_token("reset-password")
        self.assertNotEqual(stale, token)
        self.assertEqual(self.client().get("/reset-password").status_code, 200)

        form = {"csrf_token": "test-csrf", "token": token, "password": NEW_PASSWORD,
                "confirm_password": "different-password"}
        mismatch = self.client().post("/reset-password", data=form)
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn(token, mismatch.get_data(as_text=True))
        expired_form = dict(form, csrf_token="wrong", confirm_password=NEW_PASSWORD)
        formless = self.client()
        del formless.environ_base["HTTP_X_CSRF_TOKEN"]
        expired = formless.post("/reset-password", data=expired_form)
        self.assertEqual(expired.status_code, 403)
        self.assertIn(token, expired.get_data(as_text=True))
        stale_response = self.client().post("/reset-password", data=dict(form, token=stale,
                                                                         confirm_password=NEW_PASSWORD))
        self.assertEqual(stale_response.status_code, 400)

        done = self.client().post("/reset-password", data=dict(form, confirm_password=NEW_PASSWORD))
        self.assertEqual(done.status_code, 302)
        self.assertTrue(done.headers["Location"].endswith("/login?reset=done"))
        self.assertIn("every device was signed out",
                      self.client().get("/login?reset=done").get_data(as_text=True))
        self.assertEqual(old_session.get("/api/billing").status_code, 401)
        self.assertEqual(authenticate(self.dsn, "new@example.test", NEW_PASSWORD)["id"], account["id"])
        with self.assertRaises(AuthError):
            authenticate(self.dsn, "new@example.test", PASSWORD)
        self.assertEqual(self.client().post("/reset-password", data=dict(
            form, confirm_password=NEW_PASSWORD)).status_code, 400)
        # The reset link proved the inbox, so the account is verified and gets its trial.
        self.assertIsNotNone(self.user("new@example.test")["email_verified_at"])
        self.assertEqual(billing.wallet_summary(self.dsn, account["id"])["balance"], "30.00")

    def test_expired_and_wrong_purpose_tokens_are_rejected(self):
        self.register(self.client())
        verify_token = self.link_token("verify-email")
        with self.assertRaises(AuthError):
            account_email.reset_password(self.dsn, verify_token, NEW_PASSWORD, NEW_PASSWORD)
        with billing.transaction(self.dsn) as cur:
            cur.execute("UPDATE auth_tokens SET expires_at=NOW()-INTERVAL '1 second'")
        with self.assertRaises(AuthError):
            account_email.verify_email(self.dsn, verify_token)
        with billing.transaction(self.dsn) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM auth_tokens WHERE token_hash=%s", (verify_token,))
            self.assertEqual(cur.fetchone()["n"], 0)

    def test_deactivated_accounts_cannot_use_links(self):
        self.register(self.client())
        token = self.link_token("verify-email")
        with billing.transaction(self.dsn) as cur:
            cur.execute("UPDATE users SET is_active=FALSE")
        with self.assertRaises(AuthError):
            account_email.verify_email(self.dsn, token)


if __name__ == "__main__":
    unittest.main()
