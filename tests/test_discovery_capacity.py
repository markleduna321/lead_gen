import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER_ID

from discovery_runs import (
    DEFAULT_WORKER_LIMIT, DiscoveryBusyError, DiscoveryPartialSuccess,
    LocalDiscoveryRuns, MAX_WORKER_LIMIT, reset_worker_slots, worker_slots,
)
from test_discovery_runs import ImmediateThread


class WorkerLimitTests(unittest.TestCase):
    def tearDown(self):
        reset_worker_slots()

    def test_the_limit_comes_from_configuration_within_safe_bounds(self):
        for raw, expected in [("2", 2), ("", DEFAULT_WORKER_LIMIT), ("nonsense", DEFAULT_WORKER_LIMIT),
                              ("0", 1), ("-5", 1), ("9999", MAX_WORKER_LIMIT)]:
            with self.subTest(raw=raw):
                reset_worker_slots()
                with patch.dict("os.environ", {"MAX_DISCOVERY_WORKERS": raw}):
                    self.assertEqual(worker_slots().limit, expected)

    def test_concurrent_searches_are_capped_across_accounts(self):
        reset_worker_slots(2)
        runs = LocalDiscoveryRuns()
        release, started = threading.Event(), []

        def work():
            started.append(True)
            release.wait(timeout=5)
        try:
            # Two different accounts fill the pool.
            runs.start([("google", "Cafe", work)], runs.record_log, owner=1)
            runs.start([("google", "Cafe", work)], runs.record_log, owner=2)
            while len(started) < 2:
                pass
            # A third account must be refused rather than spawning a new thread.
            with self.assertRaisesRegex(DiscoveryBusyError, "search workers are busy"):
                runs.start([("google", "Cafe", Mock())], runs.record_log, owner=3)
        finally:
            release.set()

    def test_a_finished_search_returns_its_slot_to_the_pool(self):
        reset_worker_slots(1)
        runs = LocalDiscoveryRuns()
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            runs.start([("google", "Cafe", Mock())], runs.record_log, owner=1)
            # The first run completed, so the single slot must be reusable.
            second = runs.start([("google", "Cafe", Mock())], runs.record_log, owner=2)
        self.assertEqual(runs.snapshot(second, owner=2)["state"], "succeeded")

    def test_a_failing_search_also_returns_its_slot(self):
        reset_worker_slots(1)
        runs = LocalDiscoveryRuns()
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            runs.start([("google", "Cafe", Mock(side_effect=RuntimeError("boom")))],
                       runs.record_log, owner=1)
            self.assertIsNotNone(runs.start([("google", "Cafe", Mock())], runs.record_log, owner=2))

    def test_a_rejected_duplicate_search_does_not_leak_a_slot(self):
        reset_worker_slots(2)
        runs = LocalDiscoveryRuns()
        release, started = threading.Event(), threading.Event()

        def work():
            started.set()
            release.wait(timeout=5)
        try:
            runs.start([("google", "Cafe", work)], runs.record_log, owner=1)
            self.assertTrue(started.wait(timeout=2))
            # The owner-specific message must win over the capacity message.
            with self.assertRaisesRegex(DiscoveryBusyError, "already running"):
                runs.start([("google", "Cafe", Mock())], runs.record_log, owner=1)
            # The refused attempt must not have consumed the second slot.
            with patch("discovery_runs.threading.Thread", ImmediateThread):
                other = runs.start([("google", "Cafe", Mock())], runs.record_log, owner=2)
            self.assertEqual(runs.snapshot(other, owner=2)["state"], "succeeded")
        finally:
            release.set()

    def test_a_worker_that_cannot_start_releases_its_slot(self):
        reset_worker_slots(1)
        runs = LocalDiscoveryRuns()
        with patch("discovery_runs.threading.Thread", side_effect=RuntimeError("No thread")):
            with self.assertRaises(RuntimeError):
                runs.start([("google", "Cafe", Mock())], runs.record_log, owner=1)
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            recovered = runs.start([("google", "Cafe", Mock())], runs.record_log, owner=2)
        self.assertEqual(runs.snapshot(recovered, owner=2)["state"], "succeeded")


class PartialSuccessTests(unittest.TestCase):
    def tearDown(self):
        reset_worker_slots()

    def test_partial_work_is_a_warning_not_a_failure(self):
        runs = LocalDiscoveryRuns()
        partial = Mock(side_effect=DiscoveryPartialSuccess(["contact lookup failed"], saved=7))
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            run_id = runs.start([("google_jobs", "video_editing", partial)], runs.record_log, owner=1)
        run = runs.snapshot(run_id, owner=1)
        # Leads were saved, so the operator must not be told the search failed.
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(run["errors"], [])
        self.assertEqual(run["saved"], 7)
        self.assertIn("contact lookup failed", run["warnings"][0])

    def test_a_real_failure_still_fails_the_run(self):
        runs = LocalDiscoveryRuns()
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            run_id = runs.start([("google_jobs", "video_editing",
                                  Mock(side_effect=RuntimeError("QA database save failed")))],
                                runs.record_log, owner=1)
        run = runs.snapshot(run_id, owner=1)
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["warnings"], [])
        self.assertEqual(run["saved"], 0)

    def test_saved_counts_accumulate_and_ignore_non_counts(self):
        runs = LocalDiscoveryRuns()
        tasks = [
            ("google", "Cafe", Mock(return_value=3)),
            ("yelp", "Cafe", Mock(return_value=None)),
            ("google", "Dentist", Mock(return_value=True)),  # bool is not a count
            ("google_jobs", "video_editing", Mock(side_effect=DiscoveryPartialSuccess(["x"], saved=2))),
        ]
        with patch("discovery_runs.threading.Thread", ImmediateThread):
            run_id = runs.start(tasks, runs.record_log, owner=1)
        run = runs.snapshot(run_id, owner=1)
        self.assertEqual(run["saved"], 5)
        self.assertEqual(run["completed"], 4)
        self.assertEqual(run["state"], "succeeded")


class HiringPartialSuccessTests(unittest.TestCase):
    def test_saved_postings_raise_partial_success_carrying_the_count(self):
        import types
        import discovery_jobs
        leads = [{"company_name": "QA", "job_title": "Video Editor", "location": "Canada",
                  "lead_type": "video_editing", "job_identity": "qa-1"}]
        api = types.ModuleType("control_gate_api")
        api.emit_ui_log = Mock()
        with (
            patch.dict(sys.modules, {"control_gate_api": api}),
            patch.object(discovery_jobs, "SERPAPI_KEY", "qa-only"),
            patch.object(discovery_jobs, "save_job_leads"),
            patch.object(discovery_jobs, "deduplicate_job_leads", return_value=leads),
            patch.object(discovery_jobs, "enrich_company_contact",
                         side_effect=RuntimeError("contact lookup failed")),
            patch("requests.get") as get,
        ):
            get.return_value = Mock(json=Mock(return_value={"jobs_results": [
                {"company_name": "QA", "title": "Video Editor", "location": "Canada"}]}))
            with self.assertRaises(DiscoveryPartialSuccess) as partial:
                discovery_jobs.run_job_discovery("video_editing", "Canada", user_id=QA_USER_ID)
        self.assertEqual(partial.exception.saved, len(leads))
        self.assertTrue(partial.exception.messages)


if __name__ == "__main__":
    unittest.main()
