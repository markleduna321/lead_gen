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
            (dict(base, smtp_port=0), "between 1 and 65535"),
            (dict(base, smtp_port="many"), "between 1 and 65535"),
            (dict(base, smtp_port=True), "between 1 and 65535"),
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
            patch("smtplib.SMTP", return_value=server) as smtp,
        ):
            server.__enter__.return_value = server
            user_email.send_email("qa-only", QA_USER_ID, "lead@example.test",
                                  "Subject here", "<p>body</p>")
        smtp.assert_called_once_with("smtp.example.test", 587, timeout=user_email.CONNECT_TIMEOUT)
        # App passwords are routinely pasted with spaces or dashes.
        server.login.assert_called_once_with("qa@example.test", "apppassword")
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
            patch("smtplib.SMTP_SSL", return_value=server) as smtp_ssl,
            patch("smtplib.SMTP", side_effect=AssertionError("must not use plain SMTP")),
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
            patch("smtplib.SMTP", side_effect=smtplib.SMTPAuthenticationError(535, b"nope")),
        ):
            with self.assertRaisesRegex(user_email.EmailSettingsError, "app password"):
                user_email.send_email("qa-only", QA_USER_ID, "lead@example.test", "s", "b")
        with (
            patch.dict(os.environ, {"SMTP_ENCRYPTION_KEY": QA_KEY}),
            patch.object(user_email, "_load_raw", return_value=row_with_password("pw")),
            patch("smtplib.SMTP", side_effect=OSError("host unreachable")),
        ):
            with self.assertRaisesRegex(user_email.EmailSettingsError, "could not be reached"):
                user_email.send_email("qa-only", QA_USER_ID, "lead@example.test", "s", "b")


class EmailSettingsRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {}, clear=True),
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


if __name__ == "__main__":
    unittest.main()
