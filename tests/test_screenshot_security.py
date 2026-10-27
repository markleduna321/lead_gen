import contextlib
import importlib
import io
import os
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER_ID, signed_in_client

import net_guard
import r2_storage
import screenshot_agent

R2_ENV = {"R2_ACCOUNT_ID": "acct123", "R2_ACCESS_KEY_ID": "AKIAQAONLY",
          "R2_SECRET_ACCESS_KEY": "qa-only-secret", "R2_BUCKET": "qa-bucket"}
KEY = "screenshots/" + "a" * 32 + ".png"


def fake_resolver(mapping):
    def getaddrinfo(host, port, *args, **kwargs):
        ip = mapping.get(host, host)
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
    return getaddrinfo


class UrlGuardTests(unittest.TestCase):
    def test_internal_and_malformed_targets_are_rejected(self):
        hostile = ["http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/",
                   "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                   "http://0.0.0.0/", "file:///etc/passwd", "ftp://example.com/",
                   "http://user:pw@example.com/", "http://example.com:22/", "http:///nohost"]
        resolver = fake_resolver({"localhost": "127.0.0.1", "example.com": "93.184.216.34"})
        with patch("socket.getaddrinfo", resolver):
            for url in hostile:
                with self.subTest(url=url), self.assertRaises(net_guard.UnsafeTarget):
                    net_guard.check_url(url)

    def test_a_public_name_that_resolves_privately_is_rejected(self):
        with patch("socket.getaddrinfo", fake_resolver({"rebind.example": "127.0.0.1"})):
            with self.assertRaises(net_guard.UnsafeTarget):
                net_guard.check_url("https://rebind.example/")

    def test_public_websites_are_allowed(self):
        with patch("socket.getaddrinfo", fake_resolver({"example.com": "93.184.216.34"})):
            addresses = net_guard.check_url("https://example.com/contact")
        self.assertEqual(addresses[0][1], ("93.184.216.34", 443))


class _CaptureHandler(socketserver.BaseRequestHandler):
    received = []

    def handle(self):
        data = self.request.recv(65536)
        type(self).received.append(data)
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")


class FilteringProxyTests(unittest.TestCase):
    def setUp(self):
        self.proxy = net_guard.FilteringProxy()
        self.addCleanup(self.proxy.close)

    def send(self, raw):
        with socket.create_connection(("127.0.0.1", self.proxy.port), timeout=5) as sock:
            sock.sendall(raw)
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks)

    def test_internal_destinations_are_refused(self):
        for raw in [b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
                    b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n",
                    b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n",
                    b"GET http://10.1.2.3:8080/admin HTTP/1.1\r\n\r\n"]:
            with self.subTest(raw=raw):
                self.assertTrue(self.send(raw).startswith(b"HTTP/1.1 403"))

    def test_public_requests_are_relayed_to_the_checked_address(self):
        upstream = socketserver.TCPServer(("127.0.0.1", 0), _CaptureHandler)
        self.addCleanup(upstream.server_close)
        threading.Thread(target=upstream.handle_request, daemon=True).start()
        address = upstream.server_address
        _CaptureHandler.received = []
        with patch.object(net_guard, "resolve_public",
                          return_value=[(socket.AF_INET, address)]) as resolve:
            reply = self.send(b"GET http://public.example/page?q=1 HTTP/1.1\r\n"
                              b"Host: public.example\r\nProxy-Connection: keep-alive\r\n\r\n")
        self.assertEqual(resolve.call_args.args[:2], ("public.example", 80))
        self.assertTrue(reply.endswith(b"ok"))
        forwarded = _CaptureHandler.received[0].decode()
        self.assertTrue(forwarded.startswith("GET /page?q=1 HTTP/1.1\r\n"))
        self.assertNotIn("Proxy-Connection", forwarded)


class ScreenshotAgentTests(unittest.TestCase):
    def test_chrome_keeps_its_sandbox_and_is_forced_through_the_proxy(self):
        arguments = screenshot_agent.chrome_options("http://127.0.0.1:9999").arguments
        self.assertNotIn("--no-sandbox", arguments)
        self.assertIn("--proxy-server=http://127.0.0.1:9999", arguments)
        self.assertIn("--proxy-bypass-list=<-loopback>", arguments)

    def test_capture_is_skipped_without_r2_or_for_internal_urls(self):
        with patch("selenium.webdriver.Chrome") as chrome:
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(screenshot_agent.capture_business_site_snapshot("https://example.com", "p1"))
            with patch.dict(os.environ, R2_ENV):
                self.assertIsNone(screenshot_agent.capture_business_site_snapshot("http://127.0.0.1:8080/", "p1"))
        chrome.assert_not_called()

    def test_capture_uploads_privately_and_returns_the_key(self):
        driver = MagicMock(current_url="https://example.com/")
        driver.get_screenshot_as_png.return_value = b"png-bytes"
        proxy = MagicMock(url="http://127.0.0.1:7777")
        with (patch.dict(os.environ, R2_ENV),
              patch("socket.getaddrinfo", fake_resolver({"example.com": "93.184.216.34"})),
              patch.object(net_guard, "shared_proxy", return_value=proxy),
              patch("selenium.webdriver.Chrome", return_value=driver) as chrome,
              patch.object(r2_storage, "upload_png", return_value=KEY) as upload, \
              patch("time.sleep")):
            key = screenshot_agent.capture_business_site_snapshot("https://example.com", "../../etc")
        self.assertEqual(key, KEY)
        upload.assert_called_once_with(b"png-bytes")
        self.assertIn("--proxy-server=http://127.0.0.1:7777", chrome.call_args.kwargs["options"].arguments)
        driver.quit.assert_called_once()


