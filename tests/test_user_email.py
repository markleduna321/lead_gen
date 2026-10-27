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

from cryptography.fernet import Fernet
import user_email

QA_KEY = Fernet.generate_key().decode()

SETTINGS_ROW = {
    "user_id": QA_USER_ID, "smtp_host": "smtp.example.test", "smtp_port": 587,
    "smtp_security": "starttls", "smtp_username": "qa@example.test",
    "smtp_password_encrypted": b"", "from_name": "QA Sender",
    "from_email": "qa@example.test", "verified_at": None,
    "signature_title": "Solutions Architect", "signature_company": "QA Co",
    "signature_phone": "0995 982 2419", "signature_website": "qa.test",
}


def row_with_password(password: str) -> dict:
    with patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}):
        return dict(SETTINGS_ROW, smtp_password_encrypted=user_email.encrypt_secret(password))


class SecretStorageTests(unittest.TestCase):
    def test_passwords_round_trip_and_are_not_stored_in_clear_text(self):
        with patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}):
            blob = user_email.encrypt_secret("super-secret-app-pw")
            self.assertNotIn(b"super-secret-app-pw", blob)
            self.assertEqual(user_email.decrypt_secret(blob), "super-secret-app-pw")

    def test_a_changed_key_fails_loudly_instead_of_sending_with_garbage(self):
        with patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}):
            blob = user_email.encrypt_secret("super-secret-app-pw")
        with patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            with self.assertRaisesRegex(user_email.EmailSettingsError, "SMTP_ENCRYPTION_KEY changed"):
                user_email.decrypt_secret(blob)

    def test_a_missing_or_invalid_key_is_reported_clearly(self):
        with patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": ""}):
            with self.assertRaisesRegex(user_email.EmailSettingsError, "SMTP_ENCRYPTION_KEY is not set"):
                user_email.encrypt_secret("x")
        with patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": "not-a-fernet-key"}):
            with self.assertRaisesRegex(user_email.EmailSettingsError, "not a valid Fernet key"):
                user_email.encrypt_secret("x")


class SettingsValidationTests(unittest.TestCase):
    def test_required_fields_and_ranges_are_enforced(self):
        base = {"smtp_host": "smtp.example.test", "smtp_username": "qa@example.test",
                "smtp_password": "pw"}
        for payload, expected in [
            ({}, "sending email"),
            (dict(base, smtp_host=""), "Mail server is required"),
            (dict(base, smtp_port=0), "Port must be 587"),
            (dict(base, smtp_port="many"), "Port must be 587"),
            (dict(base, smtp_port=True), "Port must be 587"),
            (dict(base, smtp_port=25), "Port must be 587"),
            (dict(base, smtp_port=5432), "Port must be 587"),
            (dict(base, smtp_security="plaintext"), "starttls"),
            (dict(base, from_email="not-an-email"), "valid sending email"),
            (dict(base, smtp_host="smtp\n.test"), "control characters"),
            ("not-a-dict", "JSON object"),
        ]:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(user_email.EmailSettingsError, expected):
                    user_email.validate_settings(payload)

    def test_a_blank_password_is_only_allowed_when_one_is_already_stored(self):
        payload = {"smtp_host": "smtp.example.test", "smtp_username": "qa@example.test"}
        with self.assertRaisesRegex(user_email.EmailSettingsError, "Enter your mail password"):
            user_email.validate_settings(payload, has_existing_password=False)
        kept = user_email.validate_settings(payload, has_existing_password=True)
        self.assertIsNone(kept["smtp_password"], "an unchanged password must not be re-encrypted")

    def test_internal_mail_servers_are_rejected(self):
        base = {"smtp_username": "qa@example.test", "smtp_password": "pw"}
        for host in ["localhost", "127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254",
                     "[::1]", "::1", "db.internal", "printer.local", "mail", "smtp.example.test:25",
                     "http://smtp.example.test", "user@smtp.example.test"]:
            with self.subTest(host=host), self.assertRaisesRegex(user_email.EmailSettingsError,
                                                                 "public mail server"):
                user_email.validate_settings(dict(base, smtp_host=host))
        for port in (465, 587, 2525, "2525"):
            with self.subTest(port=port):
                values = user_email.validate_settings(dict(base, smtp_host="SMTP.Gmail.com.", smtp_port=port))
                self.assertEqual(values["smtp_host"], "smtp.gmail.com")
                self.assertEqual(values["smtp_port"], int(port))

    def test_the_port_defaults_to_the_selected_security_mode(self):
        base = {"smtp_host": "smtp.example.test", "smtp_username": "qa@example.test",
                "smtp_password": "pw"}
        self.assertEqual(user_email.validate_settings(base)["smtp_port"], 587)
        self.assertEqual(
            user_email.validate_settings(dict(base, smtp_security="ssl"))["smtp_port"], 465)


