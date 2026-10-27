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
from support import QA_USER, signed_in_client, with_csrf

import auth
from plans import PlanError
import rate_limit
from rate_limit import Limit, Limiter, MemoryStore, RateLimited


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class LimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(600.0)
        self.limiter = Limiter(MemoryStore(self.clock), self.clock)
        self.limit = Limit("test", 3, 60, "Slow down. Try again in {wait}.")

    def test_allows_the_limit_then_rejects_with_retry_after(self):
        for _ in range(3):
            self.limiter.hit(self.limit, "user-1")
        self.clock.now = 615.0
        with self.assertRaises(RateLimited) as raised:
            self.limiter.hit(self.limit, "user-1")
        self.assertEqual(raised.exception.retry_after, 45)
        self.assertEqual(str(raised.exception), "Slow down. Try again in 45 seconds.")
        self.limiter.hit(self.limit, "user-2")

    def test_a_new_window_starts_fresh(self):
        for _ in range(3):
            self.limiter.hit(self.limit, "user-1")
        self.clock.now = 660.0
        self.limiter.hit(self.limit, "user-1")

    def test_check_does_not_count_and_reset_clears(self):
        for _ in range(5):
            self.limiter.check(self.limit, "key")
        for _ in range(3):
            self.limiter.record(self.limit, "key")
        with self.assertRaises(RateLimited):
            self.limiter.check(self.limit, "key")
        self.limiter.reset(self.limit, "key")
        self.limiter.check(self.limit, "key")

    def test_raw_keys_are_never_stored(self):
        self.limiter.hit(self.limit, "qa@example.test|203.0.113.7")
        stored = " ".join(self.limiter.store.buckets)
        self.assertNotIn("example.test", stored)
        self.assertNotIn("203.0.113.7", stored)

    def test_waits_are_described_in_plain_words(self):
        self.assertEqual(rate_limit.describe_wait(1), "1 second")
        self.assertEqual(rate_limit.describe_wait(60), "1 minute")
        self.assertEqual(rate_limit.describe_wait(61), "2 minutes")
        self.assertEqual(rate_limit.describe_wait(900), "15 minutes")


class RateLimitedRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.api = importlib.import_module("control_gate_api")

    def setUp(self):
        self.clock = Clock()
        limiter = Limiter(MemoryStore(self.clock), self.clock)
        patcher = patch.dict(self.api.app.config, {"RATE_LIMITER": limiter})
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, email, password="pw", ip="203.0.113.7"):
        return with_csrf(self.api.app.test_client()).post(
            "/login", data={"email": email, "password": password},
            environ_base={"REMOTE_ADDR": ip})

    def test_failed_sign_ins_block_only_that_email_from_that_network(self):
        with patch("auth.authenticate", side_effect=auth.AuthError(auth.GENERIC_SIGN_IN_ERROR)):
            for _ in range(rate_limit.LOGIN_FAILURES.max_hits):
                self.assertEqual(self.login("qa@example.test").status_code, 401)
        with patch("auth.authenticate", return_value=QA_USER) as correct:
            blocked = self.login("QA@example.test ")
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("Too many failed sign-in attempts", blocked.get_data(as_text=True))
            self.assertIn("Retry-After", blocked.headers)
            correct.assert_not_called()
            # The real owner signing in from elsewhere is unaffected.
            self.assertEqual(self.login("qa@example.test", ip="198.51.100.4").status_code, 302)
            self.assertEqual(self.login("other@example.test").status_code, 302)

    def test_unknown_emails_are_throttled_exactly_like_real_ones(self):
        with patch("auth.authenticate", side_effect=auth.AuthError(auth.GENERIC_SIGN_IN_ERROR)):
            for _ in range(rate_limit.LOGIN_FAILURES.max_hits):
                self.login("nobody@example.test")
            unknown = self.login("nobody@example.test")
        self.assertEqual(unknown.status_code, 429)
        self.assertIn("Too many failed sign-in attempts", unknown.get_data(as_text=True))

    def test_successful_sign_in_clears_earlier_failures(self):
        with patch("auth.authenticate", side_effect=auth.AuthError(auth.GENERIC_SIGN_IN_ERROR)):
            for _ in range(rate_limit.LOGIN_FAILURES.max_hits - 1):
                self.login("qa@example.test")
        with patch("auth.authenticate", return_value=QA_USER):
            self.assertEqual(self.login("qa@example.test").status_code, 302)
        with patch("auth.authenticate", side_effect=auth.AuthError(auth.GENERIC_SIGN_IN_ERROR)):
            for _ in range(rate_limit.LOGIN_FAILURES.max_hits - 1):
                self.assertEqual(self.login("qa@example.test").status_code, 401)

    def test_one_network_cannot_spray_many_accounts(self):
        with patch("auth.authenticate", side_effect=auth.AuthError(auth.GENERIC_SIGN_IN_ERROR)):
            for n in range(rate_limit.LOGIN_IP.max_hits):
                self.assertEqual(self.login(f"user{n}@example.test").status_code, 401)
            self.assertEqual(self.login("fresh@example.test").status_code, 429)
            self.assertEqual(self.login("fresh@example.test", ip="198.51.100.4").status_code, 401)

    def test_registration_is_limited_per_network(self):
        client = with_csrf(self.api.app.test_client())
        form = {"email": "new@example.test", "password": "a", "confirm_password": "b"}
        for _ in range(rate_limit.REGISTER_IP.max_hits):
            self.assertEqual(client.post("/register", data=form).status_code, 400)
        blocked = client.post("/register", data=form)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Too many sign-up attempts", blocked.get_data(as_text=True))

    def test_api_limits_return_json_429_with_retry_after(self):
        client = signed_in_client(self.api.app)
        not_found = PlanError("Lead not found.", "lead_not_found", 404)
        with patch.object(self.api, "OPENAI_KEY", "sk-test"), \
                patch("billing.reserve_pitch", side_effect=not_found) as reserve:
            for _ in range(rate_limit.PITCH.max_hits):
                self.assertEqual(client.post("/api/trigger/pitch-single/1").status_code, 404)
            limited = client.post("/api/trigger/pitch-single/1")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json["code"], "rate_limited")
        self.assertIn("drafting pitches too quickly", limited.json["message"])
        self.assertTrue(limited.headers["Retry-After"].isdigit())
        self.assertEqual(reserve.call_count, rate_limit.PITCH.max_hits)

    def test_smtp_test_dispatch_and_quote_are_limited(self):
        client = signed_in_client(self.api.app)
        cases = [
            (rate_limit.SMTP_TEST, "/api/email-settings/test", None),
            (rate_limit.DISPATCH, "/api/dispatch/1", {"final_pitch": "Hello"}),
            (rate_limit.QUOTE, "/api/discovery/quote", {}),
        ]
        for limit, path, body in cases:
            with self.subTest(path=path):
                for _ in range(limit.max_hits):
                    self.api.app.config["RATE_LIMITER"].record(limit, 1)
                with patch("user_email.send_test_email") as send, \
                        patch("user_email.sender_identity") as sender, \
                        patch("billing.quote") as quote:
                    response = client.post(path, json=body)
                self.assertEqual(response.status_code, 429, response.get_data(as_text=True))
                self.assertEqual(response.json["code"], "rate_limited")
                send.assert_not_called()
                sender.assert_not_called()
                quote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
