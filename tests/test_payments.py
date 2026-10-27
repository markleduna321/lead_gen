import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import payments
from plans import PlanError


class PayMongoUnitTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "PAYMONGO_SECRET_KEY": "sk_test_offline", "PAYMONGO_WEBHOOK_SECRET": "offline-hook",
            "APP_BASE_URL": "https://app.example.test",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def signature(self, raw, timestamp=None, live=False):
        timestamp = str(timestamp if timestamp is not None else int(time.time()))
        digest = hmac.new(b"offline-hook", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return f"t={timestamp},te={'' if live else digest},li={digest if live else ''}"

    def test_signature_binds_raw_body_mode_and_fresh_timestamp(self):
        raw = json.dumps({"data": {"attributes": {
            "livemode": False, "type": "checkout_session.payment.paid",
        }}}).encode()
        self.assertEqual(payments.verify_webhook(raw, self.signature(raw))["livemode"], False)
        for body, signature in [
            (raw + b" ", self.signature(raw)),
            (raw, self.signature(raw, int(time.time()) - 301)),
            (raw, self.signature(raw, int(time.time()) + 301)),
            (raw, self.signature(raw, live=True)),
            (raw, ""), (b"{", self.signature(b"{")),
        ]:
            with self.subTest(signature=signature), self.assertRaises(PlanError):
                payments.verify_webhook(body, signature)
        with patch.dict(os.environ, {"PAYMONGO_SECRET_KEY": "sk_live_offline"}):
            with self.assertRaises(PlanError):
                payments.verify_webhook(raw, self.signature(raw, live=True))

    def test_live_signature_uses_live_key_without_accepting_test_signature(self):
        raw = b'{"data":{"attributes":{"livemode":true,"type":"checkout_session.payment.paid"}}}'
        with patch.dict(os.environ, {"PAYMONGO_SECRET_KEY": "sk_live_offline"}):
            self.assertTrue(payments.verify_webhook(raw, self.signature(raw, live=True))["livemode"])
            with self.assertRaises(PlanError):
                payments.verify_webhook(raw, self.signature(raw))

    def test_configuration_requires_secret_webhook_and_trusted_https_origin(self):
        for key, value in [("PAYMONGO_SECRET_KEY", ""), ("PAYMONGO_WEBHOOK_SECRET", ""),
                           ("APP_BASE_URL", "http://app.example.test"),
                           ("APP_BASE_URL", "https://user:pass@app.example.test"),
                           ("APP_BASE_URL", "https://app.example.test/path")]:
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                self.assertEqual(payments.checkout_ready(), (False, False))

    def test_gateway_uses_basic_secret_auth_timeouts_and_no_redirects(self):
        response = Mock(status_code=200)
        response.json.return_value = {"data": {"id": "cs_offline", "attributes": {}}}
        with patch("payments.requests.request", return_value=response) as request:
            payments.provider_request("GET", "/v1/checkout_sessions/cs_offline")
        self.assertEqual(request.call_args.args, ("GET", "https://api.paymongo.com/v1/checkout_sessions/cs_offline"))
        self.assertEqual(request.call_args.kwargs["auth"], ("sk_test_offline", ""))
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(request.call_args.kwargs["timeout"], (5, 25))

    def test_gateway_errors_are_explicit_and_never_expose_response_or_credentials(self):
        for response in [Mock(status_code=401), Mock(status_code=503), Mock(status_code=302),
                         Mock(status_code=200, json=Mock(return_value={"bad": "body"}))]:
            with self.subTest(response=response), patch("payments.requests.request", return_value=response):
                with self.assertRaises(PlanError) as raised:
                    payments.provider_request("POST", "/v2/checkout_sessions", {"test": True})
                self.assertNotIn("sk_test", str(raised.exception))
        with patch("payments.requests.request", side_effect=requests.Timeout):
            with self.assertRaises(PlanError) as raised:
                payments.provider_request("POST", "/v2/checkout_sessions", {})
            self.assertEqual(raised.exception.code, "gateway_unavailable")

    def test_checkout_url_and_session_id_are_strictly_scoped(self):
        self.assertTrue(payments.safe_checkout_url("https://checkout.paymongo.com/cs_abc#token"))
        for url in ["https://checkout.paymongo.com.evil.test/", "javascript:alert(1)",
                    "https://user@checkout.paymongo.com/", "http://checkout.paymongo.com/"]:
            self.assertFalse(payments.safe_checkout_url(url))
        self.assertTrue(payments.valid_session_id("cs_valid123"))
        self.assertFalse(payments.valid_session_id("cs_bad/../refund"))

    def test_prices_match_published_peso_totals_in_centavos(self):
        self.assertEqual([(p[1], p[12], p["topup"], p["pack"]) for p in payments.PRICES.values()], [
            (99900, 999000, 49900, 350), (249900, 2499000, 79900, 750),
            (599900, 5999000, 149900, 2000),
        ])


if __name__ == "__main__":
    unittest.main()
