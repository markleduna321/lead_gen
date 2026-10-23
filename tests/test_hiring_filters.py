from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discovery_job_filters import matches_hiring_country, matches_hiring_role, resolve_hiring_country


class HiringFilterTests(unittest.TestCase):
    def test_country_names_codes_and_aliases(self):
        for value, code, name in [
            (" Canada ", "ca", "Canada"), ("CA", "ca", "Canada"),
            ("Australia", "au", "Australia"), ("AU", "au", "Australia"),
            ("United Kingdom", "gb", "United Kingdom"), ("UK", "gb", "United Kingdom"),
            ("U.K.", "gb", "United Kingdom"), ("USA", "us", "United States"),
            ("U.S.", "us", "United States"), ("UAE", "ae", "United Arab Emirates"),
            ("Vietnam", "vn", "Viet Nam"),
        ]:
            with self.subTest(value=value):
                country = resolve_hiring_country(value)
                self.assertIsNotNone(country)
                if country is not None:
                    self.assertEqual((country.code, country.name), (code, name))
        self.assertIsNone(resolve_hiring_country(" "))
        for invalid in ["Atlantis", "Toronto, Canada", "Worldwide", None, []]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                resolve_hiring_country(invalid)

    def test_strict_country_evidence(self):
        canada = resolve_hiring_country("Canada")
        for location in [
            "Canada", "Toronto, ON, Canada", "Remote - Canada",
            "Remote in Canada", "Canada (Remote)", "Canada only",
        ]:
            with self.subTest(location=location):
                self.assertTrue(matches_hiring_country(location, canada))
        for location in [
            "", "Unknown", "Remote", "Anywhere", "Worldwide",
            "Toronto, ON", "Sydney, Australia", "Canada Bay, Australia",
            "Not Canada", "United States", "CA", "Remote (North America)",
        ]:
            with self.subTest(location=location):
                self.assertFalse(matches_hiring_country(location, canada))
        for country, location in [
            ("United States", "New York, NY, US"),
            ("UK", "London, England"), ("UK", "UK (Remote)"),
            ("Australia", "Sydney, Australia"),
        ]:
            with self.subTest(country=country, location=location):
                self.assertTrue(matches_hiring_country(location, resolve_hiring_country(country)))
        self.assertTrue(matches_hiring_country("Remote", None))
        self.assertTrue(matches_hiring_country("Unknown", None))

    def test_all_service_roles_and_common_aliases(self):
        roles = {
            "video_editing": [
                "Video Editor", "Video Content Creator", "Motion Graphics Designer",
                "Video Production Specialist", "Senior Video-Editor", "Motion Graphic Artist",
                "Video Editing Specialist",
            ],
            "bpo_customer_service": [
                "Customer Service Representative", "Customer Support Agent",
                "Call Center Agent", "Support Specialist", "Contact Centre Representative",
                "Customer Care Associate",
            ],
            "bpo_data_entry": [
                "Data Entry Specialist", "Data Processing Clerk", "Back Office Processor",
                "Data Operations Analyst", "Data-Entry Clerk",
            ],
            "social_media_management": [
                "Social Media Manager", "Social Media Specialist", "Community Manager",
                "Content Creator Social Media", "Social Media Coordinator",
            ],
            "virtual_assistant": [
                "Virtual Assistant", "Executive Assistant Remote",
                "Administrative Assistant Remote", "Online Business Manager",
            ],
        }
        for service, titles in roles.items():
            for title in titles:
                with self.subTest(service=service, title=title):
                    self.assertTrue(matches_hiring_role(title, service))

    def test_unrelated_titles_do_not_match(self):
        for service in [
            "video_editing", "bpo_customer_service", "bpo_data_entry",
            "social_media_management", "virtual_assistant",
        ]:
            for title in ["Truck Driver", "Software Engineer", "Accountant", ""]:
                with self.subTest(service=service, title=title):
                    self.assertFalse(matches_hiring_role(title, service))
        for title in ["Editor", "Software Engineer, Video Editing", "Videographer"]:
            self.assertFalse(matches_hiring_role(title, "video_editing"))
        self.assertFalse(matches_hiring_role("Data Scientist", "bpo_data_entry"))
        self.assertFalse(matches_hiring_role("Customer Service Software Engineer", "bpo_customer_service"))
        with self.assertRaises(ValueError):
            matches_hiring_role("Video Editor", "unknown")
