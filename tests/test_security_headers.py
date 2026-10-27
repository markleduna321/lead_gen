import contextlib
import importlib
import io
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from support import signed_in_client

import rate_limit
import security_headers


class SecurityHeaderTests(unittest.TestCase):
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
        clock = lambda: 1_000_000.0
        patcher = patch.dict(self.api.app.config,
                             {"RATE_LIMITER": rate_limit.Limiter(rate_limit.MemoryStore(clock), clock)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api.CSP_REPORTS_SEEN.clear()

    def assert_hardened(self, response):
        headers = response.headers
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertIn("camera=()", headers["Permissions-Policy"])
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
        csp = headers["Content-Security-Policy"]
        for directive in ["default-src 'self'", "object-src 'none'", "base-uri 'self'",
                          "form-action 'self'", "frame-ancestors 'none'"]:
            self.assertIn(directive, csp)
        report_only = headers["Content-Security-Policy-Report-Only"]
        self.assertIn("script-src 'self';", report_only)
        self.assertIn("report-uri /api/csp-report", report_only)

    def test_pages_api_errors_and_static_files_all_get_headers(self):
        client = self.api.app.test_client()
        responses = {
            "page": client.get("/login"),
            "redirect": client.get("/prospects"),
            "api 401": client.get("/api/queue"),
            "404": client.get("/no-such-page"),
            "static": client.get("/static/vendor/leaflet-1.9.4/leaflet.js"),
            "signed-in api": signed_in_client(self.api.app).get("/api/queue?page=abc"),
        }
        for name, response in responses.items():
            with self.subTest(name):
                self.assert_hardened(response)
                response.close()

    def test_hsts_only_on_https_outside_local_mode(self):
        self.assertNotIn("Strict-Transport-Security",
                         self.api.app.test_client().get("/login", base_url="https://app.test").headers)
        from flask import Flask
        hosted = Flask("hosted")
        hosted.add_url_rule("/", "index", lambda: "ok")
        security_headers.install(hosted, "production")
        client = hosted.test_client()
        self.assertNotIn("Strict-Transport-Security", client.get("/").headers)
        self.assertEqual(client.get("/", base_url="https://app.test").headers["Strict-Transport-Security"],
                         "max-age=31536000")

    def test_route_specific_values_are_kept(self):
        from flask import Flask
        app = Flask("custom")

        @app.route("/")
        def index():
            return "ok", 200, {"Referrer-Policy": "no-referrer"}

        security_headers.install(app, "local")
        self.assertEqual(app.test_client().get("/").headers["Referrer-Policy"], "no-referrer")

    def test_templates_load_no_third_party_scripts_styles_or_fonts(self):
        external = re.compile(r'<(script|link)[^>]+(src|href)="https?://', re.I)
        for template in (ROOT / "src" / "templates").glob("*.html"):
            with self.subTest(template.name):
                self.assertIsNone(external.search(template.read_text(encoding="utf-8")))
        for stylesheet in (ROOT / "static").glob("*.css"):
            with self.subTest(stylesheet.name):
                self.assertNotRegex(stylesheet.read_text(encoding="utf-8"), r"@import\s+url\(['\"]?https?:")
        for asset in ["vendor/tailwindcss-browser-4.3.3/tailwind.js", "vendor/leaflet-1.9.4/leaflet.js",
                      "vendor/leaflet-1.9.4/leaflet.css", "vendor/leaflet-1.9.4/images/marker-icon.png",
                      "vendor/fonts/fonts.css", "vendor/fonts/dm-sans-latin-wght-normal.woff2",
                      "vendor/fonts/manrope-latin-wght-normal.woff2"]:
            with self.subTest(asset):
                self.assertTrue((ROOT / "static" / asset).is_file())

    def test_csp_reports_are_public_logged_once_and_sanitized(self):
        client = self.api.app.test_client()
        report = {"csp-report": {
            "document-uri": "https://app.test/prospects?token=secret#frag",
            "effective-directive": "script-src-attr",
            "blocked-uri": "inline\r\nFAKE LOG LINE",
            "source-file": "https://app.test/prospects?x=1", "line-number": 42,
        }}
        with self.assertLogs(self.api.app.logger, "WARNING") as logs:
            for _ in range(3):
                response = client.post("/api/csp-report", json=report,
                                       headers={"Content-Type": "application/csp-report"})
                self.assertEqual(response.status_code, 204)
        self.assertEqual(len(logs.output), 1, "duplicates are logged once")
        line = logs.output[0]
        self.assertIn("directive=script-src-attr", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("\r", line)
        self.assertNotIn("\n", line)

    def test_csp_reports_ignore_junk_and_are_rate_limited(self):
        client = self.api.app.test_client()
        for body in [b"not json", b"[1, 2]", b'{"csp-report": "x"}', b"x" * 20000]:
            with self.subTest(body=body[:20]):
                self.assertEqual(client.post("/api/csp-report", data=body).status_code, 204)
        with self.assertLogs(self.api.app.logger, "WARNING") as logs:
            for n in range(rate_limit.CSP_REPORT.max_hits + 10):
                client.post("/api/csp-report", json={"csp-report": {"blocked-uri": f"https://x{n}.test"}})
        self.assertEqual(len(logs.output), rate_limit.CSP_REPORT.max_hits)


if __name__ == "__main__":
    unittest.main()