class SignatureTests(unittest.TestCase):
    def test_the_signature_uses_the_sending_account_not_a_shared_identity(self):
        with patch.object(user_email, "_load_raw", return_value=dict(SETTINGS_ROW)):
            signature = user_email.build_signature("qa-only", QA_USER_ID)
        for expected in ["QA Sender", "Solutions Architect", "QA Co",
                         "0995 982 2419", "qa.test", "qa@example.test"]:
            self.assertIn(expected, signature)

    def test_blank_signature_fields_are_omitted_and_unconfigured_yields_nothing(self):
        sparse = dict(SETTINGS_ROW, signature_title=None, signature_company=None,
                      signature_phone=None, signature_website=None)
        with patch.object(user_email, "_load_raw", return_value=sparse):
            signature = user_email.build_signature("qa-only", QA_USER_ID)
        self.assertIn("QA Sender", signature)
        self.assertNotIn("Phone", signature)
        self.assertNotIn("Web", signature)
        with patch.object(user_email, "_load_raw", return_value=None):
            self.assertEqual(user_email.build_signature("qa-only", QA_USER_ID), "")

    def test_signature_fields_cannot_inject_markup(self):
        hostile = dict(SETTINGS_ROW, from_name='<script>alert("x")</script>',
                       signature_company='A & B <b>')
        with patch.object(user_email, "_load_raw", return_value=hostile):
            signature = user_email.build_signature("qa-only", QA_USER_ID)
        self.assertNotIn("<script>", signature)
        self.assertIn("&lt;script&gt;", signature)
        self.assertIn("A &amp; B", signature)


