import re
import sys
import types
import unittest
import uuid
from unittest.mock import MagicMock, Mock, patch

from test_discovery_filters import load_function
from discovery_filters import filter_business_metrics
from discovery_profiles import canonical_social_profile
from support import QA_USER_ID


class DiscoveryPersistenceTests(unittest.TestCase):
    def test_worker_to_insert_preserves_contact_and_source_fields(self):
        cases = [
            ("discovery_engine.py", "run_discovery", "save_leads_to_db", "local_results", "google_maps"),
            ("discovery_facebook.py", "run_facebook_discovery", "save_facebook_leads", "organic_results", "facebook"),
            ("discovery_instagram.py", "run_instagram_discovery", "save_instagram_leads", "organic_results", "instagram"),
        ]
        for filename, run_name, save_name, results_key, source in cases:
            for snippet in ["Dental Clinic Contact QA@Example.test", "Dental Clinic No email in snippet"]:
                with self.subTest(source=source, snippet=snippet):
                    profile = f"https://{source}.com/qa"
                    business = {
                        "title": "QA Business", "link": profile, "snippet": snippet,
                        "website": "https://example.test", "address": "Austin",
                        "data_id": "qa-google-id", "rating": 4.5, "reviews": 25,
                        "type": "Dentist",
                    }
                    response = Mock()
                    response.json.return_value = {results_key: [business]}
                    database = MagicMock()
                    database.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = []
                    execute = Mock()
                    crawl = Mock(return_value="qa@example.test")
                    api = types.ModuleType("control_gate_api")
                    api.emit_ui_log = Mock()
                    hunter = types.ModuleType("email_hunter")
                    hunter.attempt_domain_email_crawl = crawl
                    namespace = {
                        "SERPAPI_KEY": "qa-only", "DB_DSN": "qa-only",
                        "requests": types.SimpleNamespace(get=Mock(return_value=response)),
                        "psycopg2": database, "uuid": uuid, "execute_values": execute,
                        "filter_business_metrics": filter_business_metrics,
                        "attempt_domain_email_crawl": crawl,
                        "capture_business_site_snapshot": Mock(return_value="/qa.png"),
                        "canonical_social_profile": canonical_social_profile,
                        "load_existing_social_ids": Mock(return_value=set()),
                    }
                    load_function(filename, save_name, namespace)
                    worker = load_function(filename, run_name, namespace)
                    with patch.dict(sys.modules, {"control_gate_api": api, "email_hunter": hunter}):
                        worker("Austin", "Dental Clinic", user_id=QA_USER_ID)
                    execute.assert_called_once()
                    sql, rows = execute.call_args.args[1:]
                    columns = [value.strip() for value in re.search(r"INSERT INTO prospects\s*\((.*?)\)", sql, re.S)[1].split(",")]
                    self.assertEqual(len(columns), len(rows[0]))
                    stored = dict(zip(columns, rows[0]))
                    self.assertEqual(stored["source_platform"], source)
                    self.assertEqual(stored["status"], "discovered")
                    if source == "google_maps":
                        self.assertEqual(stored["website_url"], "https://example.test")
                        self.assertEqual(stored["email"], "qa@example.test")
                    else:
                        self.assertEqual(stored["profile_url"], profile)
                        self.assertNotIn("website_url", stored)
                        self.assertIsNone(stored["rating"])
                        self.assertIsNone(stored["review_count"])
                        if source == "instagram":
                            self.assertEqual(stored["email"], "qa@example.test")
                            self.assertEqual(crawl.call_count, int("No email" in snippet))
                        else:
                            crawl.assert_not_called()
