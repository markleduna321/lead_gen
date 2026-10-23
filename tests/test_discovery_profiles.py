import sys
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

from test_discovery_filters import load_function
from discovery_profiles import canonical_social_profile, load_existing_social_ids
from support import QA_USER_ID


class DiscoveryProfileTests(unittest.TestCase):
    def test_profile_variants_share_stable_identities(self):
        for source, variants in [
            ("facebook", ["https://www.facebook.com/QA.Business/", "http://m.facebook.com/qa.business?ref=search"]),
            ("instagram", ["https://www.instagram.com/QA.Business/", "http://instagram.com/qa.business?igsh=tracking#bio"]),
            ("facebook", ["https://facebook.com/profile.php?id=123&ref=search", "https://m.facebook.com/people/QA/123/"]),
        ]:
            with self.subTest(source=source):
                first = canonical_social_profile(source, variants[0])
                self.assertEqual(first, canonical_social_profile(source, variants[1]))
                self.assertNotEqual(first[0], canonical_social_profile(source, f"https://{source}.com/another.business")[0])
        self.assertNotEqual(
            canonical_social_profile("facebook", "https://facebook.com/profile.php?id=123")[0],
            canonical_social_profile("facebook", "https://facebook.com/profile.php?id=456")[0],
        )

    def test_nonprofiles_and_wrong_hosts_are_rejected(self):
        for source, url in [
            ("instagram", "https://instagram.com/p/post123"),
            ("instagram", "https://instagram.com/explore"),
            ("facebook", "https://facebook.com/groups/123"),
            ("facebook", "https://facebook.com/profile.php?ref=123"),
            ("facebook", "https://facebook.com.evil.test/qa"),
            ("facebook", "javascript:alert(1)"),
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                canonical_social_profile(source, url)

    def test_existing_random_ids_can_be_matched_by_saved_profile_url(self):
        database = MagicMock()
        database.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            ("ig_serp_oldrandom", "https://www.instagram.com/QA/"),
            ("fb_serp_old", "https://facebook.com/qa"), ("legacy-no-url", None),
        ]
        with patch("psycopg2.connect", return_value=database):
            identities = load_existing_social_ids("qa-only", "instagram", QA_USER_ID)
        self.assertIn("ig_serp_oldrandom", identities)
        self.assertIn(canonical_social_profile("instagram", "https://instagram.com/qa")[0], identities)
        self.assertIn("legacy-no-url", identities)

    def test_repeated_profiles_skip_before_enrichment_and_do_not_insert_twice(self):
        for source, filename, worker_name, save_name in [
            ("facebook", "discovery_facebook.py", "run_facebook_discovery", "save_facebook_leads"),
            ("instagram", "discovery_instagram.py", "run_instagram_discovery", "save_instagram_leads"),
        ]:
            with self.subTest(source=source):
                records = [
                    {"title": "QA", "link": f"https://www.{source}.com/QA/", "snippet": "Dental Clinic and Cafe"},
                    {"title": "QA", "link": f"https://{source}.com/qa?ref=tracking", "snippet": "Dental Clinic and Cafe"},
                ]
                response = Mock()
                response.json.return_value = {"organic_results": records}
                saved_rows = []
                known_ids = set()
                def save(rows, user_id=None):
                    saved_rows.extend(rows)
                    known_ids.update(row[4] for row in rows)
                crawl = Mock(return_value="qa@example.test")
                namespace = {
                    "SERPAPI_KEY": "qa-only", "DB_DSN": "qa-only",
                    "requests": types.SimpleNamespace(get=Mock(return_value=response)),
                    "psycopg2": types.SimpleNamespace(Error=RuntimeError),
                    "canonical_social_profile": canonical_social_profile,
                    "load_existing_social_ids": Mock(side_effect=lambda *args: set(known_ids)),
                    save_name: save,
                }
                api = types.ModuleType("control_gate_api")
                api.emit_ui_log = Mock()
                hunter = types.ModuleType("email_hunter")
                hunter.attempt_domain_email_crawl = crawl
                worker = load_function(filename, worker_name, namespace)
                with patch.dict(sys.modules, {"control_gate_api": api, "email_hunter": hunter}):
                    worker("Austin", "Dental Clinic")
                    worker("Austin", "Cafe")
                self.assertEqual(len(saved_rows), 1)
                self.assertEqual(crawl.call_count, int(source == "instagram"))
                self.assertIn("already discovered", api.emit_ui_log.call_args.args[0])
