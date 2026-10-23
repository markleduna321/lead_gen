from html.parser import HTMLParser
from pathlib import Path
import types
import unittest
import uuid
from unittest.mock import MagicMock, Mock, patch

from test_discovery_filters import load_function
from discovery_filters import filter_business_metrics
from discovery_industries import INDUSTRY_ALIASES, has_industry_evidence, matching_industry_category


class IndustryInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.industries = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "biz-type":
            self.industries.add(attributes["value"])


class DiscoveryIndustryTests(unittest.TestCase):
    def test_every_rendered_preset_has_matching_aliases(self):
        template = Path(__file__).resolve().parents[1] / "src" / "templates" / "dashboard.html"
        parser = IndustryInputs()
        parser.feed(template.read_text(encoding="utf-8").split("<script")[0])
        self.assertEqual(len(parser.industries), 32)
        self.assertEqual(parser.industries, set(INDUSTRY_ALIASES))
        for industry, aliases in INDUSTRY_ALIASES.items():
            for alias in aliases:
                with self.subTest(industry=industry, alias=alias):
                    self.assertTrue(has_industry_evidence(industry, f"Local {alias} business"))
        self.assertTrue(has_industry_evidence("Dental Clinic", "Dentists"))
        self.assertTrue(has_industry_evidence("Cafe", "Café"))
        self.assertFalse(has_industry_evidence("Spa", "Space equipment"))

    def test_provider_categories_not_business_title_determine_category(self):
        log = Mock()
        self.assertIsNone(matching_industry_category(
            {"title": "Dental Clinic", "type": "Plumber"}, "Dental Clinic", "google_maps", log
        ))
        self.assertIsNone(matching_industry_category(
            {"title": "Dental Clinic"}, "Dental Clinic", "yelp", log
        ))
        self.assertEqual(matching_industry_category(
            {"categories": [{"title": "Plumbing"}, {"title": "Cosmetic Dentists"}]},
            "Dental Clinic", "yelp", log
        ), "Cosmetic Dentists")
        self.assertEqual(matching_industry_category(
            {"types": ["Orthodontist", "Dentist"]}, "Dental Clinic", "google_maps", log
        ), "Dentist")

    def test_social_text_and_custom_industry_matching(self):
        log = Mock()
        for source in ["facebook", "instagram"]:
            self.assertEqual(matching_industry_category(
                {"title": "QA", "snippet": "Local <b>dentist</b> in Austin"},
                "Dental Clinic", source, log
            ), "Dental Clinic")
            self.assertIsNone(matching_industry_category(
                {"title": "QA", "snippet": "Plumbing services"}, "Dental Clinic", source, log
            ))
        self.assertTrue(has_industry_evidence("Tattoo Shop", "Independent TATTOO-SHOP"))
        self.assertFalse(has_industry_evidence("Tattoo Shop", "Shoe shop"))

    def test_workers_skip_mismatches_before_enrichment_and_save_actual_category(self):
        for filename, worker_name, save_name, results_key in [
            ("discovery_engine.py", "run_discovery", "save_leads_to_db", "local_results"),
            ("discovery_yelp.py", "run_yelp_discovery", "save_yelp_leads", "organic_results"),
        ]:
            with self.subTest(worker=worker_name):
                response = Mock()
                response.json.return_value = {results_key: [
                    {"title": "QA", "type": category, "data_id": str(index),
                     "rating": 5, "reviews": 1, "website": "https://example.test"}
                    for index, category in enumerate(["Plumber", None, "Cosmetic Dentist"])
                ]}
                database = MagicMock()
                database.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = []
                crawl, snapshot, saved = Mock(return_value="qa@example.test"), Mock(), Mock()
                namespace = {
                    "SERPAPI_KEY": "qa-only", "DB_DSN": "qa-only", "uuid": uuid,
                    "requests": types.SimpleNamespace(get=Mock(return_value=response)), "psycopg2": database,
                    "filter_business_metrics": filter_business_metrics,
                    "attempt_domain_email_crawl": crawl, "capture_business_site_snapshot": snapshot,
                    save_name: saved,
                }
                modules = {}
                for name, symbol, value in [
                    ("control_gate_api", "emit_ui_log", Mock()),
                    ("email_hunter", "attempt_domain_email_crawl", crawl),
                    ("screenshot_agent", "capture_business_site_snapshot", snapshot),
                ]:
                    module = types.ModuleType(name)
                    setattr(module, symbol, value)
                    modules[name] = module
                worker = load_function(filename, worker_name, namespace)
                with patch.dict("sys.modules", modules):
                    worker("Austin", "Dental Clinic")
                crawl.assert_called_once()
                snapshot.assert_called_once()
                rows = saved.call_args.args[0]
                self.assertEqual(len(rows), 1)
                category = rows[0]["category"] if results_key == "local_results" else rows[0][1]
                self.assertEqual(category, "Cosmetic Dentist")
