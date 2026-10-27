import contextlib
import importlib
import io
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER, signed_in_client, with_csrf

import csrf

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
SAMPLE_ARGS = {"int": "1", "uuid": str(uuid.uuid4())}


class CsrfTests(unittest.TestCase):
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

    def tokenless_client(self):
        client = signed_in_client(self.app)
        del client.environ_base["HTTP_X_CSRF_TOKEN"]
        return client

    def mutating_routes(self):
        for rule in self.app.url_map.iter_rules():
            path = re.sub(r"<(?:(\w+)(?:\([^)]*\))?:)?\w+>",
                          lambda m: SAMPLE_ARGS.get(m.group(1) or "int", "1"), rule.rule)
            for method in sorted(MUTATING & rule.methods):
                yield rule.endpoint, method, path

    def test_every_mutating_route_requires_the_token(self):
        routes = list(self.mutating_routes())
        self.assertGreater(len(routes), 25)
        client = self.tokenless_client()
        for endpoint, method, path in routes:
            if endpoint in csrf.EXEMPT_ENDPOINTS:
                continue
            with self.subTest(endpoint=endpoint, method=method):
                response = client.open(path, method=method, json={})
                if endpoint in csrf.AUTH_FORMS:
                    self.assertEqual(response.status_code, 403)
                    self.assertIn(csrf.FORM_EXPIRED_MESSAGE, response.get_data(as_text=True))
                elif endpoint == "sign_out":
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.headers["Location"].endswith("/overview"))
                else:
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.get_json()["code"], "csrf_failed")

    def test_wrong_token_is_rejected_and_right_token_reaches_the_route(self):
        client = signed_in_client(self.app)
        bad = client.post("/api/discovery/logs/clear", headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(bad.status_code, 403)
        good = client.post("/api/discovery/logs/clear")
        self.assertNotEqual(good.status_code, 403)

    def test_form_field_token_is_accepted(self):
        client = self.tokenless_client()
        response = client.post("/logout", data={"csrf_token": "qa-csrf-token"})
        self.assertTrue(response.headers["Location"].endswith("/login"))
        with client.session_transaction() as saved:
            self.assertNotIn("user_id", saved)

    def test_stale_logout_keeps_the_user_signed_in(self):
        client = self.tokenless_client()
        response = client.post("/logout", data={"csrf_token": "stale"})
        self.assertTrue(response.headers["Location"].endswith("/overview"))
        with client.session_transaction() as saved:
            self.assertEqual(saved["user_id"], QA_USER["id"])

    def test_signed_out_requests_get_the_sign_in_response_first(self):
        client = self.app.test_client()
        self.assertIn("/login", client.post("/logout").headers["Location"])
        self.assertEqual(client.post("/api/discovery/logs/clear").status_code, 401)

    def test_expired_login_form_is_shown_again_with_a_fresh_token(self):
        client = self.app.test_client()
        stale = client.post("/login", data={"email": "qa@example.test", "password": "x",
                                             "next": "https://evil.example/", "csrf_token": "old"})
        html = stale.get_data(as_text=True)
        self.assertEqual(stale.status_code, 403)
        self.assertIn(csrf.FORM_EXPIRED_MESSAGE, html)
        self.assertIn('value="qa@example.test"', html)
        self.assertNotIn("evil.example", html)
        with client.session_transaction() as saved:
            fresh = saved["csrf_token"]
        self.assertIn(f'name="csrf_token" value="{fresh}"', html)
        with patch("auth.authenticate", return_value=QA_USER), patch("auth.load_user", return_value=QA_USER):
            signed_in = client.post("/login", data={"email": "qa@example.test", "password": "x",
                                                    "csrf_token": fresh})
        self.assertEqual(signed_in.status_code, 302)
        with client.session_transaction() as saved:
            self.assertNotEqual(saved.get("csrf_token"), fresh)  # rotated at sign-in

    def test_sign_in_ignores_backslash_redirects(self):
        for hostile in ["/\\evil.example", "/%5Cevil.example"]:
            with self.subTest(hostile=hostile):
                client = self.app.test_client()
                client.get("/login")
                with client.session_transaction() as saved:
                    token = saved["csrf_token"]
                with patch("auth.authenticate", return_value=QA_USER), \
                        patch("auth.load_user", return_value=QA_USER):
                    response = client.post(f"/login?next={hostile}",
                                           data={"email": "qa@example.test", "password": "x",
                                                 "next": hostile, "csrf_token": token})
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("evil.example", response.headers["Location"])

    def test_expired_register_form_keeps_name_and_email(self):
        client = self.app.test_client()
        response = client.post("/register", data={"email": "new@example.test", "display_name": "Ana",
                                                  "password": "x", "confirm_password": "x"})
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 403)
        self.assertIn('value="new@example.test"', html)
        self.assertIn('value="Ana"', html)

    def test_cross_site_origin_or_referer_is_rejected_even_with_a_token(self):
        client = signed_in_client(self.app)
        path = "/api/discovery/logs/clear"
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Referer": "https://evil.example/page"}):
            with self.subTest(headers=headers):
                response = client.post(path, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["code"], "csrf_failed")
        self.assertNotEqual(client.post(path, headers={"Origin": "http://localhost"}).status_code, 403)
        self.assertNotEqual(client.post(path, headers={"Referer": "http://localhost/discovery"}).status_code, 403)

    def test_public_base_url_host_is_trusted_behind_a_rewriting_proxy(self):
        client = signed_in_client(self.app)
        path = "/api/discovery/logs/clear"
        headers = {"Origin": "https://leads.example.com"}
        self.assertEqual(client.post(path, headers=headers).status_code, 403)
        with patch.dict(os.environ, {"APP_BASE_URL": "https://leads.example.com"}):
            self.assertNotEqual(client.post(path, headers=headers).status_code, 403)

    def test_exempt_endpoints_work_without_a_token(self):
        client = self.app.test_client()
        report = client.post("/api/csp-report", json={"csp-report": {"violated-directive": "x"}},
                             headers={"Origin": "null"})
        self.assertEqual(report.status_code, 204)
        with patch("payments.receive_webhook") as webhook:
            hook = client.post("/api/paymongo/webhook", data=b"{}")
        self.assertEqual(hook.status_code, 200)
        webhook.assert_called_once()

    def test_auth_forms_and_logout_render_the_hidden_field(self):
        login = with_csrf(self.app.test_client(), "form-token").get("/login").get_data(as_text=True)
        self.assertIn('name="csrf_token" value="form-token"', login)
        with patch("billing.wallet_summary", side_effect=Exception("skip")):
            page = signed_in_client(self.app).get("/settings").get_data(as_text=True)
        self.assertIn('name="csrf_token" value="qa-csrf-token"', page)


if __name__ == "__main__":
    unittest.main()
