import contextlib
import csv
import importlib
import io
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from discovery_runs import LocalDiscoveryRuns


class DashboardPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"CARTO_BASEMAP_API_KEY": "qa-public-key"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.module = importlib.import_module("control_gate_api")
        cls.client = cls.module.app.test_client()

    def test_all_pages_render_without_database_access(self):
        cases = [
            ("/", "overview", "Overview"),
            ("/prospects", "prospects", "Prospects"),
            ("/job-leads", "jobs", "Job Leads"),
            ("/discovery", "discovery", "Discovery"),
        ]
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            for url, key, title in cases:
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertIn(f'data-page="{key}"', html)
                    self.assertIn(f"<title>{title} | AsuraTECH</title>", html)
                    self.assertEqual(html.count('aria-current="page"'), 1)
                    self.assertIn('href="/prospects"', html)
                    self.assertIn('href="/job-leads"', html)
                    self.assertIn('href="/discovery"', html)

    def test_overview_and_discovery_have_dedicated_content(self):
        overview = self.client.get("/").get_data(as_text=True)
        discovery = self.client.get("/discovery").get_data(as_text=True)
        self.assertIn('id="overview-recent-prospects"', overview)
        self.assertNotIn('id="overview-recent-prospects"', discovery)
        self.assertIn('role="region" aria-label="Discover leads"', discovery)
        self.assertIn('id="discovery-panel-jobs"', discovery)

    def test_shared_assets_are_available(self):
        for asset in ("dashboard.css", "workspace.js"):
            with self.subTest(asset=asset):
                response = self.client.get(f"/static/{asset}")
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_prospect_queue_and_export_preserve_separate_profile_urls(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 1, "pending": 1, "sent": 0, "followup": 0}
        lead = {
            "id": 1, "business_name": "QA", "source_platform": "instagram",
            "profile_url": "https://instagram.com/qa", "website_url": None,
            "email": "qa@example.test",
        }
        cursor.fetchall.return_value = [lead]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/queue?source=instagram")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["leads"][0]["profile_url"], lead["profile_url"])
            query, args = cursor.execute.call_args.args
            self.assertIn("website_url, profile_url", query)
            self.assertIn("instagram", args)
            exported = self.client.get("/api/export")
            rows = list(csv.DictReader(io.StringIO(exported.get_data(as_text=True))))
            self.assertEqual(rows[0]["profile_url"], lead["profile_url"])
            self.assertEqual(rows[0]["website_url"], "")
            self.assertEqual(rows[0]["email"], lead["email"])

    def test_profile_column_startup_migration_is_idempotent_and_has_no_backfill(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with (
            patch.object(self.module, "DB_DSN", "qa-only"),
            patch("psycopg2.connect", return_value=database),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.module.run_startup_migrations()
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS profile_url         TEXT;", queries)
        self.assertFalse(any(query.lstrip().upper().startswith("UPDATE ") for query in queries))

    def test_local_run_endpoint_reports_partial_failure_and_unknown_run(self):
        from unittest.mock import Mock
        runs = LocalDiscoveryRuns()
        google = types.ModuleType("discovery_engine")
        google.run_discovery = Mock(side_effect=RuntimeError("QA quota exceeded"))
        yelp = types.ModuleType("discovery_yelp")
        yelp.run_yelp_discovery = Mock()
        def thread_factory(target, args, daemon):
            thread = Mock()
            thread.start.side_effect = lambda: target(*args)
            return thread
        with (
            patch.object(self.module, "LOCAL_DISCOVERY_RUNS", runs),
            patch.dict(sys.modules, {"discovery_engine": google, "discovery_yelp": yelp}),
            patch("discovery_runs.threading.Thread", side_effect=thread_factory),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            started = self.client.post("/api/trigger/discover", json={
                "location": "Austin", "categories": ["Dental Clinic"],
                "sources": ["google", "yelp"], "lat": 30, "lng": -97,
            })
            self.assertEqual(started.status_code, 202)
            result = self.client.get(f"/api/discovery/runs/{started.json['run_id']}")
            self.assertEqual(result.status_code, 200)
            run = result.json["run"]
            self.assertEqual(run["state"], "failed")
            self.assertEqual(run["completed"], 2)
            self.assertIn("quota exceeded", run["errors"][0])
            self.assertTrue(run["logs"])
            yelp.run_yelp_discovery.assert_called_once()
            missing = self.client.get("/api/discovery/runs/missing")
            self.assertEqual(missing.status_code, 404)
            self.assertIn("restarted", missing.json["message"])


if __name__ == "__main__":
    unittest.main()
