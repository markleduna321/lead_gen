import threading
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

from test_discovery_filters import load_function
from discovery_runs import DiscoveryBusyError, LocalDiscoveryRuns


class ImmediateThread:
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class DiscoveryRunTests(unittest.TestCase):
    def test_empty_search_success_and_partial_failure_are_distinct(self):
        runs = LocalDiscoveryRuns()
        completed = Mock()
        failed = Mock(side_effect=RuntimeError("Provider unavailable"))
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            successful_id = runs.start([("google", "Dentist", lambda: None)], runs.record_log)
            failed_id = runs.start([
                ("google", "Dentist", failed),
                ("yelp", "Cafe", completed),
            ], runs.record_log)
        success = runs.snapshot(successful_id)
        self.assertEqual(success["state"], "succeeded")
        self.assertEqual(success["completed"], 1)
        failure = runs.snapshot(failed_id)
        self.assertEqual(failure["state"], "failed")
        self.assertEqual(failure["completed"], 2)
        self.assertIn("google / Dentist: Provider unavailable", failure["errors"])
        self.assertIn("Provider unavailable", failure["logs"][0])
        completed.assert_called_once()
        self.assertEqual(runs.snapshot(successful_id)["logs"], [])
        self.assertIsNone(runs.snapshot("missing"))

    def test_overlapping_runs_are_rejected_and_running_worker_is_not_canceled(self):
        runs = LocalDiscoveryRuns()
        release = threading.Event()
        started = threading.Event()
        finished = threading.Event()
        def work():
            started.set()
            release.wait(timeout=5)
            finished.set()
        try:
            run_id = runs.start([("google", "Cafe", work)], runs.record_log)
            self.assertTrue(started.wait(timeout=2))
            with self.assertRaises(DiscoveryBusyError):
                runs.start([("yelp", "Cafe", Mock())], runs.record_log)
        finally:
            release.set()
            self.assertTrue(finished.wait(timeout=2))
        # Snapshot is immutable to callers.
        state = runs.snapshot(run_id)
        state["logs"].append("not an internal log")
        self.assertNotIn("not an internal log", runs.snapshot(run_id)["logs"])

    def test_thread_start_failure_leaves_a_failed_record_not_a_permanent_lock(self):
        runs = LocalDiscoveryRuns()
        with patch("discovery_runs.threading.Thread", side_effect=RuntimeError("No thread")):
            with self.assertRaises(RuntimeError):
                runs.start([("google", "Cafe", Mock())], runs.record_log)
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            run_id = runs.start([("google", "Cafe", Mock())], runs.record_log)
        self.assertEqual(runs.snapshot(run_id)["state"], "succeeded")

    def test_completed_run_and_log_retention_is_bounded(self):
        runs = LocalDiscoveryRuns()
        def work():
            for index in range(60):
                runs.record_log(str(index))
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            old_id = runs.start([("google", "Cafe", work)], runs.record_log)
            self.assertEqual(len(runs.snapshot(old_id)["logs"]), 50)
            for _ in range(20):
                runs.start([("google", "Cafe", Mock())], runs.record_log)
        self.assertIsNone(runs.snapshot(old_id))

    def test_source_provider_errors_and_missing_keys_propagate(self):
        for filename, name in [
            ("discovery_engine.py", "run_discovery"),
            ("discovery_yelp.py", "run_yelp_discovery"),
            ("discovery_facebook.py", "run_facebook_discovery"),
            ("discovery_instagram.py", "run_instagram_discovery"),
        ]:
            with self.subTest(worker=name):
                response = Mock()
                response.json.return_value = {"error": "Provider quota exceeded"}
                namespace = {
                    "SERPAPI_KEY": "qa-only",
                    "requests": types.SimpleNamespace(get=Mock(return_value=response)),
                }
                api = types.ModuleType("control_gate_api")
                api.emit_ui_log = Mock()
                hunter, screenshot = types.ModuleType("email_hunter"), types.ModuleType("screenshot_agent")
                hunter.attempt_domain_email_crawl = Mock()
                screenshot.capture_business_site_snapshot = Mock()
                worker = load_function(filename, name, namespace)
                with patch.dict("sys.modules", {"control_gate_api": api, "email_hunter": hunter, "screenshot_agent": screenshot}):
                    with self.assertRaisesRegex(RuntimeError, "quota exceeded"):
                        worker("Austin", "Dental Clinic")
                    namespace["SERPAPI_KEY"] = None
                    with self.assertRaisesRegex(RuntimeError, "requires SERPAPI_API_KEY"):
                        worker("Austin", "Dental Clinic")

    def test_all_source_save_errors_propagate(self):
        for filename, name in [
            ("discovery_engine.py", "save_leads_to_db"),
            ("discovery_yelp.py", "save_yelp_leads"),
            ("discovery_facebook.py", "save_facebook_leads"),
            ("discovery_instagram.py", "save_instagram_leads"),
        ]:
            with self.subTest(save=name):
                namespace = {
                    "DB_DSN": "qa-only", "psycopg2": MagicMock(),
                    "execute_values": Mock(side_effect=RuntimeError("QA insert failed")),
                }
                save = load_function(filename, name, namespace)
                api = types.ModuleType("control_gate_api")
                api.emit_ui_log = Mock()
                row = {"name": "QA", "category": "Dentist", "phone": None, "address": None,
                       "place_id": "qa", "reviews": 1, "rating": 5}
                with patch.dict("sys.modules", {"control_gate_api": api}):
                    with self.assertRaisesRegex(RuntimeError, "QA insert failed"):
                        save([row])

    def test_google_fallback_provider_errors_are_not_silent_no_email_results(self):
        api = types.ModuleType("control_gate_api")
        api.emit_ui_log = Mock()
        response = Mock()
        response.json.return_value = {"error": "QA provider unavailable"}
        for name in ["_find_facebook_email", "_search_for_business_website"]:
            namespace = {
                "SERPAPI_KEY": "qa-only",
                "requests": types.SimpleNamespace(get=Mock(return_value=response)),
            }
            helper = load_function("discovery_engine.py", name, namespace)
            with self.subTest(helper=name), patch.dict("sys.modules", {"control_gate_api": api}):
                with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                    helper("QA", "Austin")
