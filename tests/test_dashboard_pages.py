import contextlib
import csv
import importlib
import io
from html import unescape
import os
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from discovery_runs import LocalDiscoveryRuns
from support import QA_USER_ID, signed_in_client


class DashboardPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            patch.dict(os.environ, {"APP_ENV": "local", "CARTO_BASEMAP_API_KEY": "qa-public-key"}, clear=True),
            patch("dotenv.load_dotenv"),
            patch("psycopg2.connect", side_effect=AssertionError("Live database access is forbidden")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cls.module = importlib.import_module("control_gate_api")
        cls.client = signed_in_client(cls.module.app)

    def test_all_pages_render_without_database_access(self):
        cases = [
            ("/", "overview", "Overview"),
            ("/overview", "overview", "Overview"),
            ("/prospects", "prospects", "Prospects"),
            ("/job-leads", "jobs", "Job Leads"),
            ("/discovery", "discovery", "Discovery"),
        ]
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            for url, key, title in cases:
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertIn(f'data-page="{key}"', html)
                    self.assertIn(f"<title>{title} | AsuraTECH</title>", html)
                    self.assertEqual(html.count('aria-current="page"'), 1)
                    self.assertIn('href="/prospects"', html)
                    self.assertIn('href="/job-leads"', html)
                    self.assertIn('href="/discovery"', html)
                    self.assertIn('id="sidebar-credits"', html)
                    self.assertIn('id="credit-usage-meter"', html)
                    self.assertIn('aria-label="Search credits used this cycle"', html)

    def test_overview_and_discovery_have_dedicated_content(self):
        overview = self.client.get("/").get_data(as_text=True)
        discovery = self.client.get("/discovery").get_data(as_text=True)
        self.assertIn('id="overview-recent-prospects"', overview)
        self.assertNotIn('id="overview-recent-prospects"', discovery)
        self.assertIn('role="region" aria-label="Discover leads"', discovery)
        self.assertIn('id="discovery-panel-jobs"', discovery)

    def test_shared_assets_are_available(self):
        for asset in ("dashboard.css", "workspace.js", "home.css"):
            with self.subTest(asset=asset):
                response = self.client.get(f"/static/{asset}")
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_public_home_is_available_without_database_access(self):
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            response = self.module.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-page="home"', html)
        self.assertIn('id="theme-preference"', html)
        for preference in ('system', 'light', 'dark'):
            self.assertIn(f'value="{preference}"', html)
        self.assertIn('href="/login"', html)
        self.assertIn('href="/register"', html)
        self.assertIn('href="/plans"', html)
        self.assertNotIn('id="plan-solo"', html)
        self.assertIn('id="platform"', html)
        self.assertIn('id="how-it-works"', html)
        self.assertIn('id="hero-globe"', html)
        self.assertIn('globe/globe.js', html)
        self.assertIn('Illustrative animation, not live discovery data.', html)
        self.assertNotIn('id="table-body"', html)

    def test_public_home_clears_a_removed_account_session(self):
        client = signed_in_client(self.module.app, user_id=4242)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-page="home"', response.get_data(as_text=True))
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_plans_are_on_a_separate_public_page_linking_to_private_checkout(self):
        clients = [self.module.app.test_client(), signed_in_client(self.module.app)]
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            for client in clients:
                with self.subTest(authenticated=client is clients[1]):
                    response = client.get("/plans")
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertIn('<title>Plans &amp; Pricing | AsuraTECH</title>', html)
                    self.assertIn('data-page="plans"', html)
                    for key, name in [('solo', 'Solo'), ('team', 'Small Team'), ('enterprise', 'Enterprise')]:
                        self.assertIn(f'<h2 id="plan-{key}">{name}</h2>', html)
                    self.assertEqual(html.count('class="plan-availability"'), 3)
                    self.assertNotIn('Coming soon', html)
                    self.assertIn('id="trial-title"', html)
                    self.assertIn('Best Value', html)
                    self.assertIn('href="/plans" aria-current="page"', html)
                    self.assertEqual(html.count('aria-current="page"'), 1)
                    self.assertIn('href="/#platform"', html)
                    self.assertIn('href="/#how-it-works"', html)
                    self.assertIn('id="theme-preference"', html)
                    for preference in ('system', 'light', 'dark'):
                        self.assertIn(f'value="{preference}"', html)
                    self.assertNotIn('globe/globe.js', html)
                    self.assertNotIn('type="submit"', html)
                    self.assertIn('href="/billing#buy-solo"', html)
                    self.assertNotIn('$', html)
                    self.assertIn('New accounts receive a 7-day free trial with 30 search credits once they verify their email (one trial per network).', html)

    def test_plan_comparison_matches_published_offering(self):
        response = self.module.app.test_client().get("/plans")
        self.assertEqual(response.status_code, 200)
        html = unescape(response.get_data(as_text=True))
        table = re.search(r'<table class="plans-table">(.*?)</table>', html, re.S).group(1)
        rows = re.findall(r'<tr>(.*?)</tr>', table, re.S)
        cells = [
            [' '.join(re.sub(r'<[^>]+>', ' ', cell).split())
             for cell in re.findall(r'<t[hd]\b[^>]*>(.*?)</t[hd]>', row, re.S)]
            for row in rows
        ]
        self.assertEqual(cells, [
            ['Feature / Metric', 'Free Trial', 'Solo Plan', 'Small Team Plan Best Value', 'Enterprise Plan'],
            ['Monthly Price', '₱0 (7 Days)', '₱999 / month', '₱2,499 / month', '₱5,999 / month'],
            ['Annual Billing (2 Months Free)', 'Not available', '₱9,990 / year (approx. ₱832/mo)', '₱24,990 / year (approx. ₱2,082/mo)', '₱59,990 / year (approx. ₱4,999/mo)'],
            ['Included Search Credits', '30 Search Credits / 7 days', '750 Search Credits / mo', '2,500 Search Credits / mo', '7,500 Search Credits / mo'],
            ['Local Discovery Charge', *['10 credits / industry'] * 4],
            ['Local Industry Searches (Before Refunds; Local-Only Usage)', '3 / trial', '75 / mo', '250 / mo', '750 / mo'],
            ['Approx. Monthly Price per Local Industry Search', 'Free', '₱13.32', '₱10.00', '₱8.00'],
            ['Max Industries per Submission', '1', '3', '5', '10'],
            ['Hiring Discovery Charge', 'Not included', 'Not included', '15 credits / role-page', '15 credits / role-page'],
            ['Max Hiring Pages per Role', 'Not included', 'Not included', '3', '5'],
            ['Zero-New-Lead Refund (Completed Search)', *['70% of search charge'] * 4],
            ['Team Seats', '1 User', '1 User', 'Up to 4 Users', 'Up to 15 Users'],
            ['Daily Submission Cap (Subject to Credit Balance)', '10 submissions / day', '50 submissions / day', '200 submissions / day', '1,000 submissions / day'],
            ['Local Discovery Data Sources', 'Google Maps', 'Google Maps + Yelp', 'Google Maps + Yelp + Facebook + Instagram + LinkedIn company pages', 'Google Maps + Yelp + Facebook + Instagram + LinkedIn company pages'],
            ['Hiring Companies Discovery', 'Not included', 'Upgrade to Small Team', 'Preset services', 'Preset + custom services'],
            ['Add Custom Hiring Service', 'Not included', 'Not included', 'Upgrade to Enterprise', 'Included'],
            ['AI Pitch Drafts', 'Upgrade to Solo', *['1 credit / draft'] * 3],
            ['Other Included Features', 'Site Email/Phone', 'Website Email/Phone', 'All in Solo + Ad Pixel Check', 'All in Team + Bulk CSV Enrichment + Webhook/API Export'],
            ['Max Credit Rollover', 'None', 'Up to 350 credits', 'Up to 1,250 credits', 'Up to 3,500 credits'],
            ['Credit Top-Up Pack (Active subs only)', 'Must upgrade', '₱499 for 350 credits', '₱799 for 750 credits', '₱1,499 for 2,000 credits'],
        ])
        cards = re.findall(r'<article class="plan-card[^"]*".*?</article>', html, re.S)
        for card, monthly, annual, equivalent, credits, seats, searches, rollover, topup in zip(
            cards, ['999', '2,499', '5,999'], ['9,990', '24,990', '59,990'],
            ['832', '2,082', '4,999'], ['750', '2,500', '7,500'],
            ['1 user', 'Up to 4 users', 'Up to 15 users'], ['50', '200', '1,000'],
            ['350', '1,250', '3,500'],
            ['₱499 for 350 credits', '₱799 for 750 credits', '₱1,499 for 2,000 credits'],
        ):
            for expected in [f'₱{monthly}<small> / month', f'₱{annual} / year',
                             f'₱{equivalent}/mo', f'{credits} search credits / month',
                             seats, f'{searches} submissions / day',
                             f'Rollover up to {rollover} credits', topup]:
                with self.subTest(expected=expected):
                    self.assertIn(expected, card)

    def test_search_credit_policy_discloses_charges_refunds_and_preview_status(self):
        response = self.module.app.test_client().get("/plans")
        html = unescape(response.get_data(as_text=True))
        for expected in [
            "Search credits pay for discovery work, not a guaranteed number of leads",
            "Three industries cost 30 credits, not 10.",
            "Local and hiring discovery share the same credit balance.",
            "60 credits at one page per role, 180 at three pages, or 300 at five pages",
            "70% of the search charge is refunded as search credits, not cash",
            "7 credits back (net cost: 3)",
            "10.5 back (net cost: 4.5)",
            "the whole completed submission produces zero new leads",
            "Validation failures and wholly failed provider searches are not charged.",
            "PayMongo QR Ph hosted checkout when payments are configured",
            "one-time payments with no automatic renewal",
            "Solo can select Google Maps and Yelp only.",
            "Facebook, Instagram, and LinkedIn company-page options remain visible but locked",
            "Hiring Companies discovery also remains visible but locked for Solo",
            "Add Service remains visible but locked with an Upgrade to Enterprise prompt.",
            "members share the owner's search credit balance and daily submission cap.",
        ]:
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
        for obsolete in ["lead credits", "Lead Credits", "per lead", "per-lead"]:
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, html)
        cards = re.findall(r'<article class="plan-card[^"]*".*?</article>', html, re.S)
        for card, industries, allowance in zip(cards, [3, 5, 10], [75, 250, 750]):
            self.assertIn(f"Up to {industries} industries per submission", card)
            self.assertIn(f"{allowance} local industry searches before refunds", card)
        self.assertIn("Google Maps and Yelp discovery.", cards[0])
        self.assertIn("Upgrade to Small Team to unlock Facebook, Instagram, LinkedIn company pages", cards[0])
        self.assertIn("Upgrade to Enterprise to add custom hiring services.", cards[1])
        self.assertIn("custom hiring service creation.", cards[2])

    def test_user_management_renders_live_team_data_without_exposing_owner_actions_to_members(self):
        from flask import render_template

        with (
            patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")),
            patch("requests.get", side_effect=AssertionError("Unexpected network access")),
            self.module.app.test_request_context("/"),
        ):
            for plan, name, seats, available in [
                ("small_team", "Small Team", 4, 3),
                ("enterprise", "Enterprise", 15, 14),
            ]:
                with self.subTest(plan=plan):
                    html = render_template("user_management.html", team={
                        "plan": name, "seats": seats, "available": available, "balance": "123.50",
                        "members": [{"id": 1, "email": "owner@example.test", "display_name": "<Owner>",
                                     "team_role": "owner"}],
                        "invitations": [],
                    })
                    self.assertIn("<title>User Management | AsuraTECH</title>", html)
                    self.assertIn('data-page="user-management"', html)
                    self.assertIn(f"{name} team", html)
                    values = re.findall(r'<strong class="metric-value">([\d.]+)</strong>', html)
                    self.assertEqual(values, [str(seats), "1", str(available), "123.50"])
                    self.assertIn("owner@example.test", html)
                    self.assertIn("&lt;Owner&gt;", html)
                    self.assertNotIn('data-remove-member="1"', html)
                    self.assertIn('id="team-invite-form"', html)
                    self.assertIn("Invitations are sent by system email", html)
                    self.assertIn("One team, one credit balance", html)

    def test_user_management_route_requires_sign_in_and_navigation_starts_hidden(self):
        routes = {rule.rule for rule in self.module.app.url_map.iter_rules()}
        self.assertIn("/user-management", routes)
        self.assertNotIn("/team", routes)
        html = self.client.get("/overview").get_data(as_text=True)
        self.assertIn('id="nav-user-management" class="nav-link" hidden', html)
        self.assertEqual(self.module.app.test_client().get("/user-management").status_code, 302)

    def test_globe_assets_are_public_and_have_browser_compatible_content_types(self):
        client = self.module.app.test_client()
        assets = {
            'globe.js': ('text/javascript', 'application/javascript'),
            'regions.json': ('application/json',),
            'region-id.png': ('image/png',),
            'region-id-mobile.png': ('image/png',),
            'world-mask.png': ('image/png',),
            'world-mask-mobile.png': ('image/png',),
            'globe-fallback.png': ('image/png',),
            'flags/ph.svg': ('image/svg+xml',),
        }
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            for asset, mimetypes in assets.items():
                with self.subTest(asset=asset):
                    response = client.get(f"/static/globe/{asset}")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(response.mimetype, mimetypes)
                    response.close()

    def test_prospect_queue_and_export_preserve_separate_profile_urls(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 1, "pending": 1, "sent": 0, "followup": 0}
        lead = {
            "id": 1, "business_name": "QA", "source_platform": "instagram",
            "profile_url": "https://instagram.com/qa", "website_url": None,
            "email": "qa@example.test",
        }
        cursor.fetchall.return_value = [lead]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/queue?source=instagram")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["leads"][0]["profile_url"], lead["profile_url"])
            query, args = cursor.execute.call_args_list[1].args
            self.assertIn("website_url, profile_url", query)
            self.assertIn("instagram", args)
            exported = self.client.get("/api/export")
            rows = list(csv.DictReader(io.StringIO(exported.get_data(as_text=True))))
            self.assertEqual(rows[0]["profile_url"], lead["profile_url"])
            self.assertEqual(rows[0]["website_url"], "")
            self.assertEqual(rows[0]["email"], lead["email"])

    def test_export_neutralizes_formulas_and_builds_a_safe_filename(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{
            "id": 7, "business_name": '=HYPERLINK("http://evil.test","Click")',
            "category": "+cmd|' /C calc'!A0", "subject_line": "-2+3", "suggested_angle": "@SUM(A1)",
            "ai_pitch_draft": "\tTabbed", "formatted_address": "\r=1+1",
            "phone": "+63 912 345 6789", "email": "-safe@example.test", "rating": 4.5,
            "lead_score": -3, "status": "drafted", "website_url": "Café Niño",
        }]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/export?status=drafted")
        self.assertEqual(response.status_code, 200)
        body = response.get_data()
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"), "Excel needs the UTF-8 BOM")
        row = next(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(row["business_name"], '\'=HYPERLINK("http://evil.test","Click")')
        self.assertEqual(row["category"], "'+cmd|' /C calc'!A0")
        self.assertEqual(row["subject_line"], "'-2+3")
        self.assertEqual(row["suggested_angle"], "'@SUM(A1)")
        self.assertEqual(row["ai_pitch_draft"], "'\tTabbed")
        self.assertEqual(row["formatted_address"], "'\r=1+1")
        self.assertEqual(row["email"], "'-safe@example.test")
        self.assertEqual(row["phone"], "+63 912 345 6789")
        self.assertEqual((row["rating"], row["lead_score"]), ("4.5", "-3"))
        self.assertEqual(row["website_url"], "Café Niño")
        disposition = response.headers["Content-Disposition"]
        self.assertRegex(disposition, r'^attachment; filename="leads_drafted_\d{4}-\d{2}-\d{2}\.csv"$')
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("drafted", cursor.execute.call_args.args[1])

    def test_export_rejects_unknown_statuses_before_querying(self):
        with patch("psycopg2.connect", side_effect=AssertionError("Bad input must not reach the database")):
            for status in ["bogus", "x\r\nSet-Cookie: a=b", "drafted;rm", "../etc"]:
                with self.subTest(status=status):
                    response = self.client.get("/api/export", query_string={"status": status})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json["code"], "invalid_status")

    def test_pagination_rejects_non_numbers_and_clamps_ranges(self):
        for route in ["/api/queue", "/api/job-leads"]:
            with patch("psycopg2.connect", side_effect=AssertionError("Bad input must not reach the database")):
                for query in ["page=abc", "per_page=1.5", "page=1e3", "per_page=ten"]:
                    with self.subTest(route=route, query=query):
                        response = self.client.get(f"{route}?{query}")
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(response.json["code"], "invalid_pagination")
                        self.assertNotIn("invalid literal", response.get_data(as_text=True))
            for query, expected in [("per_page=5000&page=0", (50, 0)), ("page=-3&per_page=0", (1, 0)),
                                    ("page=3&per_page=20", (20, 40)), ("page=&per_page=", None),
                                    ("page=99999999999999999999&per_page=50", (50, (100000 - 1) * 50))]:
                with self.subTest(route=route, query=query):
                    database = MagicMock()
                    cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
                    cursor.fetchone.return_value = {"total": 0, "pending": 0, "sent": 0, "followup": 0,
                                                    "with_email": 0, "count": 0}
                    cursor.fetchall.return_value = []
                    with patch("psycopg2.connect", return_value=database):
                        response = self.client.get(f"{route}?{query}")
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                    paged = [call.args[1] for call in cursor.execute.call_args_list
                             if "LIMIT %s OFFSET %s" in call.args[0]]
                    default = 10 if route == "/api/queue" else 20
                    self.assertEqual(tuple(paged[-1][-2:]), expected or (default, 0))

    def test_queue_filters_match_the_displayed_source_and_normalised_industry(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 0, "pending": 0, "sent": 0, "followup": 0}
        cursor.fetchall.side_effect = [[], [
            {"facet": "source", "value": "facebook", "label": "facebook", "count": 2},
            {"facet": "category", "value": "law firm", "label": "Law firm", "count": 2},
            {"facet": "status", "value": "sent_followup_1", "label": "sent_followup_1", "count": 1},
            {"facet": "category", "value": "", "label": "", "count": 9},
        ]]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/queue?source=Facebook&category=Law%20Firm%20&q=main")
        self.assertEqual(response.status_code, 200)
        query, args = cursor.execute.call_args_list[1].args
        self.assertIn("LEFT(COALESCE(google_place_id, ''), 3) = 'fb_'", query)
        self.assertIn("LOWER(BTRIM(COALESCE(category, ''))) = LOWER(%s)", query)
        self.assertIn("COALESCE(formatted_address, '') ILIKE %s", query)
        self.assertNotIn("lead_type, 'web_design') = %s", query)
        self.assertIn("facebook", args)
        self.assertIn("Law Firm", args)
        self.assertEqual(response.json["filter_options"], {
            "source": [{"value": "facebook", "label": "facebook", "count": 2}],
            "category": [{"value": "law firm", "label": "Law firm", "count": 2}],
            "status": [{"value": "sent_followup_1", "label": "sent_followup_1", "count": 1}],
        })

    def test_job_lead_filters_cover_job_board_and_job_search_fields(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 0, "pending": 0, "sent": 0, "with_email": 0}
        cursor.fetchall.side_effect = [[], [
            {"facet": "via", "value": "linkedin", "label": "LinkedIn", "count": 3},
        ]]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/job-leads?via=LinkedIn&lead_type=video_editing&q=Toronto")
        self.assertEqual(response.status_code, 200)
        query, args = cursor.execute.call_args_list[1].args
        self.assertIn("REGEXP_REPLACE(COALESCE(job_via, ''), '^\\s*via\\s+', '', 'i')", query)
        self.assertIn("COALESCE(location, '') ILIKE %s", query)
        self.assertIn("lead_type = %s", query)
        self.assertIn("LinkedIn", args)
        self.assertEqual(args.count("%Toronto%"), 5)
        self.assertEqual(response.json["filter_options"],
                         {"via": [{"value": "linkedin", "label": "LinkedIn", "count": 3}]})

    def test_profile_column_startup_migration_is_idempotent_and_has_no_backfill(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with (
            patch.object(self.module, "DB_DSN", "qa-only"),
            patch("psycopg2.connect", return_value=database),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.module.run_startup_migrations()
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("ALTER TABLE prospects ADD COLUMN IF NOT EXISTS profile_url         TEXT;", queries)
        self.assertFalse(any(query.lstrip().upper().startswith("UPDATE ") for query in queries))

    def test_job_identity_migration_preserves_legacy_rows_and_matches_schema(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with (
            patch.object(self.module, "DB_DSN", "qa-only"),
            patch("psycopg2.connect", return_value=database),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.module.run_startup_migrations()
            first = [call.args[0] for call in cursor.execute.call_args_list]
            cursor.execute.reset_mock()
            self.module.run_startup_migrations()
            second = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(first, second)
        add = "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_identity TEXT;"
        owner = "ALTER TABLE job_leads        ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;"
        index = "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_leads_user_identity   ON job_leads (user_id, job_identity);"
        drop = "ALTER TABLE job_leads DROP CONSTRAINT IF EXISTS unique_job_lead;"
        # Identity and ownership must both exist before the per-user unique index.
        self.assertLess(first.index(add), first.index(owner))
        self.assertLess(first.index(owner), first.index(index))
        self.assertLess(first.index(drop), first.index(index))
        schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()
        for statement in (add, owner, index, drop):
            self.assertIn(statement, first)
            self.assertIn(statement, schema)
        # The old global unique index must not be recreated on startup.
        self.assertNotIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_leads_identity ON job_leads (job_identity);",
            first,
        )
        create = next(sql for sql in first if "CREATE TABLE IF NOT EXISTS job_leads" in sql)
        self.assertIn("job_identity    TEXT,", create)
        self.assertNotIn("UNIQUE (company_name, job_role_signal)", create)
        self.assertFalse(any(sql.lstrip().upper().startswith(
            ("UPDATE ", "DELETE ", "TRUNCATE ", "DROP TABLE ")
        ) for sql in first))
        self.assertEqual(database.__enter__.return_value.commit.call_count, 2)

    def test_richer_job_fields_reach_api_and_manual_email_does_not_claim_matching(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 1, "pending": 1, "sent": 0, "with_email": 1}
        details = {"posted_at": "2 days ago", "apply_options": [
            {"title": "Company", "link": "https://example.test/apply"},
        ]}
        evidence = {"status": "matched", "email_status": "public_domain_match",
                    "email_source_url": "https://example.test/contact"}
        cursor.fetchall.return_value = [{
            "id": 7, "company_name": "QA", "job_title": "Video Editor",
            "job_description": "Full description " * 100, "job_details": details,
            "contact_evidence": evidence,
        }]
        with patch("psycopg2.connect", return_value=database):
            response = self.client.get("/api/job-leads")
            self.assertEqual(response.status_code, 200)
            lead = response.json["leads"][0]
            self.assertEqual(lead["job_details"], details)
            self.assertEqual(lead["contact_evidence"], evidence)
            self.assertGreater(len(lead["job_description"]), 600)
            select = cursor.execute.call_args_list[1].args[0]
            self.assertIn("job_details, contact_evidence", select)
            response = self.client.post("/api/job-leads/7/update-email", json={"email": "manual@example.test"})
            self.assertEqual(response.status_code, 200)
            sql, args = cursor.execute.call_args.args
            self.assertEqual(args, ("manual@example.test", 7, QA_USER_ID))
            self.assertIn("'email_status', 'manual'", sql)
            self.assertIn("'email_source_url', NULL", sql)

    def test_richer_job_migration_is_additive_without_backfill(self):
        database = MagicMock()
        cursor = database.__enter__.return_value.cursor.return_value.__enter__.return_value
        with (
            patch.object(self.module, "DB_DSN", "qa-only"),
            patch("psycopg2.connect", return_value=database),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.module.run_startup_migrations()
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()
        for column in ("job_details", "contact_evidence"):
            statement = f"ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS {column} JSONB;"
            self.assertIn(statement, queries)
            self.assertIn(statement, schema)
        self.assertFalse(any(sql.lstrip().upper().startswith(("UPDATE ", "DELETE ")) for sql in queries))

    def test_local_run_endpoint_reports_partial_failure_and_unknown_run(self):
        from unittest.mock import Mock
        runs = LocalDiscoveryRuns()
        google = types.ModuleType("discovery_engine")
        google.run_discovery = Mock(side_effect=RuntimeError("QA quota exceeded"))
        yelp = types.ModuleType("discovery_yelp")
        yelp.run_yelp_discovery = Mock()
        def thread_factory(target, args, daemon):
            thread = Mock()
            thread.start.side_effect = lambda: target(*args)
            return thread
        with (
            patch.object(self.module, "LOCAL_DISCOVERY_RUNS", runs),
            patch.dict(sys.modules, {"discovery_engine": google, "discovery_yelp": yelp}),
            patch("discovery_runs.threading.Thread", side_effect=thread_factory),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            started = self.client.post("/api/trigger/discover", json={
                "location": "Austin", "categories": ["Dental Clinic"],
                "sources": ["google", "yelp"], "lat": 30, "lng": -97,
            })
            self.assertEqual(started.status_code, 202)
            result = self.client.get(f"/api/discovery/runs/{started.json['run_id']}")
            self.assertEqual(result.status_code, 200)
            run = result.json["run"]
            self.assertEqual(run["state"], "failed")
            self.assertEqual(run["completed"], 2)
            self.assertIn("quota exceeded", run["errors"][0])
            self.assertTrue(run["logs"])
            yelp.run_yelp_discovery.assert_called_once()
            missing = self.client.get("/api/discovery/runs/missing")
            self.assertEqual(missing.status_code, 404)
            self.assertIn("restarted", missing.json["message"])


if __name__ == "__main__":
    unittest.main()
