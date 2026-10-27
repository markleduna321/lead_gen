import contextlib
import importlib
import io
import logging
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import auth
import log_redact
from log_redact import redact


class RedactTests(unittest.TestCase):
    def test_addresses_keep_first_character_and_domain(self):
        cases = {
            "Contact Extracted: owner@shop.com": "Contact Extracted: o***@shop.com",
            "a@b.co and Jane.Doe+leads@mail.example.ph": "a***@b.co and J***@mail.example.ph",
            'DETAIL:  Key (email_key)=(qa@example.test) already exists.':
                'DETAIL:  Key (email_key)=(q***@example.test) already exists.',
            "<owner@shop.com>": "<o***@shop.com>",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(redact(raw), expected)

    def test_text_without_addresses_is_unchanged(self):
        for text in ["no email here", "@handle on instagram", "https://x.test/@user",
                     "user@localhost", "price 5@10", "", None, 42]:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_streams_are_masked(self):
        target = io.StringIO()
        stream = log_redact.RedactingStream(target)
        print("Found owner@shop.com", file=stream)
        stream.writelines(["x sales@acme.test\n"])
        self.assertEqual(target.getvalue(), "Found o***@shop.com\nx s***@acme.test\n")
        self.assertIs(stream.getvalue.__self__, target, "other attributes pass through")

    def test_log_records_and_tracebacks_are_masked(self):
        target = io.StringIO()
        handler = logging.StreamHandler(target)
        handler.addFilter(log_redact.FILTER)
        logger = logging.getLogger("qa.redact")
        logger.addHandler(handler)
        logger.propagate = False
        self.addCleanup(logger.removeHandler, handler)
        logger.warning("sent to %s", "owner@shop.com")
        try:
            raise ValueError("Key (email_key)=(qa@example.test) already exists")
        except ValueError:
            logger.exception("registration failed for %s", "qa@example.test")
        output = target.getvalue()
        self.assertNotIn("owner@shop.com", output)
        self.assertNotIn("qa@example.test", output)
        self.assertIn("sent to o***@shop.com", output)
        self.assertIn("(q***@example.test) already exists", output)

    def test_malformed_records_do_not_raise_from_the_filter(self):
        record = logging.LogRecord("qa", logging.INFO, __file__, 1, "%d", ("not-a-number",), None)
        self.assertTrue(log_redact.FILTER.filter(record))

    def test_install_is_idempotent(self):
        original_out, original_err = sys.stdout, sys.stderr
        self.addCleanup(setattr, sys, "stdout", original_out)
        self.addCleanup(setattr, sys, "stderr", original_err)
        handler = logging.StreamHandler(io.StringIO())
        logging.getLogger("qa.install").addHandler(handler)
        self.addCleanup(logging.getLogger("qa.install").removeHandler, handler)
        log_redact.install()
        wrapped = sys.stdout
        log_redact.install()
        self.assertIsInstance(sys.stdout, log_redact.RedactingStream)
        self.assertIs(sys.stdout, wrapped)
        self.assertNotIsInstance(sys.stdout._stream, log_redact.RedactingStream)
        self.assertEqual(handler.filters.count(log_redact.FILTER), 1)


class AppLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.api = importlib.import_module("control_gate_api")

    def test_live_log_keeps_the_address_but_the_server_copy_is_masked(self):
        captured = io.StringIO()
        with patch.object(sys, "stdout", log_redact.RedactingStream(captured)):
            self.api.bind_log_user(4242)
            try:
                self.api.emit_ui_log("Contact Extracted: owner@shop.com")
            finally:
                self.api.bind_log_user(None)
        self.assertIn("Contact Extracted: o***@shop.com", captured.getvalue())
        self.assertNotIn("owner@shop.com", captured.getvalue())
        self.assertIn("Contact Extracted: owner@shop.com", self.api.user_log_buffer(4242))

    def test_importing_the_app_masks_process_streams(self):
        import subprocess
        script = (
            "import sys, logging\n"
            "from unittest.mock import patch\n"
            "sys.path.insert(0, 'src')\n"
            "with patch('dotenv.load_dotenv'), patch('psycopg2.connect', side_effect=RuntimeError('no db')):\n"
            "    import control_gate_api as api\n"
            "print('stdout owner@shop.com')\n"
            "print('stderr owner@shop.com', file=sys.stderr)\n"
            "api.app.logger.error('logger owner@shop.com')\n"
        )
        base = {k: v for k, v in os.environ.items()
                if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONIOENCODING"}}
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env={**base, "APP_ENV": "local"}, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertNotIn("owner@shop.com", output)
        for line in ["stdout o***@shop.com", "stderr o***@shop.com", "logger o***@shop.com"]:
            self.assertIn(line, output)


class DuplicateRegistrationTests(unittest.TestCase):
    def test_duplicates_are_refused_without_a_database_error(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        with patch("psycopg2.connect", return_value=database), patch("billing.lock_wallet") as wallet:
            with self.assertRaises(auth.AuthError) as refused:
                auth.create_user("qa-only", "qa@example.test", "correct-horse-battery")
        self.assertEqual(str(refused.exception), auth.REGISTRATION_CONFLICT_ERROR)
        self.assertNotIn("exists", str(refused.exception))
        sql = cursor.execute.call_args.args[0]
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        wallet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
