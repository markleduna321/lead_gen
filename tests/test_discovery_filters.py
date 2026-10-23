import ast
import contextlib
import io
from pathlib import Path
import sys
import types
import unittest
import uuid
from unittest.mock import MagicMock, Mock, patch

from flask import Flask, jsonify, request

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from discovery_filters import filter_business_metrics
from discovery_industries import matching_industry_category
from discovery_runs import DiscoveryBusyError
from support import QA_USER_ID, signed_in_client


def load_function(filename, name, namespace):
    namespace.setdefault("matching_industry_category", matching_industry_category)

    def serpapi_fetch(params, timeout=None, attempts=None, log=None, label=""):
        """Stands in for serpapi_client.fetch inside an isolated namespace.

        The call is routed through the namespace's own ``requests`` stub so
        these suites stay offline; the real client's retry behaviour is
        covered separately in test_provider_retries.py.
        """
        import requests as real_requests
        http = namespace.get("requests", real_requests)
        response = http.get("https://serpapi.com/search", params=params, timeout=timeout)
        checker = getattr(response, "raise_for_status", None)
        if callable(checker):
            checker()
        return response

    namespace.setdefault("serpapi_fetch", serpapi_fetch)
    tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    exec(compile(ast.Module(body=[function], type_ignores=[]), filename, "exec"), namespace)
    return namespace[name]


class DiscoveryFilterTests(unittest.TestCase):
    def test_boundaries_and_unknown_metrics(self):
        cases = [
            ({"rating": 4.49, "reviews": 25}, 4.5, 25, None),
            ({"rating": 4.5, "reviews": 25}, 4.5, 25, (4.5, 25)),
            ({"rating": 5, "reviews": 26}, 4.5, 25, None),
            ({"rating": 5, "reviews": 100000}, 4.5, None, (5, 100000)),
            ({"rating": None, "reviews": 0}, 4.5, 25, None),
            ({"rating": 5, "reviews": None}, 4.5, 25, None),
            ({}, 0, None, (None, None)),
            ({"rating": 0, "reviews": 0}, 0, 0, (0, 0)),
            ({"rating": "4.5", "reviews": "25"}, 4.5, 25, (4.5, 25)),
        ]
        for business, minimum, maximum, expected in cases:
            with self.subTest(business=business, minimum=minimum, maximum=maximum):
                log = Mock()
                self.assertEqual(filter_business_metrics(business, minimum, maximum, log), expected)
                if expected is None:
                    log.assert_called_once()

    def test_malformed_metrics_are_logged_and_skipped(self):
        for business in [
            {"rating": "NaN"}, {"rating": 6}, {"rating": True},
            {"reviews": -1}, {"reviews": 1.5}, {"reviews": "many"},
            {"reviews": float("inf")}, {"rating": []},
        ]:
            with self.subTest(business=business):
                log = Mock()
                self.assertIsNone(filter_business_metrics(business, 0, None, log))
                self.assertIn("invalid rating/review", log.call_args.args[0])

    def test_both_workers_filter_before_enrichment(self):
        for filename, name, save_name, result_key in [
            ("discovery_engine.py", "run_discovery", "save_leads_to_db", "local_results"),
            ("discovery_yelp.py", "run_yelp_discovery", "save_yelp_leads", "organic_results"),
        ]:
            for rating, reviews, minimum, maximum, accepted in [
                (4.49, 25, 4.5, 25, False), (4.5, 25, 4.5, 25, True),
                (5, 26, 4.5, 25, False), (5, 100000, 4.5, None, True),
                (None, None, 4.5, 25, False), (None, None, 0, None, True),
            ]:
                with self.subTest(worker=name, rating=rating, reviews=reviews, maximum=maximum):
                    business = {
                        "title": "QA Business", "rating": rating, "reviews": reviews,
                        "type": "Dentist", "categories": [{"title": "Dentists"}],
                        "website": "https://example.test", "data_id": "qa-id", "address": "Austin",
                    }
                    response = Mock()
                    response.json.return_value = {result_key: [business]}
                    database = MagicMock()
                    database.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = []
                    crawl = Mock(return_value="qa@example.test")
                    snapshot = Mock(return_value="/qa.png")
                    saved = Mock()
                    namespace = {
                        "SERPAPI_KEY": "qa-only", "DB_DSN": "qa-only",
                        "requests": types.SimpleNamespace(get=Mock(return_value=response)),
                        "psycopg2": database, "uuid": uuid,
                        "filter_business_metrics": filter_business_metrics,
                        "attempt_domain_email_crawl": crawl,
                        "capture_business_site_snapshot": snapshot,
                        save_name: saved,
                    }
                    modules = {}
                    for module_name, symbol, value in [
                        ("control_gate_api", "emit_ui_log", Mock()),
                        ("email_hunter", "attempt_domain_email_crawl", crawl),
                        ("screenshot_agent", "capture_business_site_snapshot", snapshot),
                    ]:
                        module = types.ModuleType(module_name)
                        setattr(module, symbol, value)
                        modules[module_name] = module
                    worker = load_function(filename, name, namespace)
                    with patch.dict(sys.modules, modules):
                        worker("Austin", "Dental Clinic", minimum, maximum)
                    rows = saved.call_args.args[0]
                    self.assertEqual(len(rows), int(accepted))
                    self.assertEqual(crawl.call_count, int(accepted))
                    self.assertEqual(snapshot.call_count, int(accepted))
                    if accepted:
                        stored_rating = rows[0]["rating"] if result_key == "local_results" else rows[0][6]
                        stored_reviews = rows[0]["reviews"] if result_key == "local_results" else rows[0][5]
                        self.assertEqual(stored_rating, rating)
                        self.assertEqual(stored_reviews, reviews)


class DiscoveryFilterRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask("filter-route-qa")
        self.thread = Mock()
        self.thread.side_effect = lambda target: types.SimpleNamespace(start=target, daemon=False)
        self.workers = {}
        self.runs = Mock()
        def start(tasks, log, owner=None, on_thread_start=None):
            for _, _, task in tasks:
                task()
            return "qa-run"
        self.runs.start.side_effect = start
        self.modules = {}
        for module_name, function_name in [
            ("discovery_engine", "run_discovery"),
            ("discovery_yelp", "run_yelp_discovery"),
            ("discovery_facebook", "run_facebook_discovery"),
            ("discovery_instagram", "run_instagram_discovery"),
        ]:
            module = types.ModuleType(module_name)
            worker = Mock()
            setattr(module, function_name, worker)
            self.modules[module_name] = module
            self.workers[module_name] = worker
        namespace = {
            "app": self.app, "request": request, "jsonify": jsonify,
            "threading": types.SimpleNamespace(Thread=self.thread), "emit_ui_log": Mock(),
            "LOCAL_DISCOVERY_RUNS": self.runs, "DiscoveryBusyError": DiscoveryBusyError,
            "active_user_id": lambda: QA_USER_ID, "bind_log_user": Mock(),
        }
        load_function("control_gate_api.py", "trigger_discovery_engine", namespace)
        self.payload = {
            "location": "Austin", "categories": ["Dental Clinic", "Cafe"],
            "sources": ["google", "yelp", "facebook", "instagram"],
            "rating_min": 4.5, "max_reviews": 25,
            "lat": 30, "lng": -97,
        }

    def test_filters_reach_google_and_yelp_but_social_sources_stay_unfiltered(self):
        for maximum in [25, None, 100000]:
            with self.subTest(maximum=maximum), patch.dict(sys.modules, self.modules), contextlib.redirect_stdout(io.StringIO()):
                self.payload["max_reviews"] = maximum
                response = self.app.test_client().post("/api/trigger/discover", json=self.payload)
                self.assertEqual(response.status_code, 202)
                self.workers["discovery_engine"].assert_any_call("Austin", "Cafe", 4.5, maximum, 30.0, -97.0, 10, user_id=QA_USER_ID)
                self.workers["discovery_yelp"].assert_any_call("Austin", "Dental Clinic", 4.5, maximum, user_id=QA_USER_ID)
                self.workers["discovery_facebook"].assert_any_call("Austin", "Cafe", user_id=QA_USER_ID)
                self.workers["discovery_instagram"].assert_any_call("Austin", "Cafe", user_id=QA_USER_ID)

    def test_invalid_filters_do_not_start_workers(self):
        for field, value in [
            ("rating_min", -1), ("rating_min", 6), ("rating_min", "NaN"),
            ("rating_min", True), ("max_reviews", -1), ("max_reviews", 1.5),
            ("max_reviews", "invalid"), ("max_reviews", True),
        ]:
            with self.subTest(field=field, value=value):
                payload = {**self.payload, field: value}
                response = self.app.test_client().post("/api/trigger/discover", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["status"], "error")
        self.thread.assert_not_called()
        self.runs.start.assert_not_called()

    def test_unavailable_or_invalid_sources_never_start_workers(self):
        for sources, message in [
            (["linkedin"], "LinkedIn discovery is not available"),
            (["google", "linkedin"], "LinkedIn discovery is not available"),
            (["unknown"], "Unsupported discovery data source"),
            ([], "Select at least one"),
            ("google", "Select at least one"),
            (None, "Select at least one"),
        ]:
            with self.subTest(sources=sources):
                response = self.app.test_client().post(
                    "/api/trigger/discover", json={**self.payload, "sources": sources}
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["status"], "error")
                self.assertIn(message, response.json["message"])
        self.thread.assert_not_called()
        self.runs.start.assert_not_called()

    def test_invalid_map_pins_or_radius_do_not_start_workers(self):
        for changes in [
            {"lat": None}, {"lng": None}, {"lat": 91}, {"lng": -181}, {"lat": "NaN"},
            {"lat": True}, {"radius_km": 0}, {"radius_km": 51},
            {"radius_km": 2.5}, {"radius_km": "invalid"},
        ]:
            with self.subTest(changes=changes):
                response = self.app.test_client().post(
                    "/api/trigger/discover", json={**self.payload, **changes}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("valid latitude/longitude", response.json["message"])
        self.thread.assert_not_called()
        self.runs.start.assert_not_called()

    def test_invalid_industries_are_rejected_and_overlapping_run_returns_conflict(self):
        for categories in [[], None, "Dentist", [""], [None]]:
            with self.subTest(categories=categories):
                response = self.app.test_client().post(
                    "/api/trigger/discover", json={**self.payload, "categories": categories}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("industry", response.json["message"])
        self.runs.start.assert_not_called()
        self.runs.start.side_effect = DiscoveryBusyError("Already running")
        response = self.app.test_client().post("/api/trigger/discover", json=self.payload)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Already running", response.json["message"])