class R2StorageTests(unittest.TestCase):
    def test_keys_are_random_and_strictly_validated(self):
        self.assertTrue(r2_storage.is_screenshot_key(r2_storage.new_screenshot_key()))
        self.assertNotEqual(r2_storage.new_screenshot_key(), r2_storage.new_screenshot_key())
        for bad in ["/static/screenshots/snap_x.png", "screenshots/../x.png", None, KEY + "x"]:
            self.assertFalse(r2_storage.is_screenshot_key(bad))

    def test_signed_links_are_private_and_short_lived(self):
        with patch.dict(os.environ, R2_ENV):
            url = r2_storage.signed_url(KEY)
        parts = urlsplit(url)
        self.assertEqual(parts.netloc, "acct123.r2.cloudflarestorage.com")
        self.assertEqual(parts.path, f"/qa-bucket/{KEY}")
        self.assertEqual(parse_qs(parts.query)["X-Amz-Expires"], ["300"])
        self.assertNotIn("qa-only-secret", url)


class ScreenshotRouteTests(unittest.TestCase):
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

    def request(self, row, client=None):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = row
        client = client or signed_in_client(self.module.app)
        with (patch.dict(os.environ, R2_ENV),
              patch("psycopg2.connect", return_value=database),
              patch.object(r2_storage, "signed_url", return_value="https://signed.example/x") as sign):
            response = client.get("/api/prospects/42/screenshot")
        return response, cursor, sign

    def test_owner_is_redirected_to_a_signed_link(self):
        response, cursor, sign = self.request({"screenshot_path": KEY})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://signed.example/x")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(cursor.execute.call_args.args[1], (42, QA_USER_ID))
        sign.assert_called_once_with(KEY)

    def test_other_accounts_and_legacy_paths_get_not_found(self):
        for row in [None, {"screenshot_path": None},
                    {"screenshot_path": "/static/screenshots/snap_x.png"}]:
            with self.subTest(row=row):
                response, _, sign = self.request(row)
                self.assertEqual(response.status_code, 404)
                sign.assert_not_called()

    def test_anonymous_callers_and_legacy_static_files_are_blocked(self):
        anonymous = self.module.app.test_client()
        self.assertEqual(anonymous.get("/api/prospects/42/screenshot").status_code, 401)
        for client in [anonymous, signed_in_client(self.module.app)]:
            self.assertEqual(client.get("/static/screenshots/snap_x.png").status_code, 404)


class LegacyMigrationTests(unittest.TestCase):
    def run_migration(self, rows, dry_run=False):
        import manage_screenshots
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "snap_abc.png").write_bytes(b"old-png")
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = rows
        storage = MagicMock()
        storage.upload_png.return_value = KEY
        with patch("psycopg2.connect", return_value=database):
            summary = manage_screenshots.upload_legacy("qa-only", folder, dry_run, storage)
        updates = [call.args for call in cursor.execute.call_args_list if call.args[0].startswith("UPDATE")]
        return summary, updates, storage, folder

    def test_files_are_uploaded_once_relinked_and_deleted(self):
        rows = [{"id": 1, "screenshot_path": "/static/screenshots/snap_abc.png"},
                {"id": 2, "screenshot_path": "/static/screenshots/snap_abc.png"},
                {"id": 3, "screenshot_path": "/static/screenshots/snap_gone.png"},
                {"id": 4, "screenshot_path": "/static/screenshots/../../.env"}]
        summary, updates, storage, folder = self.run_migration(rows)
        storage.upload_png.assert_called_once_with(b"old-png")
        self.assertEqual(summary, {"uploaded": 1, "relinked": 2, "missing": 2, "deleted_files": 1})
        relinked = [params for query, params in updates if "= NULL" not in query]
        cleared = [params for query, params in updates if "= NULL" in query]
        self.assertEqual([(key, lead_id) for key, lead_id, _ in relinked], [(KEY, 1), (KEY, 2)])
        self.assertEqual([lead_id for lead_id, _ in cleared], [3, 4])
        self.assertFalse((folder / "snap_abc.png").exists())

    def test_dry_run_changes_nothing(self):
        rows = [{"id": 1, "screenshot_path": "/static/screenshots/snap_abc.png"}]
        summary, updates, storage, folder = self.run_migration(rows, dry_run=True)
        storage.upload_png.assert_not_called()
        self.assertEqual(updates, [])
        self.assertEqual(summary["relinked"], 1)
        self.assertTrue((folder / "snap_abc.png").exists())


if __name__ == "__main__":
    unittest.main()
