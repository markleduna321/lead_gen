import contextlib
from dataclasses import FrozenInstanceError
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import psycopg2

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hiring_services import (
    BUILTIN_SERVICES, DuplicateHiringService, HiringService, create_hiring_service,
    list_hiring_services, matches_service_title, validate_service_payload,
)
from discovery_runs import LocalDiscoveryRuns
from test_discovery_runs import ImmediateThread
from support import QA_USER_ID, signed_in_client


PAYLOAD = {
    "name": "Bookkeeping",
    "roles": ["Bookkeeper", "Accounts payable clerk"],
    "title_phrases": ["bookkeeper", "accounts payable"],
}
SERVICE = HiringService("custom_" + "a" * 32, "Bookkeeping",
                        ("Bookkeeper", "Accounts payable clerk"), ("bookkeeper", "accounts payable"))


class HiringServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True), patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live DB forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.api = importlib.import_module("control_gate_api")
            cls.jobs = importlib.import_module("discovery_jobs")

    def setUp(self):
        self.database_guard = patch("psycopg2.connect", side_effect=AssertionError("Live DB forbidden")).start()
        self.network_guard = patch("requests.get", side_effect=AssertionError("Live providers forbidden")).start()
        self.log = patch.object(self.api, "emit_ui_log", Mock()).start()
        empty_linkedin = Mock()
        empty_linkedin.json.return_value = []
        patch.object(self.jobs, "fetch_linkedin_jobs", return_value=empty_linkedin).start()
        self.addCleanup(patch.stopall)

    def test_validation_bounds_normalization_and_duplicates(self):
        name, roles, phrases = validate_service_payload({
            "name": "  Bookkeeping  Services  ", "roles": ["Bookkeeper", " bookkeeper "],
            "title_phrases": ["Accounts-Payable", "accounts payable"],
        })
        self.assertEqual(name, "Bookkeeping Services")
        self.assertEqual(roles, ("Bookkeeper",))
        self.assertEqual(phrases, ("Accounts-Payable",))
        for invalid in [
            None, [], {}, dict(PAYLOAD, name="ab"), dict(PAYLOAD, name="x" * 81),
            dict(PAYLOAD, name="Video Editing"), dict(PAYLOAD, name=" VIDEO   EDITING "),
            dict(PAYLOAD, name="Bad\x00Name"), dict(PAYLOAD, roles="Bookkeeper"),
            dict(PAYLOAD, roles=[]), dict(PAYLOAD, roles=["Role"] * 5),
            dict(PAYLOAD, roles=[None]), dict(PAYLOAD, roles=["x" * 101]),
            dict(PAYLOAD, title_phrases=[]), dict(PAYLOAD, title_phrases=["match"] * 13),
            dict(PAYLOAD, title_phrases=["***"]),
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_service_payload(invalid)

    def test_custom_matching_is_literal_title_only_and_presets_unchanged(self):
        self.assertTrue(matches_service_title("Senior Bookkeeper - Remote", SERVICE))
        self.assertTrue(matches_service_title("Accounts-Payable Clerk", SERVICE))
        self.assertFalse(matches_service_title("Truck Driver", SERVICE))
        self.assertFalse(matches_service_title("Bookkeeping Sales", SERVICE))
        regex_service = HiringService("custom_" + "b" * 32, "Special", ("Specialist",), ("data.*entry",))
        self.assertFalse(matches_service_title("Data Scientist Entry", regex_service))
        self.assertTrue(matches_service_title("Data Entry Clerk", regex_service))
        self.assertEqual(len(BUILTIN_SERVICES), 5)
        self.assertTrue(all(service.builtin and len(service.roles) == 4 for service in BUILTIN_SERVICES.values()))
        with self.assertRaises(FrozenInstanceError):
            SERVICE.name = "Changed"

    def test_create_list_roundtrip_uses_database_not_process_cache(self):
        database = MagicMock()
        connection = database.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        with patch("psycopg2.connect", return_value=database):
            created = create_hiring_service("qa-only", PAYLOAD, QA_USER_ID)
            statement, args = cursor.execute.call_args.args
            self.assertIn("INSERT INTO hiring_services", statement)
            self.assertEqual(args[:3], (created.id, "Bookkeeping", "bookkeeping"))
            self.assertEqual(args[3].adapted, SERVICE.roles)
            self.assertEqual(args[4].adapted, SERVICE.title_phrases)
            self.assertEqual(len(created.id), 39)
            cursor.fetchall.return_value = [{
                "service_id": created.id, "name": args[1],
                "roles": list(args[3].adapted), "title_phrases": list(args[4].adapted),
            }]
            listed = list_hiring_services("qa-only", QA_USER_ID)
            self.assertEqual(listed[-1], created)
            cursor.fetchall.return_value = []
            self.assertEqual(list_hiring_services("qa-only", QA_USER_ID), list(BUILTIN_SERVICES.values()))
        connection.commit.assert_called_once()

    def test_duplicate_database_names_and_failed_writes_propagate(self):
        database = MagicMock()
        connection = database.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        for error, expected in [
            (psycopg2.errors.UniqueViolation("QA duplicate"), DuplicateHiringService),
            (psycopg2.OperationalError("QA unavailable"), psycopg2.OperationalError),
        ]:
            with self.subTest(error=error), patch("psycopg2.connect", return_value=database):
                cursor.execute.side_effect = error
                with self.assertRaises(expected):
                    create_hiring_service("qa-only", PAYLOAD, QA_USER_ID)
        connection.commit.assert_not_called()

    def test_corrupt_stored_definitions_fail_explicitly_instead_of_empty_success(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        for row in [
            {"service_id": "custom_invalid", **PAYLOAD},
            {"service_id": SERVICE.id, **dict(PAYLOAD, roles="invalid")},
            {"service_id": SERVICE.id, **dict(PAYLOAD, title_phrases=[])},
        ]:
            cursor.fetchall.return_value = [row]
            with self.subTest(row=row), patch("psycopg2.connect", return_value=database):
                with self.assertRaises(ValueError):
                    list_hiring_services("qa-only", QA_USER_ID)
                response = signed_in_client(self.api.app).get("/api/hiring-services")
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json["status"], "error")
                self.assertNotIn("services", response.json)

    def test_concurrent_duplicate_save_returns_conflict_and_does_not_commit(self):
        database = MagicMock()
        connection = database.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = psycopg2.errors.UniqueViolation("QA duplicate name_key")
        with patch("psycopg2.connect", return_value=database):
            response = signed_in_client(self.api.app).post("/api/hiring-services", json=PAYLOAD)
        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.json["message"])
        connection.commit.assert_not_called()

    def test_service_api_create_list_invalid_and_database_failures(self):
        client = signed_in_client(self.api.app)
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with patch("psycopg2.connect", return_value=database):
            created = client.post("/api/hiring-services", json=PAYLOAD)
            self.assertEqual(created.status_code, 201)
            service = created.json["service"]
            self.assertEqual(service["roles"], list(SERVICE.roles))
            cursor.fetchall.return_value = [{
                "service_id": service["id"], **PAYLOAD,
            }]
            listed = client.get("/api/hiring-services")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(len(listed.json["services"]), 6)
            self.assertEqual(listed.json["services"][-1], service)
        for invalid in [[], {}, dict(PAYLOAD, roles=[]), dict(PAYLOAD, name="Video Editing")]:
            response = client.post("/api/hiring-services", json=invalid)
            self.assertIn(response.status_code, (400, 409))
        self.database_guard.assert_not_called()
        for method in [client.get, client.post]:
            with patch("psycopg2.connect", side_effect=psycopg2.OperationalError("QA DB error")):
                response = method("/api/hiring-services", **({"json": PAYLOAD} if method == client.post else {}))
                self.assertEqual(response.status_code, 500)
                self.assertIn("Restart", response.json["message"])

    def test_custom_trigger_budget_snapshot_and_missing_definitions(self):
        client = signed_in_client(self.api.app)
        runs = Mock()
        runs.start.return_value = "qa-snapshot"
        definitions = [*BUILTIN_SERVICES.values(), SERVICE]
        with (
            patch.object(self.api, "HIRING_DISCOVERY_RUNS", runs),
            patch("hiring_services.list_hiring_services", return_value=definitions) as load,
        ):
            response = client.post("/api/trigger/discover-jobs", json={
                "service_types": ["video_editing", SERVICE.id, SERVICE.id], "pages_per_role": 3,
            })
            self.assertEqual(response.status_code, 202)
            self.assertNotIn("max_provider_credits", response.json)
            self.assertNotIn("max_job_requests", response.json)
            tasks = runs.start.call_args.args[0]
            self.assertEqual(len(tasks), 2)
            definitions.clear()
            with patch.object(self.jobs, "run_job_discovery") as worker:
                tasks[1][2]()
                worker.assert_called_once_with(SERVICE.id, "", pages_per_role=3, service_definition=SERVICE, user_id=QA_USER_ID)
            load.assert_called_once()
            runs.start.reset_mock()
            response = client.post("/api/trigger/discover-jobs", json={"service_types": [SERVICE.id]})
            self.assertEqual(response.status_code, 400)
            runs.start.assert_not_called()
        with (
            patch.object(self.api, "HIRING_DISCOVERY_RUNS", runs),
            patch("hiring_services.list_hiring_services", side_effect=RuntimeError("QA load failed")),
        ):
            self.assertEqual(client.post("/api/trigger/discover-jobs",
                                        json={"service_types": [SERVICE.id]}).status_code, 500)
            runs.start.assert_not_called()

    def test_custom_pipeline_filters_paginates_saves_and_reports_status(self):
        response = Mock()
        response.json.return_value = {"jobs_results": [
            {"company_name": "Right", "title": "Senior Bookkeeper", "location": "Canada"},
            {"company_name": "Wrong role", "title": "Truck Driver", "location": "Canada",
             "description": "Bookkeeper department"},
            {"company_name": "Wrong country", "title": "Bookkeeper", "location": "Australia"},
        ]}
        runs = LocalDiscoveryRuns("hiring")
        with (
            patch.object(self.jobs, "SERPAPI_KEY", "qa-only"),
            patch.object(self.api, "HIRING_DISCOVERY_RUNS", runs),
            patch("hiring_services.list_hiring_services", return_value=[*BUILTIN_SERVICES.values(), SERVICE]),
            patch("requests.get", return_value=response) as searches,
            patch.object(self.jobs, "enrich_company_contact", return_value={"website_url": None, "email": None}),
            patch.object(self.jobs, "save_job_leads") as save,
            patch("discovery_runs.threading.Thread", ImmediateThread),
        ):
            client = signed_in_client(self.api.app)
            started = client.post("/api/trigger/discover-jobs", json={
                "service_types": [SERVICE.id], "country": "Canada", "pages_per_role": 2,
            })
            run = client.get(f"/api/discovery/job-runs/{started.json['run_id']}").json["run"]
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(searches.call_count, 2)
        self.assertEqual(searches.call_args_list[0].kwargs["params"]["q"], "Bookkeeper Canada")
        self.assertEqual(searches.call_args_list[1].kwargs["params"]["q"], "Accounts payable clerk Canada")
        leads = save.call_args.args[0]
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["lead_type"], SERVICE.id)
        self.assertEqual(leads[0]["company_name"], "Right")
        with self.assertRaisesRegex(ValueError, "Unsupported hiring service"), patch.object(self.jobs, "SERPAPI_KEY", "qa-only"):
            self.jobs.run_job_discovery(SERVICE.id, user_id=QA_USER_ID)

    def test_migration_adds_only_new_table_and_has_no_historical_mutation(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with (
            patch.object(self.api, "DB_DSN", "qa-only"),
            patch("psycopg2.connect", return_value=database),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.api.run_startup_migrations()
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        create = next(sql for sql in statements if "CREATE TABLE IF NOT EXISTS hiring_services" in sql)
        schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()
        self.assertIn(" ".join(create.split()), " ".join(schema.split()))
        self.assertIn("name_key TEXT NOT NULL", create)
        # Service names are unique per account, not globally.
        self.assertNotIn("name_key TEXT NOT NULL UNIQUE", create)
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hiring_services_user_name ON hiring_services (user_id, name_key);",
            statements,
        )
        self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for sql in statements))

    def test_custom_services_cannot_silently_use_web_design_pitch(self):
        from niche_prompts import get_service_parameters
        with self.assertRaisesRegex(ValueError, "discovery-only"):
            get_service_parameters(SERVICE.id)
        self.assertIn("pain_points", get_service_parameters("video_editing"))
