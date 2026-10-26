import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER_ID

import serpapi_client
from discovery_runs import DiscoveryPartialSuccess


def ok_response(payload=None):
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload if payload is not None else {}
    response.raise_for_status.return_value = None
    return response


class RetryTests(unittest.TestCase):
    def test_a_read_timeout_is_retried_and_can_succeed(self):
        good = ok_response({"local_results": []})
        with (
            patch("requests.get", side_effect=[requests.Timeout("read timed out"), good]) as get,
            patch("serpapi_client.time.sleep") as sleep,
        ):
            result = serpapi_client.fetch({"q": "Law Firm"}, label="Google Maps search")
        self.assertIs(result, good)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()

    def test_connection_errors_and_gateway_faults_are_retried(self):
        throttled = Mock(status_code=429)
        for failure in [requests.ConnectionError("reset"), throttled]:
            with self.subTest(failure=failure):
                good = ok_response()
                side_effect = ([failure, good] if isinstance(failure, Exception)
                               else [failure, good])
                with (
                    patch("requests.get", side_effect=side_effect) as get,
                    patch("serpapi_client.time.sleep"),
                ):
                    self.assertIs(serpapi_client.fetch({"q": "x"}), good)
                self.assertEqual(get.call_count, 2)

    def test_giving_up_reports_a_readable_cause_not_a_connection_pool_dump(self):
        with (
            patch("requests.get", side_effect=requests.Timeout(
                "HTTPSConnectionPool(host='serpapi.com', port=443): Read timed out. (read timeout=10)")),
            patch("serpapi_client.time.sleep"),
        ):
            with self.assertRaises(serpapi_client.SerpApiUnavailable) as failure:
                serpapi_client.fetch({"q": "Law Firm"}, label="google / Law Firm")
        message = str(failure.exception)
        self.assertIn("google / Law Firm", message)
        self.assertIn("timed out", message)
        self.assertNotIn("HTTPSConnectionPool", message)
        self.assertIn("3 attempts", message)

    def test_client_errors_are_not_retried(self):
        """A bad key or malformed query will not fix itself; retrying burns quota."""
        denied = Mock(status_code=401)
        denied.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
        with patch("requests.get", return_value=denied) as get:
            with self.assertRaises(requests.HTTPError):
                serpapi_client.fetch({"q": "x"})
        self.assertEqual(get.call_count, 1)

    def test_backoff_grows_between_attempts_and_retries_are_logged(self):
        log = Mock()
        with (
            patch("requests.get", side_effect=requests.Timeout("slow")),
            patch("serpapi_client.time.sleep") as sleep,
        ):
            with self.assertRaises(serpapi_client.SerpApiUnavailable):
                serpapi_client.fetch({"q": "x"}, attempts=3, log=log, label="QA search")
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertLess(delays[0], delays[1])
        self.assertEqual(log.call_count, 2)
        self.assertIn("timed out", log.call_args.args[0])


class EnrichmentResilienceTests(unittest.TestCase):
    """One unreachable lookup must not discard a whole industry's results."""

    def _run(self, failing_helper):
        import discovery_engine
        api = types.ModuleType("control_gate_api")
        api.emit_ui_log = Mock()
        maps = ok_response({"local_results": [
            {"title": "QA Law Firm", "address": "Austin", "data_id": "qa-1",
             "rating": 4.5, "reviews": 25, "type": "Law Firm"},
        ]})
        with (
            patch.dict(sys.modules, {"control_gate_api": api}),
            patch.object(discovery_engine, "SERPAPI_KEY", "qa-only"),
            patch.object(discovery_engine, "serpapi_fetch", return_value=maps),
            patch.object(discovery_engine, "matching_industry_category", return_value="Law Firm"),
            patch.object(discovery_engine, "save_leads_to_db") as save,
            patch.object(discovery_engine, failing_helper,
                         side_effect=serpapi_client.SerpApiUnavailable("the search provider timed out")),
            patch.object(discovery_engine, "attempt_domain_email_crawl", return_value=None),
        ):
            with self.assertRaises(DiscoveryPartialSuccess) as partial:
                discovery_engine.run_discovery("Austin", "Law Firm", user_id=QA_USER_ID)
        return save, partial.exception

    def test_a_timed_out_website_lookup_still_saves_the_business(self):
        save, partial = self._run("_search_for_business_website")
        saved = save.call_args.args[0]
        self.assertEqual([lead["name"] for lead in saved], ["QA Law Firm"])
        self.assertIsNone(saved[0]["website_url"])
        self.assertEqual(partial.saved, 1)
        self.assertIn("website lookup", partial.messages[0])
        self.assertIn("timed out", partial.messages[0])

    def test_a_failed_screenshot_does_not_lose_the_lead(self):
        import discovery_engine
        api = types.ModuleType("control_gate_api")
        api.emit_ui_log = Mock()
        maps = ok_response({"local_results": [
            {"title": "QA Law Firm", "address": "Austin", "data_id": "qa-2",
             "website": "https://qa.test", "rating": 4.5, "reviews": 25, "type": "Law Firm"},
        ]})
        with (
            patch.dict(sys.modules, {"control_gate_api": api}),
            patch.object(discovery_engine, "SERPAPI_KEY", "qa-only"),
            patch.object(discovery_engine, "serpapi_fetch", return_value=maps),
            patch.object(discovery_engine, "matching_industry_category", return_value="Law Firm"),
            patch.object(discovery_engine, "save_leads_to_db") as save,
            patch.object(discovery_engine, "attempt_domain_email_crawl", return_value="qa@qa.test"),
            patch.object(discovery_engine, "capture_business_site_snapshot",
                         side_effect=RuntimeError("browser crashed")),
        ):
            with self.assertRaises(DiscoveryPartialSuccess) as partial:
                discovery_engine.run_discovery("Austin", "Law Firm", user_id=QA_USER_ID)
        saved = save.call_args.args[0]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["email"], "qa@qa.test")
        self.assertIsNone(saved[0]["screenshot_path"])
        self.assertIn("screenshot", partial.exception.messages[0])

    def test_a_clean_run_returns_a_plain_count(self):
        import discovery_engine
        api = types.ModuleType("control_gate_api")
        api.emit_ui_log = Mock()
        maps = ok_response({"local_results": [
            {"title": "QA Law Firm", "address": "Austin", "data_id": "qa-3",
             "website": "https://qa.test", "rating": 4.5, "reviews": 25, "type": "Law Firm"},
        ]})
        with (
            patch.dict(sys.modules, {"control_gate_api": api}),
            patch.object(discovery_engine, "SERPAPI_KEY", "qa-only"),
            patch.object(discovery_engine, "serpapi_fetch", return_value=maps),
            patch.object(discovery_engine, "matching_industry_category", return_value="Law Firm"),
            patch.object(discovery_engine, "save_leads_to_db"),
            patch.object(discovery_engine, "attempt_domain_email_crawl", return_value=None),
        ):
            self.assertEqual(discovery_engine.run_discovery("Austin", "Law Firm", user_id=QA_USER_ID), 1)


