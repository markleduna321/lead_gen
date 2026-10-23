from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import requests

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discovery_job_contacts import find_company_contact, web_url


def html_page(body, status=200, headers=None):
    response = Mock(status_code=status, text=body)
    response.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
    return response


class HiringContactTests(unittest.TestCase):
    def setUp(self):
        self.log = Mock()

    def test_company_name_must_match_search_and_page_before_contacts_are_used(self):
        search = Mock()
        search.json.return_value = {"organic_results": [
            {"title": "Example Studios Jobs", "link": "https://linkedin.com/company/example"},
            {"title": "Other Business", "link": "https://unrelated.test"},
            {"title": "Example Studios", "link": "https://wrong.test"},
            {"title": "Example Studios - Home", "link": "https://www.example.test"},
        ]}
        def get(url, **kwargs):
            if url == "https://serpapi.com/search":
                return search
            if url == "https://wrong.test":
                return html_page("<title>Other Company</title><p>wrong@wrong.test</p>")
            if url == "https://www.example.test":
                return html_page("""
                    <title>Example Studios | Home</title><h1>Example Studios</h1>
                    <p>hello@example.test</p><p>owner@gmail.com</p>
                    <p>noreply@example.test</p>
                    <script>tracking = 'wrong@example.test';</script>
                """)
            if url == "https://www.example.test/contact":
                return html_page('<a href="mailto:careers@example.test">Email</a>'
                                 '<a href="tel:+1-555-0100">Phone</a>')
            if url in {"https://www.example.test/about", "https://www.example.test/contact-us"}:
                return html_page("", 404)
            raise AssertionError(f"Unexpected network: {url}")
        with patch("requests.get", side_effect=get) as network:
            result = find_company_contact("Example Studios", "Canada", "qa-only", self.log)
        self.assertEqual(result["website_url"], "https://www.example.test")
        self.assertEqual(result["email"], "hello@example.test")
        evidence = result["contact_evidence"]
        self.assertEqual(evidence["status"], "matched")
        self.assertEqual(evidence["email_status"], "public_domain_match")
        self.assertEqual(evidence["email_source_url"], "https://www.example.test")
        self.assertEqual({entry["email"] for entry in evidence["emails"]},
                         {"hello@example.test", "careers@example.test"})
        self.assertEqual(evidence["phones"][0]["phone"], "+1-555-0100")
        self.assertEqual(len(evidence["pages_checked"]), 2)
        self.assertEqual(result["errors"], [])
        urls = [call.args[0] for call in network.call_args_list]
        self.assertNotIn("https://unrelated.test", urls)
        self.assertNotIn("https://linkedin.com/company/example", urls)

    def test_cross_host_redirect_cannot_supply_a_company_contact(self):
        search = Mock()
        search.json.return_value = {"organic_results": [
            {"title": "Example Studios", "link": "https://example.test"},
        ]}
        redirect = html_page("", 302, {"Location": "https://unrelated.test"})
        with patch("requests.get", side_effect=[search, redirect]) as network:
            result = find_company_contact("Example Studios", "Canada", "qa-only", self.log)
        self.assertIsNone(result["website_url"])
        self.assertIsNone(result["email"])
        self.assertEqual(network.call_count, 2)
        self.assertIn("outside the matched host", result["errors"][0])
        self.assertFalse(network.call_args.kwargs["allow_redirects"])

    def test_same_host_https_redirect_is_allowed(self):
        search = Mock()
        search.json.return_value = {"organic_results": [
            {"title": "Example Studios", "link": "http://example.test"},
        ]}
        redirect = html_page("", 301, {"Location": "https://www.example.test/"})
        page = html_page("<title>Example Studios</title><p>info@example.test</p>")
        with patch("requests.get", side_effect=[search, redirect, page,
                                               html_page("", 404), html_page("", 404), html_page("", 404)]):
            result = find_company_contact("Example Studios", "", "qa-only", self.log)
        self.assertEqual(result["website_url"], "https://www.example.test/")
        self.assertEqual(result["email"], "info@example.test")

    def test_only_free_mail_is_not_a_matched_company_email(self):
        search = Mock()
        search.json.return_value = {"organic_results": [
            {"title": "Example Studios", "link": "https://example.test"},
        ]}
        page = html_page("<title>Example Studios</title><p>business@gmail.com</p>")
        with patch("requests.get", side_effect=[search, page, html_page("", 404),
                                               html_page("", 404), html_page("", 404)]):
            result = find_company_contact("Example Studios", "", "qa-only", self.log)
        self.assertEqual(result["website_url"], "https://example.test")
        self.assertIsNone(result["email"])
        self.assertEqual(result["contact_evidence"]["email_status"], "unconfirmed")

    def test_contact_network_failures_are_reported_without_losing_matched_website(self):
        search = Mock()
        search.json.return_value = {"organic_results": [
            {"title": "Example Studios", "link": "https://example.test"},
        ]}
        page = html_page("<title>Example Studios</title><p>info@example.test</p>")
        with patch("requests.get", side_effect=[search, page, requests.Timeout("QA timeout"),
                                               html_page("", 404), html_page("", 404)]):
            result = find_company_contact("Example Studios", "", "qa-only", self.log)
        self.assertEqual(result["email"], "info@example.test")
        self.assertIn("QA timeout", result["errors"][0])
        self.assertTrue(any("[CONTACT ERROR]" in call.args[0] for call in self.log.call_args_list))

    def test_empty_and_bad_provider_responses_are_distinct(self):
        search = Mock()
        search.json.return_value = {"organic_results": []}
        with patch("requests.get", return_value=search):
            result = find_company_contact("Example Studios", "", "qa-only", self.log)
            self.assertEqual(result["contact_evidence"]["status"], "unconfirmed")
            self.assertEqual(result["errors"], [])
            for invalid in [{"error": "QA quota failure"}, [], {"organic_results": "invalid"}]:
                search.json.return_value = invalid
                with self.subTest(invalid=invalid), self.assertRaises((ValueError, RuntimeError)):
                    find_company_contact("Example Studios", "", "qa-only", self.log)
        with self.assertRaisesRegex(RuntimeError, "SERPAPI_API_KEY"):
            find_company_contact("Example Studios", "", "", self.log)

    def test_invalid_and_nonweb_links_are_rejected(self):
        for invalid in [
            None, "javascript:alert(1)", "file:///C:/secret", "https://user:pass@example.test",
            "https://localhost", "http://127.0.0.1", "https://[::1]",
            "https://example.test:invalid", "https://example.test/a\nb",
        ]:
            with self.subTest(invalid=invalid):
                self.assertIsNone(web_url(invalid))
        self.assertEqual(web_url("https://example.test/jobs?x=1"), "https://example.test/jobs?x=1")
