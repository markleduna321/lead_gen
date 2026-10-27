import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from discovery_profiles import canonical_social_profile
from support import QA_USER_ID
import discovery_linkedin


def response_with(results):
    response = Mock()
    response.json.return_value = {"organic_results": results}
    return response


class LinkedInDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = type(sys)("control_gate_api")
        self.api.emit_ui_log = Mock()
        self.api.save = None
        self.modules = patch.dict(sys.modules, {"control_gate_api": self.api})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_searches_public_company_pages_and_saves_only_valid_unique_industry_matches(self):
        search_results = response_with([
            {"title": "Acme Dental | LinkedIn",
             "link": "https://www.linkedin.com/company/acme-dental/?trk=search",
             "snippet": "Acme Dental Clinic provides family dental care."},
            {"title": "Acme Dental - LinkedIn",
             "link": "https://linkedin.com/company/acme-dental",
             "snippet": "Dental Clinic providing care."},
            {"title": "Other Company | LinkedIn",
             "link": "https://linkedin.com/company/other",
             "snippet": "A software company."},
            {"title": "A person | LinkedIn",
             "link": "https://linkedin.com/in/person",
             "snippet": "Dental Clinic."},
            {"title": "Fake company | LinkedIn",
             "link": "https://linkedin.com.evil.test/company/fake",
             "snippet": "Dental Clinic."},
            {"title": 42, "link": "https://linkedin.com/company/malformed",
             "snippet": "Dental Clinic"},
        ])
        saved = Mock()
        with (
            patch.object(discovery_linkedin, "SCRAPINGDOG_KEY", "qa-key"),
            patch.object(discovery_linkedin, "scrapingdog_fetch", return_value=search_results) as fetch,
            patch.object(discovery_linkedin, "load_existing_social_ids", return_value=set()) as known,
            patch.object(discovery_linkedin, "save_linkedin_leads", saved),
        ):
            count = discovery_linkedin.run_linkedin_discovery("Austin", "Dental Clinic", QA_USER_ID)

        self.assertEqual(count, 1)
        fetch.assert_called_once()
        params = fetch.call_args.args[0]
        self.assertIn('site:linkedin.com/company/ "Dental Clinic" "Austin"', params["query"])
        self.assertEqual(params["location"], "Austin")
        self.assertEqual(params["page"], 0)
        self.assertNotIn("engine", params)
        self.assertNotIn("profile", str(params).casefold())
        known.assert_called_once_with(discovery_linkedin.DB_DSN, "linkedin", QA_USER_ID)
        rows = saved.call_args.args[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Acme Dental")
        self.assertEqual(rows[0][4], canonical_social_profile(
            "linkedin", "https://linkedin.com/company/acme-dental"
        )[0])
        self.assertEqual(rows[0][7], "https://www.linkedin.com/company/acme-dental")
        self.assertEqual(rows[0][8:10], ("linkedin", "discovered"))
        self.assertEqual(saved.call_args.args[1], QA_USER_ID)

    def test_known_company_page_is_skipped_before_save(self):
        page_id = canonical_social_profile("linkedin", "https://linkedin.com/company/acme")[0]
        results = response_with([{
            "title": "Acme Dental | LinkedIn", "link": "https://linkedin.com/company/acme",
            "snippet": "Dental Clinic",
        }])
        with (
            patch.object(discovery_linkedin, "SCRAPINGDOG_KEY", "qa-key"),
            patch.object(discovery_linkedin, "scrapingdog_fetch", return_value=results),
            patch.object(discovery_linkedin, "load_existing_social_ids", return_value={page_id}),
            patch.object(discovery_linkedin, "save_linkedin_leads") as save,
        ):
            self.assertEqual(
                discovery_linkedin.run_linkedin_discovery("Austin", "Dental Clinic", QA_USER_ID), 0
            )
        save.assert_called_once_with([], QA_USER_ID)

    def test_empty_results_return_zero_without_saving(self):
        with (
            patch.object(discovery_linkedin, "SCRAPINGDOG_KEY", "qa-key"),
            patch.object(discovery_linkedin, "scrapingdog_fetch", return_value=response_with([])),
            patch.object(discovery_linkedin, "load_existing_social_ids") as known,
            patch.object(discovery_linkedin, "save_linkedin_leads") as save,
        ):
            self.assertEqual(
                discovery_linkedin.run_linkedin_discovery("Austin", "Dental Clinic", QA_USER_ID), 0
            )
        known.assert_not_called()
        save.assert_not_called()

    def test_provider_errors_and_missing_key_are_explicit(self):
        with patch.object(discovery_linkedin, "SCRAPINGDOG_KEY", None):
            with self.assertRaisesRegex(RuntimeError, "requires SCRAPINGDOG_API_KEY"):
                discovery_linkedin.run_linkedin_discovery("Austin", "Dental Clinic", QA_USER_ID)

        result = Mock()
        result.json.return_value = {"error": "QA quota exhausted"}
        with (
            patch.object(discovery_linkedin, "SCRAPINGDOG_KEY", "qa-key"),
            patch.object(discovery_linkedin, "scrapingdog_fetch", return_value=result),
            self.assertRaisesRegex(RuntimeError, "QA quota exhausted"),
        ):
            discovery_linkedin.run_linkedin_discovery("Austin", "Dental Clinic", QA_USER_ID)

    def test_saver_uses_account_scoped_unique_linkedin_ids(self):
        database = MagicMock()
        rows = [("Acme", "Dental Clinic", None, "Austin", "ln_serp_1", None, None,
                 "https://www.linkedin.com/company/acme", "linkedin", "discovered")]
        with (
            patch("psycopg2.connect", return_value=database),
            patch.object(discovery_linkedin, "execute_values") as execute,
        ):
            discovery_linkedin.save_linkedin_leads(rows, QA_USER_ID)
        query, inserted = execute.call_args.args[1:]
        self.assertIn("ON CONFLICT (user_id, google_place_id)", query)
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0][-1], QA_USER_ID)
        self.assertEqual(len(inserted[0]), len(rows[0]) + 1)


if __name__ == "__main__":
    unittest.main()