class MixedOutcomeTests(unittest.TestCase):
    def test_results_are_kept_when_one_source_fails_but_another_saves(self):
        from discovery_runs import LocalDiscoveryRuns
        from test_discovery_runs import ImmediateThread
        runs = LocalDiscoveryRuns()
        tasks = [
            ("google", "Law Firm", Mock(side_effect=serpapi_client.SerpApiUnavailable(
                "google / Law Firm: the search provider timed out after 3 attempts."))),
            ("yelp", "Law Firm", Mock(return_value=18)),
        ]
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            run_id = runs.start(tasks, runs.record_log, owner=1)
        run = runs.snapshot(run_id, owner=1)
        # 18 leads were saved, so the operator must get their results.
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(run["saved"], 18)
        self.assertEqual(run["errors"], [])
        self.assertTrue(any("timed out" in warning for warning in run["warnings"]))

    def test_a_run_that_saved_nothing_still_fails(self):
        from discovery_runs import LocalDiscoveryRuns
        from test_discovery_runs import ImmediateThread
        runs = LocalDiscoveryRuns()
        tasks = [("google", "Law Firm", Mock(side_effect=RuntimeError("provider down"))),
                 ("yelp", "Law Firm", Mock(return_value=0))]
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            run_id = runs.start(tasks, runs.record_log, owner=1)
        run = runs.snapshot(run_id, owner=1)
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["saved"], 0)
        self.assertIn("provider down", run["errors"][0])


class ScrapingdogRoutingTests(unittest.TestCase):
    """Verifies that generic search requests route to Scrapingdog endpoints correctly."""

    def test_google_search_routing_and_param_mapping(self):
        import scrapingdog_client
        url, params = scrapingdog_client.resolve_endpoint_and_params({
            "engine": "google",
            "q": 'site:facebook.com "Dentist" "Austin"',
            "api_key": "qa-key",
            "num": 10,
        })
        self.assertEqual(url, scrapingdog_client.SCRAPINGDOG_GOOGLE_ENDPOINT)
        self.assertEqual(params["query"], 'site:facebook.com "Dentist" "Austin"')
        self.assertEqual(params["api_key"], "qa-key")

    def test_google_maps_routing_and_param_mapping(self):
        import scrapingdog_client
        url, params = scrapingdog_client.resolve_endpoint_and_params({
            "engine": "google_maps",
            "q": "Roofing Contractor Austin",
            "ll": "@30.2672,-97.7431,20000m",
            "api_key": "qa-key",
        })
        self.assertEqual(url, scrapingdog_client.SCRAPINGDOG_MAPS_ENDPOINT)
        self.assertEqual(params["query"], "Roofing Contractor Austin")
        self.assertEqual(params["ll"], "@30.2672,-97.7431,20000m")
        self.assertEqual(params["api_key"], "qa-key")

    def test_google_jobs_routing_and_param_mapping(self):
        import scrapingdog_client
        url, params = scrapingdog_client.resolve_endpoint_and_params({
            "engine": "google_jobs",
            "q": "Video Editor",
            "gl": "ca",
            "location": "Canada",
            "api_key": "qa-key",
        })
        self.assertEqual(url, scrapingdog_client.SCRAPINGDOG_JOBS_ENDPOINT)
        self.assertEqual(params["query"], "Video Editor")
        self.assertEqual(params["country"], "ca")
        self.assertEqual(params["api_key"], "qa-key")

    def test_yelp_routing_and_param_mapping(self):
        import scrapingdog_client
        url, params = scrapingdog_client.resolve_endpoint_and_params({
            "engine": "yelp",
            "find_desc": "Coffee",
            "find_loc": "Seattle",
            "api_key": "qa-key",
        })
        self.assertEqual(url, scrapingdog_client.SCRAPINGDOG_YELP_ENDPOINT)
        self.assertEqual(params["find_desc"], "Coffee")
        self.assertEqual(params["find_loc"], "Seattle")
        self.assertEqual(params["api_key"], "qa-key")

    def test_explicit_serpapi_endpoint_preservation(self):
        import scrapingdog_client
        url, params = scrapingdog_client.resolve_endpoint_and_params(
            {"engine": "google", "q": "test", "api_key": "qa-key"},
            endpoint="https://serpapi.com/search",
        )
        self.assertEqual(url, "https://serpapi.com/search")
        self.assertEqual(params["q"], "test")


if __name__ == "__main__":
    unittest.main()

