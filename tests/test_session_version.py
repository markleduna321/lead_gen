import contextlib
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER, signed_in_client

import auth
import rate_limit
from rate_limit import Limiter, MemoryStore


class SessionVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.api = importlib.import_module("control_gate_api")
        cls.app = cls.api.app

    def setUp(self):
        patcher = patch.dict(self.app.config, {"RATE_LIMITER": Limiter(MemoryStore())})
        patcher.start()
        self.addCleanup(patcher.stop)

    def client_at(self, stored_version, account_version):
        client = signed_in_client(self.app)
        with client.session_transaction() as saved:
            if stored_version is None:
                saved.pop(auth.SESSION_VERSION_KEY, None)
            else:
                saved[auth.SESSION_VERSION_KEY] = stored_version
        account = dict(QA_USER, session_version=account_version)
        patcher = patch("auth.load_user", return_value=account)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_revoked_session_is_signed_out(self):
        client = self.client_at(1, 2)
        self.assertEqual(client.get("/api/session").status_code, 401)
        self.assertIn("/login", client.get("/overview").headers["Location"])
        with client.session_transaction() as saved:
            self.assertNotIn("user_id", saved)

    def test_matching_session_is_kept(self):
        self.assertEqual(self.client_at(4, 4).get("/api/session").status_code, 200)

    def test_pre_versioning_session_is_adopted_only_if_never_revoked(self):
        legacy = self.client_at(None, 1)
        self.assertEqual(legacy.get("/api/session").status_code, 200)
        with legacy.session_transaction() as saved:
            self.assertEqual(saved[auth.SESSION_VERSION_KEY], 1)
        patch.stopall()
        self.assertEqual(self.client_at(None, 2).get("/api/session").status_code, 401)

    def test_session_api_does_not_expose_the_version(self):
        user = self.client_at(3, 3).get("/api/session").get_json()["user"]
        self.assertEqual(user["id"], QA_USER["id"])
        self.assertNotIn("session_version", user)

    def test_sign_in_stores_the_account_version(self):
        with self.app.test_request_context():
            auth.sign_in_session(dict(QA_USER, session_version=7))
            self.assertEqual(auth.session_is_current({"session_version": 7}), True)
            self.assertEqual(auth.session_is_current({"session_version": 8}), False)

    def test_password_change_moves_this_session_to_the_new_version(self):
        client = self.client_at(1, 1)
        with patch("auth.change_password", return_value=2) as change:
            response = client.post("/api/account/password", json={
                "current_password": "old", "new_password": "x" * 12, "confirm_password": "x" * 12})
        self.assertEqual(response.status_code, 200)
        self.assertIn("other devices were signed out", response.get_json()["message"])
        change.assert_called_once_with(None, QA_USER["id"], "old", "x" * 12, "x" * 12)
        with client.session_transaction() as saved:
            self.assertEqual(saved[auth.SESSION_VERSION_KEY], 2)

    def test_password_change_errors_are_shown_and_attempts_are_limited(self):
        client = self.client_at(1, 1)
        failure = auth.AuthError("Your current password is not correct.")
        with patch("auth.change_password", side_effect=failure):
            for _ in range(rate_limit.PASSWORD_CHANGE.max_hits):
                response = client.post("/api/account/password", json=["not", "an", "object"])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["message"], str(failure))
            blocked = client.post("/api/account/password", json={})
        self.assertEqual(blocked.status_code, 429)

    def test_sign_out_everywhere_revokes_and_ends_this_session(self):
        client = self.client_at(1, 1)
        with patch("auth.end_all_sessions") as revoke:
            response = client.post("/api/account/sign-out-everywhere")
        self.assertEqual(response.get_json(), {"status": "success", "redirect": "/login"})
        revoke.assert_called_once_with(None, QA_USER["id"])
        with client.session_transaction() as saved:
            self.assertNotIn("user_id", saved)

    def test_change_password_validates_before_touching_the_database(self):
        cases = [
            ("", "long-enough-1", "long-enough-1", "Enter your current password."),
            ("old", "short", "short", "Use a password between"),
            ("old", "long-enough-1", "long-enough-2", "Both new passwords must match."),
            ("long-enough-1", "long-enough-1", "long-enough-1", "Choose a password you are not already using."),
        ]
        for current, new, confirm, message in cases:
            with self.subTest(message=message), self.assertRaises(auth.AuthError) as raised:
                auth.change_password("unused", 1, current, new, confirm)
            self.assertIn(message, str(raised.exception))

    def test_settings_page_shows_account_security(self):
        with patch("billing.wallet_summary", side_effect=Exception("skip")):
            html = self.client_at(1, 1).get("/settings").get_data(as_text=True)
        self.assertIn('id="password-change-form"', html)
        self.assertIn('id="btn-sign-out-everywhere"', html)
        self.assertIn("account_security.js", html)


if __name__ == "__main__":
    unittest.main()