class SendingTests(unittest.TestCase):
    def test_mail_is_sent_from_the_owning_account_with_a_reply_to(self):
        server = MagicMock()
        with (
            patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
            patch.object(user_email, "_load_raw", return_value=row_with_password("app pass-word")),
            patch.object(user_email, "_mark_verified") as verified,
            patch.object(user_email, "_PinnedSMTP", return_value=server) as smtp,
        ):
            server.__enter__.return_value = server
            user_email.send_email("qa-only", QA_USER_ID, "lead@example.test",
                                  "Subject here", "<p>body</p>")
        smtp.assert_called_once_with("smtp.example.test", 587, timeout=user_email.CONNECT_TIMEOUT)
        # Non-Gmail passwords are used exactly as saved, spaces and dashes included.
        server.login.assert_called_once_with("qa@example.test", "app pass-word")
        sender, recipients, raw = server.sendmail.call_args.args
        self.assertEqual(sender, "qa@example.test")
        self.assertEqual(recipients, ["lead@example.test"])
        self.assertIn("QA Sender", raw)
        self.assertIn("Reply-To: qa@example.test", raw)
        verified.assert_called_once()

    def test_ssl_mode_uses_an_implicit_tls_connection(self):
        server = MagicMock()
        row = dict(row_with_password("pw"), smtp_security="ssl", smtp_port=465)
        with (
            patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
            patch.object(user_email, "_load_raw", return_value=row),
            patch.object(user_email, "_mark_verified"),
            patch.object(user_email, "_PinnedSMTP_SSL", return_value=server) as smtp_ssl,
            patch.object(user_email, "_PinnedSMTP", side_effect=AssertionError("must not use plain SMTP")),
        ):
            server.__enter__.return_value = server
            user_email.send_email("qa-only", QA_USER_ID, "lead@example.test", "s", "<p>b</p>")
        self.assertEqual(smtp_ssl.call_args.args[:2], ("smtp.example.test", 465))

    def test_an_unconfigured_account_is_told_to_set_mail_up(self):
        with patch.object(user_email, "_load_raw", return_value=None):
            with self.assertRaises(user_email.EmailNotConfigured):
                user_email.send_email("qa-only", QA_USER_ID, "lead@example.test", "s", "b")
            with self.assertRaises(user_email.EmailNotConfigured):
                user_email.sender_identity("qa-only", QA_USER_ID)

    def test_server_rejections_become_actionable_messages(self):
        import smtplib
        with (
            patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
            patch.object(user_email, "_load_raw", return_value=row_with_password("pw")),
            patch.object(user_email, "_PinnedSMTP", side_effect=smtplib.SMTPAuthenticationError(535, b"nope")),
        ):
            with self.assertRaisesRegex(user_email.EmailSettingsError, "app password"):
                user_email.send_email("qa-only", QA_USER_ID, "lead@example.test", "s", "b")
        import socket
        import ssl
        import net_guard
        cases = [
            (OSError("Connection refused by 10.0.0.5:5432"), "Could not connect to your mail server"),
            (socket.timeout("timed out after banner"), "did not respond in time"),
            (OSError("Could not connect"), "Could not connect to your mail server"),
            (ssl.SSLError("WRONG_VERSION_NUMBER"), "secure connection"),
            (net_guard.UnresolvableHost("Could not resolve smtp.example.test."), "could not be found"),
            (net_guard.UnsafeTarget("smtp.example.test resolves to a non-public address."), "not allowed"),
            (smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out"),
             "Could not connect to your mail server"),
            (smtplib.SMTPRecipientsRefused({"lead@example.test": (550, b"no")}), "refused the message"),
        ]
        for error, message in cases:
            with (
                self.subTest(error=repr(error)),
                patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
                patch.object(user_email, "_load_raw", return_value=row_with_password("pw")),
                patch.object(user_email, "_PinnedSMTP", side_effect=error),
                contextlib.redirect_stderr(io.StringIO()) as log,
            ):
                with self.assertRaises(user_email.EmailSettingsError) as caught:
                    user_email.send_email("qa-only", QA_USER_ID, "lead@example.test", "s", "b")
                self.assertIn(message, str(caught.exception))
                # Raw socket details stay in the server log only.
                for detail in ("10.0.0.5", "5432", "banner", "WRONG_VERSION", "resolves", "unexpectedly"):
                    self.assertNotIn(detail, str(caught.exception))
                self.assertIn(type(error).__name__, log.getvalue())

    def test_saved_settings_with_a_blocked_port_are_refused_before_connecting(self):
        with (
            patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
            patch.object(user_email, "_load_raw", return_value=dict(row_with_password("pw"), smtp_port=5432)),
            patch.object(user_email, "_PinnedSMTP", side_effect=AssertionError("must not connect")),
        ):
            with self.assertRaisesRegex(user_email.EmailSettingsError, "Update it in Settings"):
                user_email.send_email("qa-only", QA_USER_ID, "lead@example.test", "s", "b")

    def send(self, row, recipient="lead@example.test", subject="s"):
        server = MagicMock()
        server.__enter__.return_value = server
        with (
            patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
            patch.object(user_email, "_load_raw", return_value=row),
            patch.object(user_email, "_mark_verified"),
            patch.object(user_email, "_PinnedSMTP", return_value=server),
        ):
            user_email.send_email("qa-only", QA_USER_ID, recipient, subject, "<p>b</p>")
        return server

    def test_only_gmail_app_passwords_have_their_spaces_removed(self):
        gmail = {"smtp_host": "smtp.gmail.com", "smtp_username": "qa@example.test"}
        cases = [
            (gmail, "abcd efgh ijkl mnop", "abcdefghijklmnop"),
            (gmail, " abcd efgh ijkl mnop ", "abcdefghijklmnop"),
            (gmail, "abcdefghijklmnop", "abcdefghijklmnop"),
            (gmail, "my real pass-word", "my real pass-word"),
            (gmail, "ABCD EFGH IJKL MNOP", "ABCD EFGH IJKL MNOP"),
            ({}, "abcd efgh ijkl mnop", "abcd efgh ijkl mnop"),
            ({}, " padded secret ", " padded secret "),
        ]
        for overrides, saved, expected in cases:
            with self.subTest(host=overrides.get("smtp_host", "smtp.example.test"), saved=saved):
                server = self.send(dict(row_with_password(saved), **overrides))
                server.login.assert_called_once_with("qa@example.test", expected)

    def test_headers_cannot_be_injected_through_name_or_subject(self):
        row = dict(row_with_password("pw"), from_name='Evil" <x@evil.test>\r\nBcc: victim@example.test')
        server = self.send(row, subject="Hello\r\nBcc: victim@example.test")
        _, recipients, raw = server.sendmail.call_args.args
        self.assertEqual(recipients, ["lead@example.test"])
        headers = raw.split("\n\n", 1)[0]
        self.assertNotRegex(headers, r"(?mi)^bcc:")
        from_line = next(line for line in headers.splitlines() if line.startswith("From:"))
        self.assertTrue(from_line.rstrip().endswith("<qa@example.test>"), from_line)
        self.assertEqual(server.sendmail.call_args.args[0], "qa@example.test")

    def test_non_ascii_sender_names_are_encoded(self):
        server = self.send(dict(row_with_password("pw"), from_name="José Niño"))
        raw = server.sendmail.call_args.args[2]
        self.assertIn("=?utf-8?", raw)
        self.assertIn("<qa@example.test>", raw)

    def test_invalid_recipients_are_refused_before_loading_settings(self):
        for recipient in [None, "", "  ", "not-an-email", "a@b", "a,b@example.test",
                          "Name <a@example.test>", "a@example.test\r\nBcc: x@example.test", 42]:
            with (self.subTest(recipient=recipient),
                  patch.object(user_email, "_load_raw", side_effect=AssertionError("must not load"))):
                with self.assertRaises(user_email.RecipientError):
                    user_email.send_email("qa-only", QA_USER_ID, recipient, "s", "b")

    def test_recipient_validation_messages(self):
        with self.assertRaisesRegex(user_email.RecipientError, "Enter the prospect's email"):
            user_email.validate_recipient("  ")
        with self.assertRaisesRegex(user_email.RecipientError, "valid recipient"):
            user_email.validate_recipient("nope")
        self.assertEqual(user_email.validate_recipient("  Lead@Example.test "), "Lead@Example.test")


