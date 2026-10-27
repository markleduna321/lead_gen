import contextlib
import importlib
import io
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from support import QA_USER_ID, signed_in_client

from background_jobs import JobGuard, JobUnavailable


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Gate:
    """A job body that blocks until released, so tests control when it ends."""

    def __init__(self, result=None, error=None):
        self.started, self.release, self.done = threading.Event(), threading.Event(), threading.Event()
        self.result, self.error = result, error

    def __call__(self):
        self.started.set()
        try:
            self.release.wait(5)
            if self.error:
                raise self.error
            return self.result
        finally:
            self.done.set()

    def finish(self, guard, user_id, job):
        self.release.set()
        self.done.wait(5)
        for _ in range(500):
            if not guard.status(user_id)[job]["running"]:
                return
            threading.Event().wait(0.01)
        raise AssertionError("job slot was not released")


class JobGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.guard = JobGuard({"score": ("Lead scoring", 0), "sites": ("Website check", 3600)}, clock=self.clock)

    def test_same_job_cannot_run_twice_for_one_user(self):
        gate = Gate()
        self.guard.start(1, "score", gate)
        gate.started.wait(5)
        with self.assertRaises(JobUnavailable) as raised:
            self.guard.start(1, "score", Gate())
        self.assertEqual(raised.exception.code, "job_running")
        self.assertEqual(str(raised.exception), "Lead scoring is already running.")
        other_user, other_job = Gate(), Gate()
        self.guard.start(2, "score", other_user)
        self.guard.start(1, "sites", other_job)
        self.assertTrue(self.guard.status(1)["score"]["running"])
        for user_id, job, body in [(1, "score", gate), (2, "score", other_user), (1, "sites", other_job)]:
            body.finish(self.guard, user_id, job)
        self.guard.start(1, "score", lambda: None)

    def test_slot_is_released_when_the_job_raises(self):
        gate = Gate(error=RuntimeError("boom"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.guard.start(1, "sites", gate)
            gate.finish(self.guard, 1, "sites")
        self.assertEqual(self.guard.status(1)["sites"], {"running": False, "retry_after": 0})

    def test_cooldown_applies_only_after_useful_successful_runs(self):
        empty = Gate(result=0)
        self.guard.start(1, "sites", empty, cooldown_if=bool)
        empty.finish(self.guard, 1, "sites")
        audited = Gate(result=3)
        self.guard.start(1, "sites", audited, cooldown_if=bool)
        audited.finish(self.guard, 1, "sites")
        self.assertEqual(self.guard.status(1)["sites"]["retry_after"], 3600)
        self.clock.now += 3599.5
        with self.assertRaises(JobUnavailable) as raised:
            self.guard.start(1, "sites", Gate())
        self.assertEqual((raised.exception.code, raised.exception.retry_after), ("job_cooldown", 1))
        self.assertEqual(str(raised.exception), "Website check can run again in 1 minute.")
        self.guard.start(2, "sites", lambda: None)
        self.clock.now += 1
        self.guard.start(1, "sites", lambda: None)

    def test_failed_thread_start_releases_the_slot(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                self.guard.start(1, "score", lambda: None)
        self.assertFalse(self.guard.status(1)["score"]["running"])


class MaintenanceRouteTests(unittest.TestCase):
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
        self.clock = Clock()
        guard = JobGuard(self.module.maintenance_jobs.jobs, clock=self.clock)
        patcher = patch.object(self.module, "maintenance_jobs", guard)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.guard = guard
        self.client = signed_in_client(self.module.app)

    def test_second_click_is_rejected_until_the_job_finishes(self):
        gate = Gate()
        with patch("lead_scorer.run_lead_scoring", side_effect=lambda user_id: gate()) as scorer:
            first = self.client.post("/api/trigger/score-leads")
            gate.started.wait(5)
            second = self.client.post("/api/trigger/score-leads")
            status = self.client.get("/api/jobs/status").get_json()
            gate.finish(self.guard, QA_USER_ID, "score-leads")
        self.assertEqual(first.status_code, 202)
        self.assertTrue(first.get_json()["jobs"]["score-leads"]["running"])
        self.assertEqual((second.status_code, second.get_json()["code"]), (409, "job_running"))
        self.assertTrue(status["jobs"]["score-leads"]["running"])
        scorer.assert_called_once_with(QA_USER_ID)
        self.assertFalse(self.client.get("/api/jobs/status").get_json()["jobs"]["score-leads"]["running"])

    def test_website_check_cools_down_for_an_hour_after_scoring_sites(self):
        gate = Gate(result=4)
        with patch("website_checker.run_website_quality_check", side_effect=lambda user_id: gate()):
            self.assertEqual(self.client.post("/api/trigger/check-websites").status_code, 202)
            gate.finish(self.guard, QA_USER_ID, "check-websites")
        response = self.client.post("/api/trigger/check-websites")
        self.assertEqual((response.status_code, response.get_json()["code"]), (429, "job_cooldown"))
        self.assertEqual(response.headers["Retry-After"], "3600")
        self.assertIn("60 minutes", response.get_json()["message"])

    def test_follow_up_sweep_is_guarded(self):
        gate = Gate()
        with patch("followup_scheduler.run_followup_sweep", side_effect=lambda user_id: gate()):
            self.assertEqual(self.client.post("/api/trigger/followup-sweep").status_code, 202)
            gate.started.wait(5)
            self.assertEqual(self.client.post("/api/trigger/followup-sweep").status_code, 409)
            gate.finish(self.guard, QA_USER_ID, "followup-sweep")


if __name__ == "__main__":
    unittest.main()
