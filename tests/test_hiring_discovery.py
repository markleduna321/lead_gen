import contextlib
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from discovery_runs import DiscoveryBusyError, LocalDiscoveryRuns
from test_discovery_runs import ImmediateThread
from support import QA_USER_ID, guarded_web, signed_in_client
from scrapingdog_client import SCRAPINGDOG_LINKEDIN_JOBS_ENDPOINT
from hiring_services import HiringService


class HiringDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"APP_ENV": "local"}, clear=True), patch("dotenv.load_dotenv"):
            cls.jobs = importlib.import_module("discovery_jobs")
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True), patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live DB forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.api = importlib.import_module("control_gate_api")

    def setUp(self):
        self.network = patch("requests.get", side_effect=AssertionError("Live network forbidden")).start()
        self.database = patch("psycopg2.connect", side_effect=AssertionError("Live DB forbidden")).start()
        self.addCleanup(patch.stopall)
        self.log = patch.object(self.api, "emit_ui_log", Mock()).start()
        empty_linkedin = Mock()
        empty_linkedin.json.return_value = []
        self.linkedin_search_patch = patch.object(
            self.jobs, "fetch_linkedin_jobs", return_value=empty_linkedin
        )
        self.linkedin_search = self.linkedin_search_patch.start()

    def test_request_validation_deduplication_and_conflict(self):
        client = signed_in_client(self.api.app)
        runs = Mock()
        runs.start.return_value = "qa-run"
        with patch.object(self.api, "HIRING_DISCOVERY_RUNS", runs):
            for body in [
                [], {"service_types": "video_editing"}, {"service_types": []},
                {"service_types": [None]}, {"service_types": ["unknown"]},
                {"service_types": ["video_editing"], "country": None},
                {"service_types": ["video_editing"], "country": "Atlantis"},
                {"service_types": ["video_editing"], "country": "Toronto, Canada"},
            ]:
                with self.subTest(body=body):
                    self.assertEqual(client.post("/api/trigger/discover-jobs", json=body).status_code, 400)
            runs.start.assert_not_called()
            response = client.post("/api/trigger/discover-jobs", json={
                "service_types": ["video_editing", "video_editing"], "country": " Canada ",
            })
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json["run_id"], runs.start.call_args.kwargs["run_id"])
            tasks = runs.start.call_args.args[0]
            self.assertEqual(len(tasks), 1)
            with patch.object(self.jobs, "run_job_discovery") as worker:
                tasks[0][2]()
            worker.assert_called_once_with("video_editing", "Canada", pages_per_role=1, user_id=QA_USER_ID)
            runs.start.side_effect = DiscoveryBusyError("Hiring already running")
            self.assertEqual(client.post("/api/trigger/discover-jobs", json={
                "service_types": ["video_editing"]
            }).status_code, 409)

    def test_service_failures_continue_and_status_isolated(self):
        runs = LocalDiscoveryRuns("hiring")
        def worker(service, country, pages_per_role=1, user_id=None):
            runs.record_log(f"Scanning {service}")
            if service == "video_editing":
                raise RuntimeError("QA provider unavailable")
        with (
            patch.object(self.api, "HIRING_DISCOVERY_RUNS", runs),
            patch.object(self.jobs, "run_job_discovery", side_effect=worker) as scan,
            patch("discovery_runs.threading.Thread", ImmediateThread),
        ):
            client = signed_in_client(self.api.app)
            started = client.post("/api/trigger/discover-jobs", json={
                "service_types": ["video_editing", "virtual_assistant"], "country": "",
            })
            run = client.get(f"/api/discovery/job-runs/{started.json['run_id']}").json["run"]
            self.assertEqual(run["state"], "failed")
            self.assertEqual(run["completed"], 2)
            self.assertEqual(scan.call_count, 2)
            self.assertIn("QA provider unavailable", run["errors"][0])
            self.assertIn("Scanning virtual_assistant", run["logs"])
            self.assertEqual(client.get("/api/discovery/job-runs/missing").status_code, 404)

    def test_complete_hiring_pipeline_from_trigger_through_saved_api_records(self):
        for failure in (None, "page", "database"):
            with self.subTest(failure=failure):
                runs = LocalDiscoveryRuns("hiring")
                local_runs = LocalDiscoveryRuns()
                database = MagicMock()
                connection = database.__enter__.return_value
                cursor = connection.cursor.return_value.__enter__.return_value
                saved = []
                job_requests = []
                contact_searches = []
                descriptions = "Full provider description\n" * 100

                def network(url, **kwargs):
                    response = Mock(status_code=200)
                    if url in ("https://serpapi.com/search", "https://api.scrapingdog.com/google_jobs", "https://api.scrapingdog.com/google"):
                        params = kwargs["params"]
                        if params.get("engine") == "google_jobs" or url == "https://api.scrapingdog.com/google_jobs":
                            job_requests.append(dict(params))
                            self.assertEqual((params["gl"], params["location"]), ("ca", "Canada"))
                            if params.get("next_page_token"):
                                response.json.return_value = (
                                    {"error": "QA second-page failure"} if failure == "page" else
                                    {"jobs_results": [{
                                        "company_name": "Example Studios", "title": "Senior Video Editor",
                                        "location": "Toronto, Canada", "description": descriptions,
                                        "apply_options": [{"title": "Board", "link": "https://board.test/apply"}],
                                    }]}
                                )
                            else:
                                response.json.return_value = {
                                    "jobs_results": [
                                        {"company_name": "Example Studios", "title": "Senior Video Editor",
                                         "location": "Toronto, Canada", "description": descriptions,
                                         "detected_extensions": {"posted_at": "2 days ago", "health_insurance": True},
                                         "apply_options": [{"title": "Company", "link": "https://example.test/apply"}]},
                                        {"company_name": "Wrong Role", "title": "Truck Driver", "location": "Canada"},
                                        {"company_name": "Wrong Country", "title": "Video Editor", "location": "Australia"},
                                        {"company_name": "Unknown Eligibility", "title": "Video Editor", "location": "Remote"},
                                    ],
                                    "serpapi_pagination": {"next_page_token": "qa-next"},
                                }
                        else:
                            contact_searches.append(params["q"])
                            response.json.return_value = {"organic_results": [{
                                "title": "Example Studios | Official", "link": "https://example.test",
                            }]}
                    elif url == "https://example.test":
                        response.text = "<title>Example Studios</title><p>hello@example.test</p>"
                        response.headers = {"Content-Type": "text/html"}
                    elif url in {"https://example.test/contact", "https://example.test/about",
                                 "https://example.test/contact-us"}:
                        response.status_code = 404
                    else:
                        raise AssertionError(f"Unexpected request: {url}")
                    return response

                def write_rows(cur, query, rows):
                    self.assertIs(cur, cursor)
                    if failure == "database":
                        raise RuntimeError("QA database save failed")
                    columns = query.split("INSERT INTO job_leads (", 1)[1].split(")", 1)[0]
                    columns = [column.strip() for column in columns.split(",")]
                    for index, row in enumerate(rows, 1):
                        record = dict(zip(columns, row))
                        for key in ("job_details", "contact_evidence"):
                            record[key] = record[key].adapted if record[key] is not None else None
                        saved.append(dict(record, id=index))

                def record_log(message):
                    runs.record_log(message)
                    local_runs.record_log(message)

                with (
                    patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
                    patch.object(self.api, "HIRING_DISCOVERY_RUNS", runs),
                    patch.object(self.api, "LOCAL_DISCOVERY_RUNS", local_runs),
                    patch.object(self.api, "emit_ui_log", side_effect=record_log),
                    patch("requests.get", side_effect=network),
                    guarded_web(network),
                    patch("psycopg2.connect", return_value=database),
                    patch.object(self.jobs, "execute_values", side_effect=write_rows),
                    patch("discovery_runs.threading.Thread", ImmediateThread),
                ):
                    client = signed_in_client(self.api.app)
                    started = client.post("/api/trigger/discover-jobs", json={
                        "service_types": ["video_editing", "video_editing"],
                        "country": "Canada", "pages_per_role": 2,
                    })
                    self.assertEqual(started.status_code, 202)
                    self.assertNotIn("max_provider_credits", started.json)
                    self.assertNotIn("max_job_requests", started.json)
                    run_id = started.json["run_id"]
                    run = client.get(f"/api/discovery/job-runs/{run_id}").json["run"]
                    # A page error still saves the postings it collected, so the
                    # run reports success with a warning rather than failure.
                    expected_state = "failed" if failure == "database" else "succeeded"
                    self.assertEqual(run["state"], expected_state)
                    self.assertEqual((run["completed"], run["total"]), (1, 1))
                    self.assertTrue(run["logs"])
                    self.assertEqual(client.get(f"/api/discovery/runs/{run_id}").status_code, 404)
                    self.assertEqual(len(job_requests), 8)
                    self.assertEqual(len(contact_searches), 1)
                    if failure == "database":
                        self.assertEqual(saved, [])
                        connection.commit.assert_not_called()
                        self.assertIn("QA database save failed", run["errors"][0])
                    else:
                        self.assertEqual(len(saved), 1)
                        connection.commit.assert_called_once()
                        self.assertEqual(run["errors"], [])
                        if failure == "page":
                            self.assertIn("QA second-page failure", run["warnings"][0])
                            self.assertEqual(run["saved"], 1)
                        else:
                            self.assertEqual(run["warnings"], [])
                            self.assertEqual(run["saved"], 1)
                        cursor.fetchone.return_value = {
                            "total": 1, "pending": 1, "sent": 0, "with_email": 1,
                        }
                        cursor.fetchall.return_value = saved
                        fetched = client.get("/api/job-leads?lead_type=video_editing")
                        self.assertEqual(fetched.status_code, 200)
                        lead = fetched.json["leads"][0]
                        self.assertEqual(lead["job_title"], "Senior Video Editor")
                        self.assertEqual(lead["job_description"], descriptions.strip())
                        self.assertEqual(lead["email"], "hello@example.test")
                        self.assertEqual(lead["contact_evidence"]["email_status"], "public_domain_match")
                        self.assertEqual(lead["job_details"]["posted_at"], "2 days ago")
                        self.assertEqual(len(lead["job_details"]["apply_options"]), 1 if failure else 2)

    def test_worker_continues_after_bad_postings_and_role_errors(self):
        response = Mock()
        response.json.side_effect = [
            {"error": "QA quota error"},
            {"jobs_results": [
                {"company_name": "Bad", "title": None},
                {"company_name": "Valid", "title": "Video Editor", "location": "Canada"},
            ]},
            {"jobs_results": []}, {"jobs_results": []},
        ]
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response),
            patch.object(self.jobs, "enrich_company_contact", return_value={"website_url": None, "email": None}),
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            from discovery_runs import DiscoveryPartialSuccess
            # The valid posting was saved, so this is a partial success and must
            # not be reported as an outright failure.
            with self.assertRaises(DiscoveryPartialSuccess) as partial:
                self.jobs.run_job_discovery("video_editing", "Canada", user_id=QA_USER_ID)
        self.assertEqual(partial.exception.saved, 1)
        self.assertTrue(any("QA quota error" in message for message in partial.exception.messages))
        self.assertEqual(response.json.call_count, 4)
        self.assertEqual([lead["company_name"] for lead in save.call_args.args[0]], ["Valid"])

    def test_empty_success_missing_key_and_database_failure_are_distinct(self):
        response = Mock()
        response.json.return_value = {"jobs_results": []}
        with patch.object(self.jobs, "SERPAPI_KEY", "qa-only"), patch("requests.get", return_value=response):
            self.assertEqual(self.jobs.run_job_discovery("video_editing", user_id=QA_USER_ID), 0)
        with patch.object(self.jobs, "SERPAPI_KEY", None):
            with self.assertRaisesRegex(RuntimeError, "requires SERPAPI_API_KEY"):
                self.jobs.run_job_discovery("video_editing", user_id=QA_USER_ID)
        row = {"company_name": "QA", "job_title": "Editor", "lead_type": "video_editing"}
        with self.assertRaisesRegex(AssertionError, "Live DB forbidden"):
            self.jobs.save_job_leads([row], QA_USER_ID)

    def test_enrichment_errors_save_posting_but_fail_run(self):
        response = Mock()
        response.json.return_value = {"jobs_results": [{"company_name": "QA", "title": "Video Editor"}]}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response),
            patch.object(self.jobs, "enrich_company_contact", side_effect=RuntimeError("QA contact failure")),
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "QA contact failure"):
                self.jobs.run_job_discovery("video_editing", user_id=QA_USER_ID)
        self.assertTrue(all(lead["email"] is None for lead in save.call_args.args[0]))

    def test_posting_validation(self):
        for invalid in [
            None, {"company_name": [], "title": "Editor"},
            {"company_name": "QA", "title": ""},
            {"company_name": "QA", "title": "Editor", "apply_options": ["bad"]},
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.jobs.parse_job_posting(invalid, "video_editing", "video editor")
        lead = self.jobs.parse_job_posting(
            {"company_name": "QA", "title": "Editor", "location": None}, "video_editing", "video editor"
        )
        self.assertEqual(lead["job_title"], "Editor")
        self.assertEqual(lead["location"], "Unknown")

    def test_targeting_filters_before_enrichment(self):
        response = Mock()
        response.json.return_value = {"jobs_results": [
            {"company_name": "Wrong role", "title": "Truck Driver", "location": "Canada"},
            {"company_name": "Wrong country", "title": "Video Editor", "location": "Australia"},
            {"company_name": "Unknown", "title": "Video Editor", "location": "Remote",
             "description": "Our headquarters are in Canada."},
            {"company_name": "Missing", "title": "Video Editor"},
            {"company_name": "Valid", "title": "Senior Video Editor", "location": "Remote - Canada"},
        ]}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response) as search,
            patch.object(self.jobs, "enrich_company_contact",
                         return_value={"website_url": None, "email": None}) as enrich,
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            self.assertEqual(self.jobs.run_job_discovery("video_editing", "Canada", user_id=QA_USER_ID), 1)
        self.assertEqual(search.call_args.kwargs["params"]["gl"], "ca")
        self.assertEqual(search.call_args.kwargs["params"]["location"], "Canada")
        enrich.assert_called_once_with("Valid", "Remote - Canada")
        self.assertEqual({lead["company_name"] for lead in save.call_args.args[0]}, {"Valid"})

    def test_blank_country_preserves_unscoped_remote_but_filters_roles(self):
        response = Mock()
        response.json.return_value = {"jobs_results": [
            {"company_name": "Remote company", "title": "Video Editor", "location": "Remote"},
            {"company_name": "Unrelated", "title": "Truck Driver", "location": "Canada"},
        ]}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response) as search,
            patch.object(self.jobs, "enrich_company_contact",
                         return_value={"website_url": None, "email": None}),
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            self.assertEqual(self.jobs.run_job_discovery("video_editing", "", user_id=QA_USER_ID), 1)
        self.assertNotIn("gl", search.call_args.kwargs["params"])
        self.assertNotIn("location", search.call_args.kwargs["params"])
        self.assertEqual({lead["location"] for lead in save.call_args.args[0]}, {"Remote"})

    def test_fully_filtered_search_is_empty_success_without_enrichment_or_save(self):
        response = Mock()
        response.json.return_value = {"jobs_results": [
            {"company_name": "Wrong country", "title": "Video Editor", "location": "Australia"},
        ]}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response),
            patch.object(self.jobs, "enrich_company_contact") as enrich,
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            self.assertEqual(self.jobs.run_job_discovery("video_editing", "Canada", user_id=QA_USER_ID), 0)
        enrich.assert_not_called()
        save.assert_not_called()

    def test_duplicate_postings_across_roles_keep_distinct_titles_and_locations(self):
        response = Mock()
        response.json.return_value = {"jobs_results": [
            {"company_name": "QA Company", "title": "Video Editor", "location": "Toronto, Canada"},
            {"company_name": "  qa   company ", "title": "VIDEO EDITOR", "location": "Toronto, Canada"},
            {"company_name": "QA Company", "title": "Video Editor", "location": "Vancouver, Canada"},
            {"company_name": "QA Company", "title": "Senior Video Editor", "location": "Toronto, Canada"},
        ]}
        def enrich(company, location):
            return {"website_url": None, "email": f"{location.split(',')[0].lower()}@example.test"}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response),
            patch.object(self.jobs, "enrich_company_contact", side_effect=enrich) as contacts,
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            self.assertEqual(self.jobs.run_job_discovery("video_editing", "Canada", user_id=QA_USER_ID), 3)
        self.assertEqual(contacts.call_count, 2)
        leads = save.call_args.args[0]
        self.assertEqual(len(leads), 3)
        for lead in leads:
            self.assertEqual(lead["email"], f"{lead['location'].split(',')[0].lower()}@example.test")

    def test_save_deduplicates_batch_and_reuses_identity_between_search_roles(self):
        first = self.jobs.parse_job_posting({
            "company_name": "QA", "title": "Video Editor", "location": "Canada",
            "description": "Existing details",
        }, "video_editing", "video editor")
        repeated = self.jobs.parse_job_posting({
            "company_name": " qa ", "title": "VIDEO EDITOR", "location": " canada ",
            "apply_options": [{"link": "https://example.test/apply"}],
        }, "video_editing", "video content creator")
        other_location = dict(first, location="Australia")
        other_title = dict(first, job_title="Senior Video Editor")
        other_service = dict(first, lead_type="social_media_management")
        database = MagicMock()
        with (
            patch("psycopg2.connect", return_value=database),
            patch.object(self.jobs, "execute_values") as execute,
        ):
            self.jobs.save_job_leads([first, repeated, other_location, other_title, other_service], QA_USER_ID)
            query, rows = execute.call_args.args[1:]
            self.assertEqual(len(rows), 4)
            self.assertIn("ON CONFLICT (user_id, job_identity)", query)
            self.assertIn("job_identity", query.split("VALUES")[0])
            self.assertNotIn("status =", query)
            self.assertNotIn("ai_pitch_draft =", query)
            self.assertIn("NULLIF(EXCLUDED.email, '')", query)
            self.assertEqual(rows[0][8], "Existing details")
            self.assertEqual(rows[0][9], "https://example.test/apply")
            self.assertEqual(rows[0][-4].adapted["apply_options"][0]["link"], "https://example.test/apply")
            self.assertIn("WHEN job_leads.contact_evidence->>'email_status' = 'manual'", query)
            identities = [row[-2] for row in rows]
            self.assertEqual(len(set(identities)), 4)
            self.jobs.save_job_leads([repeated], QA_USER_ID)
            self.assertEqual(execute.call_args.args[2][0][-2], identities[0])
        self.assertEqual(database.__enter__.return_value.commit.call_count, 2)

    def test_identity_does_not_collapse_long_titles_or_unicode_names(self):
        def identity(company="QA", title="Video Editor", location="Canada", service="video_editing"):
            return self.jobs.job_lead_identity(company, title, location, service)
        self.assertEqual(identity(), identity(company=" \uff51\uff41 ", title=" video  editor ", location="CANADA"))
        self.assertNotEqual(identity(company="Caf\u00e9"), identity(company="Cafe"))
        prefix = "Video Editor " + "x" * 600
        self.assertNotEqual(identity(title=prefix + "A"), identity(title=prefix + "B"))
        self.assertNotEqual(identity(location="Unknown"), identity(location="Remote"))

    def test_full_posting_details_and_all_application_links_are_retained(self):
        posting = {
            "company_name": "QA", "title": "Video Editor", "description": "Full description\n" * 100,
            "job_id": "provider-id", "share_link": "https://google.com/search?job=qa",
            "detected_extensions": {"posted_at": "3 days ago", "schedule_type": "Full-time",
                                    "health_insurance": True, "work_from_home": True},
            "extensions": ["3 days ago", "Full-time"],
            "job_highlights": [{"title": "Benefits", "items": ["Health insurance"]}],
            "apply_options": [{"title": "Company", "link": "https://example.test/jobs/1"},
                              {"title": "Board", "link": "https://board.test/1"}],
        }
        lead = self.jobs.parse_job_posting(posting, "video_editing", "video editor")
        self.assertEqual(lead["job_description"], posting["description"].strip())
        details = lead["job_details"]
        self.assertEqual(details["provider_job_id"], "provider-id")
        self.assertEqual(details["posted_at"], "3 days ago")
        self.assertTrue(details["detected_extensions"]["health_insurance"])
        self.assertEqual(details["apply_options"], posting["apply_options"])
        self.assertEqual(details["job_highlights"], posting["job_highlights"])
        self.assertIn("+00:00", details["collected_at"])

    def test_company_search_does_not_accept_unrelated_first_website(self):
        response = Mock()
        response.json.return_value = {"organic_results": [
            {"title": "Completely unrelated company", "link": "https://unrelated.test",
             "snippet": "An unrelated business."},
        ]}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response),
            patch("email_hunter.attempt_domain_email_crawl", return_value="wrong@unrelated.test"),
        ):
            result = self.jobs.enrich_company_contact("Example Studios", "Canada")
        self.assertIsNone(result["website_url"])
        self.assertIsNone(result["email"])

    def test_invalid_metadata_and_links_are_handled_explicitly(self):
        base = {"company_name": "QA", "title": "Video Editor"}
        for extra in [
            {"job_highlights": "invalid"}, {"job_highlights": [{"items": "invalid"}]},
            {"extensions": [None]}, {"job_id": []}, {"detected_extensions": {"posted_at": []}},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.jobs.parse_job_posting(dict(base, **extra), "video_editing", "video editor")
        lead = self.jobs.parse_job_posting(dict(base, apply_options=[
            {"link": "javascript:alert(1)"}, {"link": "https://example.test/apply", "title": "Company"},
        ]), "video_editing", "video editor")
        self.assertEqual(lead["job_apply_link"], "https://example.test/apply")
        self.assertEqual(len(lead["job_details"]["apply_options"]), 1)
        self.assertTrue(any("Ignored invalid application URL" in call.args[0]
                            for call in self.log.call_args_list))

    def test_partial_contact_page_failure_saves_evidence_and_marks_run_failed(self):
        response = Mock()
        response.json.return_value = {"jobs_results": [{"company_name": "QA", "title": "Video Editor"}]}
        evidence = {"status": "matched", "email_status": "public_domain_match"}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", return_value=response),
            patch.object(self.jobs, "enrich_company_contact", return_value={
                "website_url": "https://example.test", "email": "info@example.test",
                "contact_evidence": evidence, "errors": ["QA contact page failed"],
            }),
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "QA contact page failed"):
                self.jobs.run_job_discovery("video_editing", user_id=QA_USER_ID)
        self.assertEqual(save.call_args.args[0][0]["contact_evidence"], evidence)

    def test_duplicates_combine_metadata_links_and_keep_longest_description(self):
        first = self.jobs.parse_job_posting({
            "company_name": "QA", "title": "Video Editor", "description": "Short description",
            "apply_options": [{"title": "Company", "link": "https://example.test/apply"}],
            "detected_extensions": {"posted_at": "2 days ago"},
        }, "video_editing", "video editor")
        second = self.jobs.parse_job_posting({
            "company_name": "QA", "title": "Video Editor", "description": "Full description " * 200,
            "apply_options": [{"title": "Board", "link": "https://board.test/apply"}],
            "detected_extensions": {"health_insurance": True},
            "job_highlights": [{"title": "Benefits", "items": ["Insurance"]}],
        }, "video_editing", "video content creator")
        unique = self.jobs.deduplicate_job_leads([first, second])
        self.assertEqual(len(unique), 1)
        self.assertEqual(unique[0]["job_description"], second["job_description"])
        self.assertEqual(len(unique[0]["job_details"]["apply_options"]), 2)
        self.assertEqual(unique[0]["job_details"]["detected_extensions"],
                         {"posted_at": "2 days ago", "health_insurance": True})
        database = MagicMock()
        with patch("psycopg2.connect", return_value=database), patch.object(self.jobs, "execute_values") as execute:
            self.jobs.save_job_leads(unique, QA_USER_ID)
        row = execute.call_args.args[2][0]
        self.assertEqual(row[8], second["job_description"])
        self.assertEqual(len(row[-4].adapted["apply_options"]), 2)

    def test_save_keeps_contact_values_and_evidence_together(self):
        first = self.jobs.parse_job_posting({
            "company_name": "QA", "title": "Video Editor",
        }, "video_editing", "video editor")
        first.update(website_url=None, email=None, contact_evidence={"status": "unconfirmed"})
        evidence = {
            "status": "matched", "website_source_url": "https://example.test",
            "email_status": "public_domain_match",
            "emails": [{"email": "info@example.test", "source_url": "https://example.test/contact"}],
        }
        second = dict(first, website_url="https://example.test",
                      email="info@example.test", contact_evidence=evidence)
        database = MagicMock()
        with patch("psycopg2.connect", return_value=database), patch.object(self.jobs, "execute_values") as execute:
            self.jobs.save_job_leads([first, second], QA_USER_ID)
        rows = execute.call_args.args[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][10:12], ("https://example.test", "info@example.test"))
        self.assertEqual(rows[0][-3].adapted, evidence)

    def test_pages_follow_tokens_preserve_filters_and_deduplicate(self):
        posting = {"company_name": "QA", "title": "Video Editor", "location": "Canada"}
        first, second = Mock(), Mock()
        first.json.return_value = {"jobs_results": [posting], "serpapi_pagination": {"next_page_token": "qa-next"}}
        second.json.return_value = {"jobs_results": [posting, dict(posting, company_name="Other")]}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", side_effect=[first, second] * 4) as search,
            patch.object(self.jobs, "enrich_company_contact",
                         return_value={"website_url": None, "email": None}) as enrich,
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            self.assertEqual(self.jobs.run_job_discovery("video_editing", "Canada", pages_per_role=5, user_id=QA_USER_ID), 2)
        self.assertEqual(search.call_count, 8)
        self.assertEqual(enrich.call_count, 2)
        self.assertEqual(len(save.call_args.args[0]), 2)
        for index, call in enumerate(search.call_args_list):
            params = call.kwargs["params"]
            self.assertEqual(params["gl"], "ca")
            self.assertEqual(params["location"], "Canada")
            if index % 2:
                self.assertEqual(params["next_page_token"], "qa-next")
            else:
                self.assertNotIn("next_page_token", params)

    def test_default_single_page_never_follows_next_token(self):
        response = Mock()
        response.json.return_value = {
            "jobs_results": [], "serpapi_pagination": {"next_page_token": "unused"},
        }
        with patch.object(self.jobs, "SERPAPI_KEY", "qa-only"), patch("requests.get", return_value=response) as search:
            self.assertEqual(self.jobs.run_job_discovery("video_editing", user_id=QA_USER_ID), 0)
        self.assertEqual(search.call_count, 4)

    def test_later_page_failure_retains_postings_and_continues_roles(self):
        first, failed = Mock(), Mock()
        first.json.return_value = {"jobs_results": [{"company_name": "QA", "title": "Video Editor"}],
                                   "serpapi_pagination": {"next_page_token": "next"}}
        failed.json.return_value = {"error": "QA page failure"}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch("requests.get", side_effect=[first, failed] * 4) as search,
            patch.object(self.jobs, "enrich_company_contact", return_value={"website_url": None, "email": None}),
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "page 2.*QA page failure"):
                self.jobs.run_job_discovery("video_editing", pages_per_role=3, user_id=QA_USER_ID)
        self.assertEqual(search.call_count, 8)
        self.assertEqual(len(save.call_args.args[0]), 1)

    def test_page_budget_and_repeated_or_invalid_tokens(self):
        for pagination, expected, message in [
            ({"next_page_token": "same"}, 2, "repeated"),
            ({"next_page_token": 123}, 1, "token"),
            ("bad", 1, "pagination"),
        ]:
            response = Mock()
            response.json.return_value = {"jobs_results": [], "serpapi_pagination": pagination}
            with (
                self.subTest(pagination=pagination),
                patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
                patch("requests.get", return_value=response) as search,
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.jobs.run_job_discovery("video_editing", pages_per_role=5, user_id=QA_USER_ID)
                self.assertEqual(search.call_count, expected * 4)
        response = Mock()
        response.json.side_effect = [
            {"jobs_results": [], "serpapi_pagination": {"next_page_token": f"token-{index}"}}
            for index in range(20)
        ]
        with patch.object(self.jobs, "SERPAPI_KEY", "qa-only"), patch("requests.get", return_value=response) as search:
            self.assertEqual(self.jobs.run_job_discovery("video_editing", pages_per_role=5, user_id=QA_USER_ID), 0)
        self.assertEqual(search.call_count, 20)

    def test_depth_validation_and_request_budget_before_worker_start(self):
        runs = Mock()
        runs.start.return_value = "qa-depth"
        client = signed_in_client(self.api.app)
        with patch.object(self.api, "HIRING_DISCOVERY_RUNS", runs):
            for value in [None, True, False, 0, -1, 4, 6, 100, 1.0, "2", [], {}]:
                with self.subTest(value=value):
                    response = client.post("/api/trigger/discover-jobs", json={
                        "service_types": ["video_editing"], "pages_per_role": value,
                    })
                    self.assertEqual(response.status_code, 400)
            runs.start.assert_not_called()
            for pages in [1, 2, 3, 5]:
                with self.subTest(pages=pages):
                    response = client.post("/api/trigger/discover-jobs", json={
                        "service_types": ["video_editing", "virtual_assistant", "video_editing"],
                        "pages_per_role": pages,
                    })
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.json["pages_per_role"], pages)
                    self.assertNotIn("max_provider_credits", response.json)
                    self.assertNotIn("max_job_requests", response.json)
                    tasks = runs.start.call_args.args[0]
                    with patch.object(self.jobs, "run_job_discovery") as worker:
                        tasks[0][2]()
                        tasks[1][2]()
                    self.assertEqual(worker.call_args_list[0].kwargs, {"pages_per_role": pages, "user_id": QA_USER_ID})
                    self.assertEqual(worker.call_args_list[1].kwargs, {"pages_per_role": pages, "user_id": QA_USER_ID})
        for value in [None, True, "2", 4, 6]:
            with self.subTest(direct=value), patch.object(self.jobs, "SERPAPI_KEY", "qa-only"):
                with self.assertRaisesRegex(ValueError, "Pages per role"):
                    self.jobs.run_job_discovery("video_editing", pages_per_role=value, user_id=QA_USER_ID)
        self.network.assert_not_called()

    def test_linkedin_jobs_results_are_normalized_deduplicated_and_enriched_with_google_jobs(self):
        service = HiringService(
            "custom_video_test", "Video Editing", ("Video Editor",), ("video editor",)
        )
        google = Mock()
        google.json.return_value = {"jobs_results": [{
            "company_name": "Acme Studio", "title": "Video Editor",
            "location": "Toronto, Canada", "description": "Google description",
            "via": "Indeed",
        }]}
        linkedin = Mock()
        linkedin.json.return_value = [
            {
                "job_position": "Video Editor", "company_name": "Acme Studio",
                "job_location": "Toronto, Canada", "job_description": "LinkedIn description",
                "job_id": "linkedin-1",
                "job_link": "https://www.linkedin.com/jobs/view/123",
                "company_profile": "https://www.linkedin.com/company/acme",
                "job_posting_date": "2 days ago",
            },
            {
                "job_position": "Video Editor", "company_name": "North Studio",
                "job_location": "Vancouver, Canada", "job_description": "Another posting",
                "job_id": "linkedin-2",
            },
        ]
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-key"),
            patch.object(self.jobs, "serpapi_fetch", return_value=google),
            patch.object(self.jobs, "fetch_linkedin_jobs", return_value=linkedin) as search_linkedin,
            patch.object(self.jobs, "enrich_company_contact",
                         return_value={"website_url": "https://example.test", "email": "info@example.test"}),
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            count = self.jobs.run_job_discovery(
                service.id, "Canada", service_definition=service, user_id=QA_USER_ID
            )
        self.assertEqual(count, 2)
        search_linkedin.assert_called_once_with("Video Editor", "Canada", 1, self.api.emit_ui_log)
        leads = save.call_args.args[0]
        self.assertEqual(len(leads), 2)
        acme = next(lead for lead in leads if lead["company_name"] == "Acme Studio")
        self.assertEqual(acme["job_via"], "Indeed")
        self.assertEqual(
            acme["job_details"]["search_providers"], ["Google Jobs", "LinkedIn Jobs"]
        )
        self.assertEqual(acme["job_description"], "LinkedIn description")
        self.assertIn(
            {"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/123"},
            acme["job_details"]["apply_options"],
        )
        self.assertEqual(
            acme["job_details"]["linkedin_company_profile"],
            "https://www.linkedin.com/company/acme",
        )
        self.assertEqual(acme["job_details"]["posted_at"], "2 days ago")
        self.assertEqual(acme["email"], "info@example.test")

    def test_linkedin_jobs_uses_documented_scrapingdog_endpoint_and_parameters(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        self.linkedin_search_patch.stop()
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-key"),
            patch.object(self.jobs, "scrapingdog_fetch", return_value=response) as fetch,
        ):
            result = self.jobs.fetch_linkedin_jobs("Video Editor", "Canada", 2, self.log)
        self.assertIs(result, response)
        fetch.assert_called_once()
        params = fetch.call_args.args[0]
        self.assertEqual(fetch.call_args.kwargs["endpoint"], SCRAPINGDOG_LINKEDIN_JOBS_ENDPOINT)
        self.assertEqual(params["field"], "Video Editor")
        self.assertEqual(params["location"], "Canada")
        self.assertEqual(params["page"], 2)
        self.assertEqual(params["api_key"], "qa-key")

    def test_linkedin_jobs_failure_is_reported_as_partial_when_google_saved_postings(self):
        service = HiringService(
            "custom_video_test", "Video Editing", ("Video Editor",), ("video editor",)
        )
        google = Mock()
        google.json.return_value = {"jobs_results": [{
            "company_name": "Acme Studio", "title": "Video Editor", "location": "Canada",
        }]}
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-key"),
            patch.object(self.jobs, "serpapi_fetch", return_value=google),
            patch.object(self.jobs, "fetch_linkedin_jobs", side_effect=RuntimeError("LinkedIn API unavailable")),
            patch.object(self.jobs, "enrich_company_contact",
                         return_value={"website_url": None, "email": None}),
            patch.object(self.jobs, "save_job_leads") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "LinkedIn API unavailable"):
                self.jobs.run_job_discovery(
                    service.id, "Canada", service_definition=service, user_id=QA_USER_ID
                )
        self.assertEqual(len(save.call_args.args[0]), 1)
