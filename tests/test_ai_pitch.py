import contextlib
from decimal import Decimal
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import requests

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER_ID, signed_in_client

import ai_pitch
from plans import PlanError

LEAD = {"id": 7, "business_name": "Harbor Dental", "category": "Dentist", "lead_type": "web_design"}
SECRET = "OpenAI says: invalid_api_key sk-proj-abc"


def completion(text):
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": text}}]}
    return response


class PitchRouteTests(unittest.TestCase):
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

    def setUp(self):
        self.client = signed_in_client(self.module.app)
        self.reserve = Mock(return_value=("pitch-1", dict(LEAD)))
        self.finish = Mock(return_value=Decimal("41.00"))
        self.fail = Mock()
        for target, mock in [("billing.reserve_pitch", self.reserve), ("billing.finish_pitch", self.finish),
                             ("billing.fail_pitch", self.fail)]:
            patcher = patch(target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in [patch.object(self.module, "OPENAI_KEY", "sk-test"),
                        patch("psycopg2.connect", side_effect=AssertionError("The route must not open a connection"))]:
            patcher.start()
            self.addCleanup(patcher.stop)
        for stream in [contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO())]:
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)

    def post(self):
        return self.client.post(f"/api/trigger/pitch-single/{LEAD['id']}")

    def test_bulk_pitch_route_is_gone(self):
        rules = {rule.rule for rule in self.module.app.url_map.iter_rules()}
        self.assertNotIn("/api/trigger/pitch-bulk", rules)
        self.assertFalse(hasattr(self.module, "trigger_bulk_pitch_generation"))

    def test_success_charges_once_and_reports_balance(self):
        calls = []

        def openai(*args, **kwargs):
            calls.append(kwargs["json"])
            return completion("Subject: drop me\nHi Harbor team," if len(calls) == 1 else '"Faster bookings"')

        with patch("requests.post", side_effect=openai):
            response = self.post()
        data = response.get_json()
        self.assertEqual(response.status_code, 200, data)
        self.assertEqual(data["balance"], "41")
        self.assertIn("1 credit used \u00b7 41 credits left", data["message"])
        self.reserve.assert_called_once_with(self.module.DB_DSN, QA_USER_ID, LEAD["id"])
        args = self.finish.call_args.args
        self.assertEqual(args[:4], (self.module.DB_DSN, QA_USER_ID, "pitch-1", LEAD["id"]))
        self.assertEqual(args[4:6], ("Hi Harbor team,", "Faster bookings"))
        self.fail.assert_not_called()
        self.assertEqual(len(calls), 2)

    def test_locked_plan_returns_upsell_code_without_calling_openai(self):
        self.reserve.side_effect = PlanError("AI pitch drafts start on the Solo plan.", "pitch_locked", 403)
        with patch("requests.post", side_effect=AssertionError("OpenAI must not be called")):
            response = self.post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "pitch_locked")

    def test_billing_rejections_keep_their_status(self):
        for code, status in [("pitch_busy", 409), ("insufficient_credits", 402), ("lead_not_found", 404)]:
            with self.subTest(code=code):
                self.reserve.side_effect = PlanError("No.", code, status)
                with patch("requests.post", side_effect=AssertionError("OpenAI must not be called")):
                    response = self.post()
                self.assertEqual((response.status_code, response.get_json()["code"]), (status, code))

    def test_openai_failures_refund_and_hide_provider_details(self):
        failures = [Mock(status_code=401, text=SECRET), requests.Timeout(SECRET), completion("   ")]
        for failure in failures:
            with self.subTest(failure=failure):
                self.fail.reset_mock()
                with patch("requests.post", side_effect=[failure]):
                    response = self.post()
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.get_json()["code"], "ai_failed")
                self.assertNotIn("sk-proj", body)
                self.assertNotIn("invalid_api_key", body)
                self.fail.assert_called_once_with(self.module.DB_DSN, QA_USER_ID, "pitch-1")
                self.finish.assert_not_called()

    def test_subject_failure_falls_back_without_refund(self):
        with patch("requests.post", side_effect=[completion("Hi there,"), Mock(status_code=500, text="x")]):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.finish.call_args.args[5], "Quick concept for Harbor Dental")
        self.fail.assert_not_called()

    def test_missing_key_is_reported_without_charging(self):
        with patch.object(self.module, "OPENAI_KEY", None):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "ai_unavailable")
        self.reserve.assert_not_called()


class PromptTests(unittest.TestCase):
    def test_outsourcing_leads_use_service_prompts(self):
        system, user, angle, _, lead_type = ai_pitch.build_prompts(
            {"business_name": "Acme", "category": "Video Editor", "lead_type": "video_editing"})
        self.assertEqual(lead_type, "video_editing")
        self.assertIn("video editing", system)
        self.assertIn("Acme", user)
        self.assertTrue(angle.startswith("[Video Editing]"))

    def test_local_leads_default_to_web_design(self):
        _, _, angle, _, lead_type = ai_pitch.build_prompts(
            {"business_name": "Acme", "category": "Dentist", "lead_type": None})
        self.assertEqual(lead_type, "web_design")
        self.assertTrue(angle.startswith("[Web Design]"))


if __name__ == "__main__":
    unittest.main()
