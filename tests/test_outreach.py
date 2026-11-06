import contextlib
from datetime import datetime, timedelta, timezone
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
from psycopg2.extensions import make_dsn
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import outreach
from support import signed_in_client


class OutreachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("No live database")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.api = importlib.import_module("control_gate_api")

    def test_validation_rejects_bad_status_note_and_expected_status(self):
        for data in (None, [], {"status": []}, {"status": "sent"},
                     {"status": "replied"}, {"status": "replied", "expected_status": []},
                     {"status": "failed", "expected_status": "replied", "note": 12},
                     {"status": "failed", "expected_status": "replied", "note": "x" * 2001}):
            with self.subTest(data=str(data)[:80]), self.assertRaises(ValueError):
                outreach.validate_update(data)
        self.assertEqual(outreach.validate_update({
            "status": "replied", "expected_status": "awaiting_reply", "note": " Context "
        }), ("replied", "awaiting_reply", "Context"))

    def test_missing_and_concurrent_changes_never_write_history(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        self.assertEqual(outreach.record_update(cur, "jobs", 9, 1, "failed"), "missing")
        self.assertEqual(cur.execute.call_count, 1)
        cur.reset_mock()
        cur.fetchone.return_value = {"outreach_status": "successful"}
        self.assertEqual(outreach.record_update(cur, "jobs", 9, 1, "failed",
                                               expected="replied"), "conflict")
        self.assertEqual(cur.execute.call_count, 1)

    def test_notes_and_corrections_are_owner_scoped_and_stop_followups(self):
        for kind in outreach.TABLES:
            cur = MagicMock()
            cur.fetchone.return_value = {"outreach_status": "awaiting_reply"}
            result = outreach.record_update(cur, kind, 42, 1, "replied", "<script>note</script>",
                                            expected="awaiting_reply")
            self.assertEqual(result, "updated")
            sql = "\n".join(call.args[0] for call in cur.execute.call_args_list)
            self.assertIn("FOR UPDATE", sql)
            self.assertIn("user_id = %s", sql)
            self.assertIn("INSERT INTO outreach_events", sql)
            self.assertEqual(cur.execute.call_args.args[1][-3:], ("<script>note</script>", "manual", "manual"))
            self.assertEqual("followup_due_at = NULL" in sql, kind == "prospects")

    def test_call_validation_covers_outcome_phone_times_and_callbacks(self):
        now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
        base = {"outcome": "no_answer", "expected_status": "not_contacted"}
        for extra in ({"outcome": "emailed"}, {"outcome": None}, {"expected_status": "sent"},
                      {"note": "x" * 2001}, {"phone": "call me"}, {"phone": "123"}, {"phone": "1" * 41},
                      {"phone": 5551234}, {"occurred_at": "2026-11-04T12:00"},
                      {"occurred_at": "2026-11-04T12:10:00Z"}, {"occurred_at": "2025-01-01T00:00:00Z"},
                      {"occurred_at": "yesterday"}, {"callback_at": "2026-11-05T09:00:00Z"},
                      {"outcome": "callback_requested", "callback_at": "2026-11-04T11:00:00Z"},
                      {"outcome": "callback_requested", "callback_at": "2028-01-01T00:00:00Z"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                outreach.validate_call({**base, **extra}, now=now)
        self.assertRaises(ValueError, outreach.validate_call, None, now)
        call = outreach.validate_call({
            "outcome": "callback_requested", "expected_status": "awaiting_reply", "note": " Ring Friday ",
            "phone": " +1 (555) 010-2000 ext 4 ", "occurred_at": "2026-11-04T11:30:00Z",
            "callback_at": "2026-11-06T09:00:00+02:00"}, now=now)
        self.assertEqual(call["note"], "Ring Friday")
        self.assertEqual(call["phone"], "+1 (555) 010-2000 ext 4")
        self.assertEqual(call["occurred_at"], datetime(2026, 11, 4, 11, 30, tzinfo=timezone.utc))
        self.assertEqual(call["callback_at"], datetime(2026, 11, 6, 7, tzinfo=timezone.utc))
        self.assertEqual(outreach.validate_call(base, now=now)["occurred_at"], now)

    def test_call_outcomes_only_move_status_forward(self):
        expected = {
            ("no_answer", "awaiting_reply"): "awaiting_reply",
            ("voicemail", "not_contacted"): "awaiting_reply",
            ("voicemail", "replied"): "replied",
            ("connected", "awaiting_reply"): "replied",
            ("connected", "successful"): "successful",
            ("callback_requested", "not_contacted"): "replied",
            ("interested", "failed"): "successful",
            ("not_interested", "replied"): "failed",
            ("wrong_number", "not_contacted"): "not_contacted",
        }
        for (outcome, previous), status in expected.items():
            with self.subTest(outcome=outcome, previous=previous):
                cur = MagicMock()
                cur.fetchone.return_value = {"outreach_status": previous, "phone": "555 0100"}
                call = {"outcome": outcome, "expected": previous, "note": "", "phone": "",
                        "occurred_at": datetime(2026, 11, 4, tzinfo=timezone.utc), "callback_at": None}
                self.assertEqual(outreach.record_call(cur, "prospects", 3, 1, call), "updated")
                update = cur.execute.call_args_list[1].args
                self.assertEqual(update[1][0], status)
                self.assertIn("user_id = %s", update[0])
                insert = cur.execute.call_args.args
                self.assertIn("'call', 'call'", insert[0])
                self.assertEqual(insert[1][2:5], (previous, status, ""))
                self.assertEqual(insert[1][-2], "555 0100")
        cur = MagicMock()
        cur.fetchone.return_value = {"outreach_status": "replied", "phone": None}
        self.assertEqual(outreach.record_call(cur, "jobs", 3, 1, {**call, "expected": "awaiting_reply"}),
                         "conflict")
        self.assertEqual(cur.execute.call_count, 1)

    def test_call_routes_validate_before_opening_database(self):
        client = signed_in_client(self.api.app)
        with patch("psycopg2.connect", side_effect=AssertionError("No database expected")):
            self.assertEqual(client.post("/api/outreach/unknown/1/calls", json={}).status_code, 404)
            response = client.post("/api/outreach/jobs/1/calls",
                                   json={"outcome": "texted", "expected_status": "not_contacted"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["message"], "Choose how the call went.")
            for path in ("/api/queue", "/api/job-leads"):
                self.assertEqual(client.get(path + "?contact_activity=invalid").status_code, 400)

    def test_routes_validate_before_opening_database(self):
        client = signed_in_client(self.api.app)
        with patch("psycopg2.connect", side_effect=AssertionError("No database expected")):
            self.assertEqual(client.post("/api/outreach/unknown/1", json={}).status_code, 404)
            self.assertEqual(client.post("/api/outreach/jobs/1", json={"status": "sent"}).status_code, 400)
            for path in ("/api/queue", "/api/job-leads"):
                self.assertEqual(client.get(path + "?outreach_status=invalid").status_code, 400)

    def test_history_route_uses_ownership_and_iso_dates(self):
        client = signed_in_client(self.api.app)
        db = MagicMock()
        cur = db.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"outreach_status": "replied"}
        cur.fetchall.return_value = [{
            "id": 1, "status": "replied", "previous_status": "awaiting_reply",
            "note": "hello", "source": "manual", "created_at": datetime(2026, 11, 4, tzinfo=timezone.utc)
        }]
        with patch("psycopg2.connect", return_value=db):
            response = client.get("/api/outreach/jobs/42")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["history"][0]["created_at"], "2026-11-04T00:00:00+00:00")
        for call in cur.execute.call_args_list:
            self.assertIn("user_id = %s", call.args[0])
            self.assertEqual(call.args[1], (42, 1))

    def test_missing_lead_and_conflict_have_explicit_errors(self):
        client = signed_in_client(self.api.app)
        for state, expected in ((None, 404), ({"outreach_status": "successful"}, 409)):
            db = MagicMock()
            cur = db.__enter__.return_value.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = state
            with patch("psycopg2.connect", return_value=db):
                response = client.post("/api/outreach/prospects/42", json={
                    "status": "failed", "expected_status": "replied", "note": "reason"})
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.json["status"], "error")


@unittest.skipUnless(os.getenv("TEST_BILLING_DSN"), "Requires disposable PostgreSQL")
class OutreachDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "outreach_test_" + uuid.uuid4().hex
        cls.base_dsn = os.environ["TEST_BILLING_DSN"]
        with psycopg2.connect(cls.base_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA {cls.schema}")
        cls.dsn = make_dsn(cls.base_dsn, options=f"-c search_path={cls.schema}")
        with psycopg2.connect(cls.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute((Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8"))
                cur.execute("""
                    INSERT INTO users (id, email, email_key, password_hash) VALUES
                        (1, 'one@example.test', 'one@example.test', 'unused'),
                        (2, 'two@example.test', 'two@example.test', 'unused');
                    INSERT INTO prospects (user_id, business_name, category, google_place_id, status) VALUES
                        (1, 'Example business', 'Cafe', 'qa-one', 'sent'),
                        (2, 'Private business', 'Cafe', 'qa-two', 'discovered');
                    INSERT INTO job_leads (user_id, company_name, job_title, lead_type, status) VALUES
                        (1, 'Example company', 'Designer', 'web_design', 'discovered'),
                        (2, 'Private company', 'Designer', 'web_design', 'sent');
                """)
                cur.execute(outreach.SCHEMA)

    @classmethod
    def tearDownClass(cls):
        with psycopg2.connect(cls.base_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA {cls.schema} CASCADE")

    def setUp(self):
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM outreach_events")
                cur.execute("UPDATE prospects SET outreach_status = CASE WHEN id = 1 "
                            "THEN 'awaiting_reply' ELSE 'not_contacted' END")
                cur.execute("UPDATE job_leads SET outreach_status = CASE WHEN id = 1 "
                            "THEN 'not_contacted' ELSE 'awaiting_reply' END")
                for table in outreach.TABLES.values():
                    cur.execute(f"UPDATE {table} SET call_attempts = 0, last_called_at = NULL, "
                                "callback_at = NULL, call_phone = NULL, phone_invalid = FALSE")
                cur.execute("UPDATE prospects SET phone = '+1 555 0100'")
                cur.execute("""UPDATE job_leads SET contact_evidence =
                    '{"phones": [{"phone": "+1 555 0200", "source_url": "https://x.test"}]}'::jsonb""")

    def call_client(self):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            api = importlib.import_module("control_gate_api")
        return api, signed_in_client(api.app)

    def test_call_log_workflow_persists_counts_callbacks_and_numbers(self):
        api, client = self.call_client()
        with patch.object(api, "DB_DSN", self.dsn):
            for kind, listed, source in (("prospects", "+1 555 0100", "listed"),
                                         ("jobs", "+1 555 0200", "listed_unverified")):
                url = f"/api/outreach/{kind}/1"
                start = client.get(url).json
                self.assertEqual(start["calls"], {"call_attempts": 0, "last_called_at": None, "callback_at": None,
                                                  "phone_invalid": False, "phone": listed, "phone_source": source})
                status = start["outreach_status"]

                def log(outcome, **extra):
                    response = client.post(url + "/calls", json={
                        "outcome": outcome, "expected_status": status, **extra})
                    self.assertEqual(response.status_code, 200, response.json)
                    return response.json

                data = log("no_answer", occurred_at="2026-01-02T10:00:00Z")
                self.assertEqual(data["outreach_status"], status)
                self.assertEqual(data["calls"]["call_attempts"], 1)
                self.assertEqual(data["history"][0]["channel"], "call")
                self.assertEqual(data["history"][0]["phone"], listed)
                first_call = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
                self.assertEqual(datetime.fromisoformat(data["history"][0]["occurred_at"]), first_call)
                data = log("wrong_number", note="Disconnected")
                self.assertTrue(data["calls"]["phone_invalid"])
                data = log("no_answer")
                self.assertTrue(data["calls"]["phone_invalid"], "No answer must not clear the flag")
                data = log("callback_requested", phone="+1 555 0999", note="Owner asked for Friday")
                status = data["outreach_status"]
                self.assertEqual(status, "replied")
                self.assertEqual(data["calls"]["phone"], "+1 555 0999")
                self.assertEqual(data["calls"]["phone_source"], "manual")
                self.assertFalse(data["calls"]["phone_invalid"])
                self.assertEqual(data["calls"]["call_attempts"], 4)
                self.assertGreater(datetime.fromisoformat(data["calls"]["last_called_at"]), first_call)
                with psycopg2.connect(self.dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute(f"UPDATE {outreach.TABLES[kind]} SET callback_at = NOW() - INTERVAL '1 hour' "
                                    "WHERE id = 1")
                queue_url = "/api/queue" if kind == "prospects" else "/api/job-leads"
                queue = client.get(queue_url + "?contact_activity=callback_due")
                self.assertEqual(queue.status_code, 200, queue.json)
                self.assertEqual([lead["id"] for lead in queue.json["leads"]], [1])
                self.assertEqual(queue.json["leads"][0]["call_attempts"], 4)
                self.assertEqual({o["value"]: o["count"] for o in queue.json["filter_options"]["contact"]},
                                 {"called": 1, "callback_due": 1})
                self.assertEqual(client.get(queue_url + "?contact_activity=invalid_number").json["leads"], [])
                self.assertEqual(client.get(queue_url + "?contact_activity=not_called").json["leads"], [])
                stale = client.post(url + "/calls", json={"outcome": "connected", "expected_status": "not_contacted"})
                self.assertEqual(stale.status_code, 409)
                data = log("interested", note="Booked a meeting")
                self.assertEqual(data["outreach_status"], "successful")
                self.assertIsNone(data["calls"]["callback_at"])
                self.assertEqual(len(data["history"]), 5)
                self.assertEqual(data["history"][0]["call_outcome"], "interested")
                self.assertEqual(client.post(f"/api/outreach/{kind}/2/calls", json={
                    "outcome": "connected", "expected_status": "not_contacted"}).status_code, 404)
                self.assertEqual(client.post(f"/api/outreach/{kind}/2/calls", json={
                    "outcome": "connected", "expected_status": "awaiting_reply"}).status_code, 404)
            with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT call_attempts FROM prospects WHERE id = 2 UNION ALL "
                                "SELECT call_attempts FROM job_leads WHERE id = 2")
                    self.assertEqual([row["call_attempts"] for row in cur.fetchall()], [0, 0])
                    cur.execute("SELECT COUNT(*) AS n FROM outreach_events")
                    before = cur.fetchone()["n"]
                    cur.execute(outreach.SCHEMA)
                    cur.execute("SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE channel = 'call') AS calls "
                                "FROM outreach_events")
                    self.assertEqual(dict(cur.fetchone()), {"n": before, "calls": before})

    def test_callback_time_and_answered_calls_stop_prospect_followups(self):
        api, client = self.call_client()
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE prospects SET followup_due_at = NOW() + INTERVAL '2 days' WHERE id = 1")
        with patch.object(api, "DB_DSN", self.dsn):
            voicemail = client.post("/api/outreach/prospects/1/calls", json={
                "outcome": "voicemail", "expected_status": "awaiting_reply"}).json
            self.assertEqual(voicemail["outreach_status"], "awaiting_reply")
            with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT followup_due_at FROM prospects WHERE id = 1")
                    self.assertIsNotNone(cur.fetchone()["followup_due_at"])
            callback = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
            data = client.post("/api/outreach/prospects/1/calls", json={
                "outcome": "callback_requested", "expected_status": "awaiting_reply",
                "callback_at": callback.isoformat()}).json
            self.assertEqual(datetime.fromisoformat(data["calls"]["callback_at"]), callback)
            self.assertEqual(datetime.fromisoformat(data["history"][0]["callback_at"]), callback)
            with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT followup_due_at, outreach_status FROM prospects WHERE id = 1")
                    row = cur.fetchone()
                    self.assertIsNone(row["followup_due_at"])
                    self.assertEqual(row["outreach_status"], "replied")
            self.assertEqual(client.get("/api/queue?contact_activity=callback_due").json["leads"], [])
            closed = client.post("/api/outreach/prospects/1", json={
                "status": "failed", "expected_status": "replied"}).json
            self.assertIsNone(closed["calls"]["callback_at"])
            self.assertEqual(closed["history"][0]["channel"], "manual")

    def test_status_history_reopen_privacy_backfill_and_migration_idempotence(self):
        with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT outreach_status FROM prospects WHERE id = 1")
                self.assertEqual(cur.fetchone()["outreach_status"], "awaiting_reply")
                for kind in outreach.TABLES:
                    self.assertEqual(outreach.record_update(cur, kind, 2, 1, "failed"), "missing")
                    for status in ("awaiting_reply", "replied", "successful", "failed", "not_contacted"):
                        self.assertEqual(outreach.record_update(cur, kind, 1, 1, status,
                                                               note="Test outcome"), "updated")
                cur.execute("SELECT COUNT(*) AS n FROM outreach_events WHERE user_id = 1")
                self.assertEqual(cur.fetchone()["n"], 10)
                cur.execute(outreach.SCHEMA)
                cur.execute("SELECT outreach_status FROM prospects WHERE id = 1")
                self.assertEqual(cur.fetchone()["outreach_status"], "not_contacted")
                cur.execute("SELECT COUNT(*) AS n FROM outreach_events")
                self.assertEqual(cur.fetchone()["n"], 10)

    def test_api_persists_notes_and_rejects_another_users_lead(self):
        with (
            patch.dict(os.environ, {"APP_ENV": "local"}, clear=True),
            patch("dotenv.load_dotenv"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            api = importlib.import_module("control_gate_api")
        client = signed_in_client(api.app)
        with patch.object(api, "DB_DSN", self.dsn):
            for kind in outreach.TABLES:
                data = client.get(f"/api/outreach/{kind}/1").json
                response = client.post(f"/api/outreach/{kind}/1", json={
                    "status": "replied", "expected_status": data["outreach_status"], "note": "Response arrived"
                })
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(response.json["outreach_status"], "replied")
                self.assertEqual(response.json["history"][0]["note"], "Response arrived")
                persisted = client.get(f"/api/outreach/{kind}/1").json
                self.assertEqual(persisted["history"][0]["note"], "Response arrived")
                stale = client.post(f"/api/outreach/{kind}/1", json={
                    "status": "failed", "expected_status": data["outreach_status"], "note": "Stale edit"
                })
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(len(client.get(f"/api/outreach/{kind}/1").json["history"]), 1)
                note_only = client.post(f"/api/outreach/{kind}/1", json={
                    "status": "replied", "expected_status": "replied", "note": "Meeting tomorrow"
                })
                self.assertEqual(note_only.status_code, 200)
                self.assertEqual(len(note_only.json["history"]), 2)
                self.assertEqual(note_only.json["history"][0]["note"], "Meeting tomorrow")
                self.assertEqual(client.get(f"/api/outreach/{kind}/2").status_code, 404)
                self.assertEqual(client.post(f"/api/outreach/{kind}/2", json={
                    "status": "failed", "expected_status": "not_contacted"}).status_code, 404)
                queue_url = "/api/queue" if kind == "prospects" else "/api/job-leads"
                queue = client.get(queue_url + "?outreach_status=replied")
                self.assertEqual(queue.status_code, 200, queue.json)
                self.assertEqual([lead["id"] for lead in queue.json["leads"]], [1])
                self.assertEqual(queue.json["filter_options"]["outreach"],
                                 [{"value": "replied", "label": "replied", "count": 1}])
                self.assertEqual(client.get(queue_url + "?outreach_status=failed").json["leads"], [])

    def test_followups_stop_after_reply_and_cannot_be_rescheduled(self):
        with patch("dotenv.load_dotenv"):
            scheduler = importlib.import_module("followup_scheduler")
        with patch.object(scheduler, "DB_DSN", self.dsn):
            scheduler.schedule_followup(1, "sent", 1)
            with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT followup_due_at FROM prospects WHERE id = 1")
                    self.assertIsNotNone(cur.fetchone()["followup_due_at"])
                    outreach.record_update(cur, "prospects", 1, 1, "replied")
            scheduler.schedule_followup(1, "sent", 1)
            with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT followup_due_at FROM prospects WHERE id = 1")
                    self.assertIsNone(cur.fetchone()["followup_due_at"])
                    cur.execute("UPDATE prospects SET followup_due_at = NOW() - INTERVAL '1 day' WHERE id = 1")
            self.assertEqual(scheduler.run_followup_sweep(1), 0)
            with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT status FROM prospects WHERE id = 1")
                    self.assertEqual(cur.fetchone()["status"], "sent")

    def test_in_app_email_records_history_only_after_successful_send(self):
        api = importlib.import_module("control_gate_api")
        import user_email
        import followup_scheduler
        client = signed_in_client(api.app)
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE prospects SET status = 'drafted', outreach_status = 'not_contacted', "
                            "followup_due_at = NULL WHERE id = 1")
        payload = {"final_pitch": "Hello", "destination_email": "lead@example.test"}
        with (
            patch.object(api, "DB_DSN", self.dsn),
            patch.object(api, "limiter", return_value=MagicMock()),
            patch.object(followup_scheduler, "DB_DSN", self.dsn),
            patch.object(user_email, "sender_identity", return_value={"name": "QA", "email": "qa@example.test"}),
            patch.object(user_email, "build_signature", return_value=""),
            patch.object(user_email, "send_email") as send,
        ):
            send.side_effect = user_email.EmailSettingsError("SMTP delivery failed")
            self.assertEqual(client.post("/api/dispatch/1", json=payload).status_code, 400)
            self.assertEqual(client.get("/api/outreach/prospects/1").json["history"], [])
            send.side_effect = None
            response = client.post("/api/dispatch/1", json=payload)
            self.assertEqual(response.status_code, 200, response.json)
            data = client.get("/api/outreach/prospects/1").json
            self.assertEqual(data["outreach_status"], "awaiting_reply")
            self.assertEqual(len(data["history"]), 1)
            self.assertEqual(data["history"][0]["source"], "email_sent")
            # Repeated Send on the sent pipeline schedules a follow-up without another email.
            self.assertEqual(client.post("/api/dispatch/1", json=payload).status_code, 200)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(len(client.get("/api/outreach/prospects/1").json["history"]), 1)


if __name__ == "__main__":
    unittest.main()
