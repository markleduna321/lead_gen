import contextlib
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER_ID, signed_in_client

import auth


class AuthCredentialTests(unittest.TestCase):
    def test_passwords_are_hashed_and_never_stored_in_clear_text(self):
        from werkzeug.security import check_password_hash
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"id": 7, "email": "QA@Example.test",
                                        "display_name": None, "role": "member"}
        with patch("psycopg2.connect", return_value=database):
            auth.create_user("qa-only", "QA@Example.test", "correct-horse-battery")
        _, params = cursor.execute.call_args.args
        address, key, stored_hash, _name = params
        self.assertEqual(key, "qa@example.test", "email_key must be normalized for lookup")
        self.assertEqual(address, "QA@Example.test", "display casing is preserved")
        self.assertNotIn("correct-horse-battery", stored_hash)
        self.assertTrue(check_password_hash(stored_hash, "correct-horse-battery"))

    def test_weak_or_malformed_credentials_are_rejected(self):
        for email in ["", "not-an-email", "missing@tld", "a@b@c.test", None, 42]:
            with self.subTest(email=email), self.assertRaises(auth.AuthError):
                auth.normalize_email(email)
        for password in ["short", "", None, "x" * 500, "has\nnewline123456"]:
            with self.subTest(password=password), self.assertRaises(auth.AuthError):
                auth.validate_password(password)
        self.assertEqual(auth.normalize_email("  QA@Example.test  "), "QA@Example.test")

    def test_unknown_email_and_wrong_password_are_indistinguishable(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value

        cursor.fetchone.return_value = None
        with patch("psycopg2.connect", return_value=database):
            with self.assertRaises(auth.AuthError) as unknown:
                auth.authenticate("qa-only", "nobody@example.test", "correct-horse-battery")

        from werkzeug.security import generate_password_hash
        cursor.fetchone.return_value = {
            "id": 1, "email": "qa@example.test", "display_name": None, "role": "member",
            "is_active": True, "password_hash": generate_password_hash("the-real-password"),
            "failed_login_count": 0, "locked_until": None,
        }
        with patch("psycopg2.connect", return_value=database):
            with self.assertRaises(auth.AuthError) as wrong:
                auth.authenticate("qa-only", "qa@example.test", "a-wrong-password")

        self.assertEqual(str(unknown.exception), str(wrong.exception))
        self.assertEqual(str(wrong.exception), auth.GENERIC_SIGN_IN_ERROR)
        # A malformed address must not reveal that it was malformed.
        with patch("psycopg2.connect", return_value=database):
            with self.assertRaises(auth.AuthError) as malformed:
                auth.authenticate("qa-only", "not-an-email", "correct-horse-battery")
        self.assertEqual(str(malformed.exception), auth.GENERIC_SIGN_IN_ERROR)

    def test_repeated_failures_lock_the_account(self):
        from werkzeug.security import generate_password_hash
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "id": 1, "email": "qa@example.test", "display_name": None, "role": "member",
            "is_active": True, "password_hash": generate_password_hash("the-real-password"),
            "failed_login_count": auth.MAX_FAILED_LOGINS - 1, "locked_until": None,
        }
        with patch("psycopg2.connect", return_value=database):
            with self.assertRaises(auth.AuthError):
                auth.authenticate("qa-only", "qa@example.test", "a-wrong-password")
        update = [call for call in cursor.execute.call_args_list
                  if "failed_login_count = %s" in call.args[0]]
        self.assertTrue(update, "the failed attempt must be recorded")
        self.assertIsNotNone(update[-1].args[1][1], "the account must be locked out")

    def test_redirect_targets_cannot_leave_the_site(self):
        for hostile in ["https://evil.test", "//evil.test", "http://evil.test/x",
                        "/\n/evil.test", None, 7, "javascript:alert(1)"]:
            with self.subTest(hostile=hostile):
                self.assertEqual(auth.safe_redirect_target(hostile), "/")
        self.assertEqual(auth.safe_redirect_target("/job-leads"), "/job-leads")


class RouteGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.module = importlib.import_module("control_gate_api")

    def test_every_api_route_rejects_anonymous_callers_with_json(self):
        client = self.module.app.test_client()
        for path in ["/api/queue", "/api/job-leads", "/api/export", "/api/hiring-services",
                     "/api/discovery/logs", "/api/session", "/api/discovery/runs/abc"]:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 401)
                # HTML here would break the dashboard's fetch().json() calls.
                self.assertEqual(response.json["code"], "auth_required")

    def test_anonymous_page_requests_redirect_to_sign_in(self):
        client = self.module.app.test_client()
        for path in ["/", "/prospects", "/job-leads", "/discovery"]:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])

    def test_sign_in_and_register_pages_stay_public(self):
        client = self.module.app.test_client()
        for path in ["/login", "/register"]:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)

    def test_a_removed_or_deactivated_account_loses_access_immediately(self):
        """A valid cookie must not outlive the account it belongs to."""
        client = signed_in_client(self.module.app, user_id=4242)  # no such account
        page = client.get("/prospects")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/login", page.headers["Location"])
        api = client.get("/api/queue")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json["code"], "auth_required")
        # The stale session is dropped rather than left to expire on its own.
        with client.session_transaction() as transaction:
            self.assertNotIn("user_id", transaction)

    def test_the_account_is_looked_up_once_per_request(self):
        with patch("auth.load_user", side_effect=lambda dsn, uid: dict(id=uid)) as lookup:
            # Public endpoints skip the account check entirely.
            self.assertEqual(self.module.app.test_client().get("/favicon.ico").status_code, 204)
            lookup.assert_not_called()
            # The guard and the template context share one lookup.
            signed_in_client(self.module.app).get("/prospects")
            self.assertEqual(lookup.call_count, 1)

    def test_mutations_report_missing_when_the_row_belongs_to_another_account(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0
        client = signed_in_client(self.module.app)
        cases = [
            ("/api/reject/42", None),
            ("/api/prospect/update-email/42", {"email": "x@example.test"}),
            ("/api/job-leads/42/reject", None),
            ("/api/job-leads/42/update-email", {"email": "x@example.test"}),
        ]
        with patch("psycopg2.connect", return_value=database):
            for path, payload in cases:
                with self.subTest(path=path):
                    response = client.post(path, json=payload) if payload else client.post(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json["status"], "error")
                    database.__enter__.return_value.commit.assert_not_called()

    def test_lead_reads_and_writes_are_scoped_to_the_signed_in_account(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = {"total": 0, "pending": 0, "sent": 0,
                                        "followup": 0, "with_email": 0}
        client = signed_in_client(self.module.app)
        with patch("psycopg2.connect", return_value=database):
            for path in ["/api/queue", "/api/job-leads", "/api/export"]:
                with self.subTest(path=path):
                    cursor.execute.reset_mock()
                    self.assertEqual(client.get(path).status_code, 200)
                    calls = [call.args for call in cursor.execute.call_args_list]
                    self.assertTrue(calls, "the route must issue a query to be meaningful")
                    for sql, params in calls:
                        self.assertIn("user_id = %s", sql)
                        self.assertIn(QA_USER_ID, list(params))


class RunIsolationTests(unittest.TestCase):
    def test_a_run_is_only_visible_to_its_owner(self):
        from discovery_runs import LocalDiscoveryRuns
        from test_discovery_runs import ImmediateThread
        runs = LocalDiscoveryRuns()
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            run_id = runs.start([("google", "Cafe", lambda: None)], runs.record_log, owner=1)
        self.assertIsNotNone(runs.snapshot(run_id, owner=1))
        self.assertIsNone(runs.snapshot(run_id, owner=2))
        self.assertIsNone(runs.snapshot(run_id, owner=None))
        self.assertNotIn("owner", runs.snapshot(run_id, owner=1))

    def test_one_account_cannot_block_another_from_searching(self):
        from discovery_runs import DiscoveryBusyError, LocalDiscoveryRuns
        import threading
        runs = LocalDiscoveryRuns()
        release, started = threading.Event(), threading.Event()

        def work():
            started.set()
            release.wait(timeout=5)
        try:
            runs.start([("google", "Cafe", work)], runs.record_log, owner=1)
            self.assertTrue(started.wait(timeout=2))
            with self.assertRaises(DiscoveryBusyError):
                runs.start([("google", "Cafe", lambda: None)], runs.record_log, owner=1)
            # A different account must not be blocked by someone else's search.
            other = runs.start([("google", "Cafe", lambda: None)], runs.record_log, owner=2)
            self.assertIsNotNone(runs.snapshot(other, owner=2))
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