class PinnedConnectionTests(unittest.TestCase):
    def test_smtp_connections_only_reach_public_mail_ports(self):
        import socket
        import net_guard

        def resolver(mapping):
            def getaddrinfo(host, port, *args, **kwargs):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping.get(host, host), port))]
            return getaddrinfo

        with patch("socket.getaddrinfo", resolver({"rebind.example.test": "127.0.0.1"})):
            for cls, host, port in [(user_email._PinnedSMTP, "rebind.example.test", 587),
                                    (user_email._PinnedSMTP, "127.0.0.1", 587),
                                    (user_email._PinnedSMTP_SSL, "169.254.169.254", 465),
                                    (user_email._PinnedSMTP, "smtp.example.test", 5432)]:
                with self.subTest(host=host, port=port), self.assertRaises(net_guard.UnsafeTarget):
                    cls(host, port, timeout=1)

    def test_the_checked_ip_is_used_and_tls_keeps_the_host_name(self):
        import socket
        connected = MagicMock()
        with patch("net_guard.connect_public", return_value=connected) as connect:
            sock = user_email._PinnedSMTP._get_socket(MagicMock(), "smtp.example.test", 587, 5)
        self.assertIs(sock, connected)
        connect.assert_called_once_with("smtp.example.test", 587, allowed_ports=user_email.ALLOWED_PORTS,
                                        timeout=5)
        self.assertIn(user_email._PinnedSMTP, user_email._PinnedSMTP_SSL.__mro__)
        self.assertLess(user_email._PinnedSMTP_SSL.__mro__.index(__import__("smtplib").SMTP_SSL),
                        user_email._PinnedSMTP_SSL.__mro__.index(user_email._PinnedSMTP))


class EmailSettingsRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.module = importlib.import_module("control_gate_api")

    def test_the_stored_password_is_never_returned_to_the_browser(self):
        client = signed_in_client(self.module.app)
        with patch.object(user_email, "_load_raw", return_value=row_with_password("super-secret-pw")):
            with patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}):
                response = client.get("/api/email-settings")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("super-secret-pw", body)
        self.assertNotIn("smtp_password", body)
        self.assertTrue(response.json["settings"]["configured"])

    def test_email_routes_require_a_signed_in_account(self):
        client = self.module.app.test_client()
        self.assertEqual(client.get("/api/email-settings").status_code, 401)
        self.assertEqual(client.post("/api/email-settings", json={}).status_code, 401)
        self.assertEqual(client.post("/api/email-settings/test").status_code, 401)

    def test_invalid_settings_are_rejected_without_a_server_error(self):
        client = signed_in_client(self.module.app)
        with patch.object(user_email, "_load_raw", return_value=None):
            response = client.post("/api/email-settings", json={"smtp_host": ""})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["status"], "error")

    def test_testing_an_unconfigured_mailbox_reports_a_conflict(self):
        client = signed_in_client(self.module.app)
        with patch.object(user_email, "_load_raw", return_value=None):
            response = client.post("/api/email-settings/test")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Save your mail settings", response.json["message"])

    def test_dispatch_refuses_with_a_actionable_code_when_mail_is_not_set_up(self):
        client = signed_in_client(self.module.app)
        with patch.object(user_email, "_load_raw", return_value=None):
            response = client.post("/api/dispatch/42", json={"final_pitch": "Hello"})
        self.assertEqual(response.status_code, 409)
        # The dashboard keys off this code to offer the Settings page.
        self.assertEqual(response.json["code"], "email_not_configured")

    def dispatch(self, payload, lead=None):
        """Runs /api/dispatch against a fake prospects table; returns
        (response, send_email mock, executed SQL)."""
        lead = lead or {"business_name": "Acme <Co>", "status": "pending", "subject_line": "Hi"}
        executed = []
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.execute.side_effect = lambda sql, params=None: executed.append((sql, params))
        cursor.fetchone.return_value = lead
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cursor
        client = signed_in_client(self.module.app)
        with (
            patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
            patch.object(user_email, "_load_raw", return_value=row_with_password("pw")),
            patch.object(user_email, "send_email") as send,
            patch.object(self.module.psycopg2, "connect", return_value=conn),
            patch("followup_scheduler.schedule_followup"),
        ):
            response = client.post("/api/dispatch/42", json=payload)
        return response, send, executed

    def test_dispatch_never_falls_back_to_the_sender_or_overwrites_the_prospect(self):
        for destination in [None, "", "   ", "qa@", "a,b@example.test"]:
            with self.subTest(destination=destination):
                payload = {"final_pitch": "Hello"}
                if destination is not None:
                    payload["destination_email"] = destination
                response, send, executed = self.dispatch(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["code"], "invalid_recipient")
                send.assert_not_called()
                self.assertFalse([sql for sql, _ in executed if sql.lstrip().upper().startswith("UPDATE")])

    def test_dispatch_escapes_the_pitch_and_saves_the_recipient(self):
        response, send, executed = self.dispatch({
            "final_pitch": "Hi <b>team</b> & co\nSee https://x.test?a=1&b=2",
            "destination_email": " lead@example.test ",
        })
        self.assertEqual(response.status_code, 200, response.json)
        recipient, subject, body = send.call_args.args[2:5]
        self.assertEqual(recipient, "lead@example.test")
        self.assertIn("Hi &lt;b&gt;team&lt;/b&gt; &amp; co<br>See https://x.test?a=1&amp;b=2", body)
        self.assertNotIn("<b>team</b>", body)
        update = next(params for sql, params in executed if sql.lstrip().upper().startswith("UPDATE"))
        self.assertEqual(update[1], "lead@example.test")


if __name__ == "__main__":
    unittest.main()
