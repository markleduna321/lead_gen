import contextlib
import importlib
import io
import logging
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import signed_in_client

SECRET = "relation \"prospects\" does not exist at /srv/app/secret.py"


class ErrorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cls.module = importlib.import_module("control_gate_api")
        cls.app = cls.module.app

    def setUp(self):
        self.client = self.app.test_client()

    def crash_views(self):
        def crash(*args, **kwargs):
            raise RuntimeError(SECRET)
        return patch.dict(self.app.view_functions, {"plans": crash, "maintenance_job_status": crash})

    def test_unknown_page_returns_public_404_page(self):
        response = self.client.get("/no-such-page")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn("Page not found", html)
        self.assertIn('class="home-header"', html)
        self.assertIn('href="/login"', html)
        self.assertNotIn("Back to workspace", html)
        self.assertNotIn("Reference:", html)

    def test_signed_in_404_offers_the_workspace(self):
        response = signed_in_client(self.app).get("/no-such-page")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Back to workspace", response.get_data(as_text=True))

    def test_unknown_api_route_returns_json_404(self):
        response = self.client.get("/api/no-such-thing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "not_found")

    def test_wrong_method_returns_405_with_allow_header(self):
        response = signed_in_client(self.app).get("/api/trigger/score-leads")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.get_json()["code"], "method_not_allowed")
        self.assertIn("POST", response.headers["Allow"])
        page = self.client.put("/plans")
        self.assertEqual(page.status_code, 405)
        self.assertIn("Action not allowed", page.get_data(as_text=True))

    def test_crashes_hide_details_and_log_a_matching_reference(self):
        client = signed_in_client(self.app)
        for path in ["/plans", "/api/jobs/status"]:
            with self.subTest(path=path):
                with self.crash_views(), self.assertLogs(self.app.logger, logging.ERROR) as logs:
                    response = client.get(path)
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 500)
                self.assertNotIn("does not exist", body)
                self.assertNotIn("secret.py", body)
                self.assertNotIn("RuntimeError", body)
                reference = re.search(r'<code>([0-9A-F]{6})</code>|"reference":\s*"([0-9A-F]{6})"', body)
                reference = reference.group(1) or reference.group(2)
                logged = "\n".join(logs.output)
                self.assertIn(reference, logged)
                self.assertIn(SECRET, logged)
                self.assertIn("Traceback", logged)
                if path.startswith("/api/"):
                    self.assertEqual(response.get_json()["code"], "server_error")
                    self.assertEqual(response.get_json()["reference"], reference)
                else:
                    self.assertIn("Something went wrong", body)
                    self.assertIn(f"<code>{reference}</code>", body)

    def test_not_found_is_not_logged(self):
        with patch.object(self.app.logger, "error") as error, patch.object(self.app.logger, "exception") as exc:
            self.client.get("/no-such-page")
        error.assert_not_called()
        exc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
