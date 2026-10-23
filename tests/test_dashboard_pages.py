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
from support import QA_USER_ID, signed_in_client


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
        cls.client = signed_in_client(cls.module.app)

    def test_all_pages_render_without_database_access(self):
        cases = [
            ("/", "overview", "Overview"),
            ("/overview", "overview", "Overview"),
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
        for asset in ("dashboard.css", "workspace.js", "home.css"):
            with self.subTest(asset=asset):
                response = self.client.get(f"/static/{asset}")
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_public_home_is_available_without_database_access(self):
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            response = self.module.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-page="home"', html)
        self.assertIn('id="theme-preference"', html)
        for preference in ('system', 'light', 'dark'):
            self.assertIn(f'value="{preference}"', html)
        self.assertIn('href="/login"', html)
        self.assertIn('href="/register"', html)
        self.assertIn('id="platform"', html)
        self.assertIn('id="how-it-works"', html)
        self.assertIn('PRODUCT PREVIEW', html)
        self.assertNotIn('id="table-body"', html)

    def test_public_home_clears_a_removed_account_session(self):
        client = signed_in_client(self.module.app, user_id=4242)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-page="home"', response.get_data(as_text=True))
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

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
            query, args = cursor.execute.call_args_list[1].args
            self.assertIn("website_url, profile_url", query)
            self.assertIn("instagram", args)
            exported = self.client.get("/api/export")
            rows = list(csv.DictReader(io.StringIO(exported.get_data(as_text=True))))
            self.assertEqual(rows[0]["profile_url"], lead["profile_url"])
            self.assertEqual(rows[0]["website_url"], "")
            self.assertEqual(rows[0]["email"], lead["email"])

    def test_queue_filters_match_the_displayed_source_and_normalised_industry(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 0, "pending": 0, "sent": 0, "followup": 0}
        cursor.fetchall.side_effect = [[], [
            {"facet": "source", "value": "facebook", "label": "facebook", "count": 2},
            {"facet": "category", "value": "law firm", "label": "Law firm", "count": 2},
            {"facet": "status", "value": "sent_followup_1", "label": "sent_followup_1", "count": 1},
            {"facet": "category", "value": "", "label": "", "count": 9},
        ]]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/queue?source=Facebook&category=Law%20Firm%20&q=main")
        self.assertEqual(response.status_code, 200)
        query, args = cursor.execute.call_args_list[1].args
        self.assertIn("LEFT(COALESCE(google_place_id, ''), 3) = 'fb_'", query)
        self.assertIn("LOWER(BTRIM(COALESCE(category, ''))) = LOWER(%s)", query)
        self.assertIn("COALESCE(formatted_address, '') ILIKE %s", query)
        self.assertNotIn("lead_type, 'web_design') = %s", query)
        self.assertIn("facebook", args)
        self.assertIn("Law Firm", args)
        self.assertEqual(response.json["filter_options"], {
            "source": [{"value": "facebook", "label": "facebook", "count": 2}],
            "category": [{"value": "law firm", "label": "Law firm", "count": 2}],
            "status": [{"value": "sent_followup_1", "label": "sent_followup_1", "count": 1}],
        })

    def test_job_lead_filters_cover_job_board_and_job_search_fields(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 0, "pending": 0, "sent": 0, "with_email": 0}
        cursor.fetchall.side_effect = [[], [
            {"facet": "via", "value": "linkedin", "label": "LinkedIn", "count": 3},
        ]]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/job-leads?via=LinkedIn&lead_type=video_editing&q=Toronto")
        self.assertEqual(response.status_code, 200)
        query, args = cursor.execute.call_args_list[1].args
        self.assertIn("REGEXP_REPLACE(COALESCE(job_via, ''), '^\\s*via\\s+', '', 'i')", query)
        self.assertIn("COALESCE(location, '') ILIKE %s", query)
        self.assertIn("lead_type = %s", query)
        self.assertIn("LinkedIn", args)
        self.assertEqual(args.count("%Toronto%"), 5)
        self.assertEqual(response.json["filter_options"],
                         {"via": [{"value": "linkedin", "label": "LinkedIn", "count": 3}]})

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

    def test_job_identity_migration_preserves_legacy_rows_and_matches_schema(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with (
            patch.object(self.module, "DB_DSN", "qa-only"),
            patch("psycopg2.connect", return_value=database),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.module.run_startup_migrations()
            first = [call.args[0] for call in cursor.execute.call_args_list]
            cursor.execute.reset_mock()
            self.module.run_startup_migrations()
            second = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(first, second)
        add = "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_identity TEXT;"
        owner = "ALTER TABLE job_leads        ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;"
        index = "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_leads_user_identity   ON job_leads (user_id, job_identity);"
        drop = "ALTER TABLE job_leads DROP CONSTRAINT IF EXISTS unique_job_lead;"
        # Identity and ownership must both exist before the per-user unique index.
        self.assertLess(first.index(add), first.index(owner))
        self.assertLess(first.index(owner), first.index(index))
        self.assertLess(first.index(drop), first.index(index))
        schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()
        for statement in (add, owner, index, drop):
            self.assertIn(statement, first)
            self.assertIn(statement, schema)
        # The old global unique index must not be recreated on startup.
        self.assertNotIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_leads_identity ON job_leads (job_identity);",
            first,
        )
        create = next(sql for sql in first if "CREATE TABLE IF NOT EXISTS job_leads" in sql)
        self.assertIn("job_identity    TEXT,", create)
        self.assertNotIn("UNIQUE (company_name, job_role_signal)", create)
        self.assertFalse(any(sql.lstrip().upper().startswith(
            ("UPDATE ", "DELETE ", "TRUNCATE ", "DROP TABLE ")
        ) for sql in first))
        self.assertEqual(database.__enter__.return_value.commit.call_count, 2)

    def test_richer_job_fields_reach_api_and_manual_email_does_not_claim_matching(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 1, "pending": 1, "sent": 0, "with_email": 1}
        details = {"posted_at": "2 days ago", "apply_options": [
            {"title": "Company", "link": "https://example.test/apply"},
        ]}
        evidence = {"status": "matched", "email_status": "public_domain_match",
                    "email_source_url": "https://example.test/contact"}
        cursor.fetchall.return_value = [{
            "id": 7, "company_name": "QA", "job_title": "Video Editor",
            "job_description": "Full description " * 100, "job_details": details,
            "contact_evidence": evidence,
        }]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/job-leads")
            self.assertEqual(response.status_code, 200)
            lead = response.json["leads"][0]
            self.assertEqual(lead["job_details"], details)
            self.assertEqual(lead["contact_evidence"], evidence)
            self.assertGreater(len(lead["job_description"]), 600)
            select = cursor.execute.call_args_list[1].args[0]
            self.assertIn("job_details, contact_evidence", select)
            response = self.client.post("/api/job-leads/7/update-email", json={"email": "manual@example.test"})
            self.assertEqual(response.status_code, 200)
            sql, args = cursor.execute.call_args.args
            self.assertEqual(args, ("manual@example.test", 7, QA_USER_ID))
            self.assertIn("'email_status', 'manual'", sql)
            self.assertIn("'email_source_url', NULL", sql)

    def test_richer_job_migration_is_additive_without_backfill(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with (
            patch.object(self.module, "DB_DSN", "qa-only"),
            patch("psycopg2.connect", return_value=database),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.module.run_startup_migrations()
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()
        for column in ("job_details", "contact_evidence"):
            statement = f"ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS {column} JSONB;"
            self.assertIn(statement, queries)
            self.assertIn(statement, schema)
        self.assertFalse(any(sql.lstrip().upper().startswith(("UPDATE ", "DELETE ")) for sql in queries))

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
