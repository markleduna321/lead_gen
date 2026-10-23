import ast
import math
from pathlib import Path
import types
import unittest
import uuid
from unittest.mock import MagicMock, Mock, patch

from test_discovery_filters import load_function
from discovery_filters import filter_business_metrics, within_search_radius


class DiscoveryLocationTests(unittest.TestCase):
    def test_google_uses_pinned_coordinates_including_zero(self):
        response = Mock()
        response.json.return_value = {"local_results": []}
        get = Mock(return_value=response)
        namespace = {"SERPAPI_KEY": "qa-only", "requests": types.SimpleNamespace(get=get)}
        api = types.ModuleType("control_gate_api")
        api.emit_ui_log = Mock()
        with patch.dict("sys.modules", {"control_gate_api": api}):
            run_discovery = load_function("discovery_engine.py", "run_discovery", namespace)
            for lat, lng in [(0, 30), (30, 0), (30.2672, -97.7431)]:
                with self.subTest(lat=lat, lng=lng):
                    run_discovery("Austin", "Dental Clinic", lat=lat, lng=lng, radius_km=7)
                    params = get.call_args.kwargs["params"]
                    self.assertEqual(params["ll"], f"@{lat},{lng},14000m")
                    self.assertTrue(params["nearby"])
                    self.assertEqual(params["q"], "Dental Clinic")
            run_discovery("Austin", "Dental Clinic")
            params = get.call_args.kwargs["params"]
            self.assertNotIn("ll", params)
            self.assertEqual(params["q"], "Dental Clinic Austin")

    def test_actual_distance_boundaries_and_antimeridian(self):
        for distance, expected in [(9.999, True), (10, True), (10.001, False)]:
            with self.subTest(distance=distance):
                business = {"title": "QA", "gps_coordinates": {
                    "latitude": math.degrees(distance / 6371.0088), "longitude": 0,
                }}
                self.assertEqual(within_search_radius(business, 0, 0, 10, Mock()), expected)
        wrapped = {"gps_coordinates": {"latitude": 0, "longitude": -179.99}}
        self.assertTrue(within_search_radius(wrapped, 0, 179.99, 3, Mock()))
        self.assertFalse(within_search_radius(wrapped, 0, 179.99, 2, Mock()))

    def test_unknown_or_invalid_result_coordinates_are_logged_and_excluded(self):
        for coordinates in [None, {}, {"latitude": None, "longitude": 0},
                            {"latitude": "NaN", "longitude": 0},
                            {"latitude": 91, "longitude": 0},
                            {"latitude": 0, "longitude": True}]:
            with self.subTest(coordinates=coordinates):
                log = Mock()
                self.assertFalse(within_search_radius({"gps_coordinates": coordinates}, 0, 0, 10, log))
                self.assertIn("cannot be verified", log.call_args.args[0])

    def test_google_filters_distance_before_contact_enrichment(self):
        response = Mock()
        response.json.return_value = {"local_results": [
            {"title": name, "data_id": name, "website": "https://example.test",
             "type": "Dentist",
             "rating": 5, "reviews": 1, "gps_coordinates": coordinates}
            for name, coordinates in [
                ("Inside", {"latitude": 0.01, "longitude": 0}),
                ("Outside", {"latitude": 1, "longitude": 0}),
                ("Unknown", None),
            ]
        ]}
        database = MagicMock()
        database.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        crawl, saved = Mock(return_value=None), Mock()
        namespace = {
            "SERPAPI_KEY": "qa-only", "DB_DSN": "qa-only", "uuid": uuid,
            "requests": types.SimpleNamespace(get=Mock(return_value=response)), "psycopg2": database,
            "filter_business_metrics": filter_business_metrics, "within_search_radius": within_search_radius,
            "attempt_domain_email_crawl": crawl, "save_leads_to_db": saved,
        }
        api = types.ModuleType("control_gate_api")
        api.emit_ui_log = Mock()
        worker = load_function("discovery_engine.py", "run_discovery", namespace)
        with patch.dict("sys.modules", {"control_gate_api": api}):
            worker("Origin", "Dental Clinic", lat=0, lng=0, radius_km=10)
        self.assertEqual([row["name"] for row in saved.call_args.args[0]], ["Inside"])
        crawl.assert_called_once()
