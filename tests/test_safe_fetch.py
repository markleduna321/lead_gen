from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import email_hunter
import net_guard
import website_checker

BIG = b"x" * (net_guard.MAX_FETCH_BYTES + 1)


class _Site(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        status, headers, body = self.routes.get(self.path, (404, {}, b""))
        self.send_response(status)
        for name, value in {"Content-Type": "text/html; charset=utf-8", **headers}.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SafeFetchTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        proxy = net_guard.FilteringProxy()
        self.addCleanup(proxy.close)
        self.lookups = []
        public = {"site.example", "www.site.example", "other.example"}

        def resolve(host, port, *args):
            self.lookups.append(host)
            if host not in public:
                raise net_guard.UnsafeTarget(f"{host} resolves to a non-public address.")
            return [(socket.AF_INET, self.server.server_address)]

        for patcher in (patch.object(net_guard, "resolve_public", side_effect=resolve),
                        patch.object(net_guard, "shared_proxy", return_value=proxy)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def url(self, host, path="/"):
        return f"http://{host}:{self.port}{path}"

    def test_public_pages_are_fetched_through_the_proxy(self):
        _Site.routes = {"/": (200, {}, b"<p>hello@site.example</p>")}
        response = net_guard.safe_get(self.url("site.example"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_html)
        self.assertIn("hello@site.example", response.text)
        self.assertEqual(self.lookups, ["site.example", "site.example"])

    def test_same_site_redirects_are_followed(self):
        _Site.routes = {"/": (301, {"Location": self.url("www.site.example", "/home")}, b""),
                        "/home": (200, {}, b"<p>home</p>")}
        response = net_guard.safe_get(self.url("site.example"))
        self.assertEqual(response.url, self.url("www.site.example", "/home"))

    def test_redirects_to_other_websites_or_internal_hosts_are_refused(self):
        for location in [self.url("other.example"), "http://127.0.0.1/admin",
                         "http://169.254.169.254/latest/meta-data/"]:
            _Site.routes = {"/": (302, {"Location": location}, b"")}
            with self.subTest(location=location), self.assertRaises(net_guard.UnsafeTarget):
                net_guard.safe_get(self.url("site.example"))

    def test_redirect_loops_stop(self):
        _Site.routes = {"/": (302, {"Location": "/"}, b"")}
        with self.assertRaisesRegex(net_guard.FetchError, "too many"):
            net_guard.safe_get(self.url("site.example"))

    def test_internal_targets_are_refused_before_connecting(self):
        for url in ["http://localhost/", "http://10.0.0.5/", "file:///etc/passwd"]:
            with self.subTest(url=url), self.assertRaises(net_guard.UnsafeTarget):
                net_guard.safe_get(url)

    def test_dns_rebinding_is_caught_by_the_proxy(self):
        answers = iter([[(socket.AF_INET, ("93.184.216.34", self.port))]])

        def rebinding(host, port, *args):
            try:
                return next(answers)
            except StopIteration:
                raise net_guard.UnsafeTarget("rebound to 127.0.0.1") from None

        with patch.object(net_guard, "resolve_public", side_effect=rebinding):
            with self.assertRaises(net_guard.UnsafeTarget):
                net_guard.safe_get(self.url("rebind.example"))

    def test_oversized_pages_are_cut_off(self):
        _Site.routes = {"/": (200, {}, BIG)}
        with self.assertRaisesRegex(net_guard.FetchError, "download limit"):
            net_guard.safe_get(self.url("site.example"))

    def test_email_crawler_uses_the_guard(self):
        _Site.routes = {"/": (200, {}, b"<p>No email here</p>"),
                        "/contact": (200, {"Content-Type": "application/pdf"}, b"pdf@site.example"),
                        "/about": (200, {}, b'<a href="mailto:Owner@Site.example?subject=hi">Mail</a>')}
        self.assertEqual(email_hunter.attempt_domain_email_crawl(self.url("site.example", "/x")),
                         "owner@site.example")

    def test_email_crawler_stops_on_internal_or_cross_site_targets(self):
        _Site.routes = {"/": (302, {"Location": self.url("other.example")}, b"")}
        self.assertIsNone(email_hunter.attempt_domain_email_crawl(self.url("site.example")))
        self.assertIsNone(email_hunter.attempt_domain_email_crawl("http://127.0.0.1:5000/"))


class WebsiteCheckerTests(unittest.TestCase):
    def test_internal_urls_are_not_sent_to_pagespeed(self):
        with (patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")),
              patch("requests.get", side_effect=AssertionError("PageSpeed must not be called")) as get):
            for url in ["http://127.0.0.1:5432/", "http://localhost/", "http://unknown.invalid/"]:
                with self.subTest(url=url):
                    result = website_checker.check_website_quality(url)
                    self.assertIsNone(result["score"])
                    self.assertTrue(result["error"])
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
