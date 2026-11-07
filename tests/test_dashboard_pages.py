import contextlib
import csv
import importlib
import io
from html import unescape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import sys
import types
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from discovery_runs import LocalDiscoveryRuns
from support import QA_USER_ID, signed_in_client, with_csrf


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
                    self.assertIn(f"<title>{title} | diskubre</title>", html)
                    self.assertIn('rel="icon" type="image/png"', html)
                    self.assertIn('class="brand-logo"', html)
                    self.assertIn(">diskubre<small>LEAD WORKSPACE</small>", html)
                    self.assertNotIn("AsuraTECH", html)
                    self.assertEqual(html.count('aria-current="page"'), 1)
                    self.assertIn('href="/prospects"', html)
                    self.assertIn('href="/job-leads"', html)
                    self.assertIn('href="/discovery"', html)
                    self.assertIn('id="sidebar-credits"', html)
                    self.assertIn('id="credit-usage-meter"', html)
                    self.assertIn('aria-label="Search credits used this cycle"', html)
                    for path in ("/terms", "/privacy", "/refund-policy", "/support"):
                        self.assertIn(f'href="{path}"', html)

    def test_overview_and_discovery_have_dedicated_content(self):
        overview = self.client.get("/").get_data(as_text=True)
        discovery = self.client.get("/discovery").get_data(as_text=True)
        self.assertIn('id="overview-recent-prospects"', overview)
        self.assertNotIn('id="overview-recent-prospects"', discovery)
        self.assertIn('role="region" aria-label="Discover leads"', discovery)
        self.assertIn('id="discovery-panel-jobs"', discovery)
        self.assertNotIn("Back to overview", discovery)
        main = overview.split('<main id="main-content"', 1)[1].split("</main>", 1)[0]
        self.assertEqual(main.count('href="/discovery"'), 1)

    def test_csv_export_starts_disabled_until_prospects_are_available(self):
        html = self.client.get("/prospects").get_data(as_text=True)
        class ExportLinkParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.attributes = None
                self.text = []
                self.in_export_link = False

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "a" and attributes.get("id") == "btn-export-csv":
                    self.attributes = attributes
                    self.in_export_link = True

            def handle_data(self, data):
                if self.in_export_link:
                    self.text.append(data)

            def handle_endtag(self, tag):
                if tag == "a" and self.in_export_link:
                    self.in_export_link = False

        export_link = ExportLinkParser()
        export_link.feed(html)
        self.assertIsNotNone(export_link.attributes)
        self.assertEqual(export_link.attributes["aria-disabled"], "true")
        self.assertEqual(export_link.attributes["tabindex"], "-1")
        self.assertNotIn("href", export_link.attributes)
        self.assertIn("Export CSV", "".join(export_link.text))
        self.assertIn('id="export-csv-status" class="settings-hint" role="status"', html)

    def test_workspace_header_and_account_menu_are_consolidated(self):
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('id="account-menu"', html)
        self.assertIn('aria-label="Account menu for qa@example.test"', html)
        self.assertIn('id="theme-toggle"', html)
        self.assertNotIn('id="theme-preference"', html)
        self.assertIn('class="account-menu-caption">Your account</span>', html)
        self.assertIn('class="account-menu-chevron"', html)
        self.assertIn("qa@example.test", html)
        self.assertIn('href="/settings#account-security-title">Change password</a>', html)
        self.assertIn('href="/settings">Account settings</a>', html)
        self.assertIn('action="/logout"', html)
        self.assertNotIn("AsuraTECH", html)
        self.assertNotIn('id="account-plan-status"', html)
        self.assertNotIn('class="primary-button" href="/discovery"', html)
        self.assertNotIn('id="sidebar-logout"', html)

        sidebar = re.search(
            r'<div class="sidebar-scroll">(.*?)</div>\s*<div class="sidebar-footer">',
            html,
            re.S,
        ).group(1)
        nav_billing = re.search(
            r'<nav class="side-nav" aria-labelledby="nav-label-more">(.*?)</nav>',
            sidebar,
            re.S,
        ).group(1)
        self.assertIn('id="nav-billing"', nav_billing)
        self.assertIn('id="nav-user-management"', nav_billing)

    def test_public_footer_links_home_and_plans(self):
        public_client = self.module.app.test_client()
        for path in ("/", "/plans"):
            html = public_client.get(path).get_data(as_text=True)
            self.assertIn('<a href="/">Home</a>', html)
            self.assertIn('<a href="/plans">Plans &amp; Pricing</a>', html)

    def test_public_seo_content_canonicals_and_sitemap(self):
        client = self.module.app.test_client()
        with patch.dict(os.environ, {"APP_BASE_URL": "https://www.diskubre.example/"}):
            home = client.get("/").get_data(as_text=True)
            self.assertIn(
                "<title>Local Business Leads &amp; Hiring Signals | diskubre</title>",
                home,
            )
            self.assertIn(
                'name="description" content="Find local business leads by industry and location, discover companies hiring for your services, and organize prospect outreach with diskubre."',
                home,
            )
            self.assertIn(
                'rel="canonical" href="https://www.diskubre.example/"',
                home,
            )
            self.assertIn(
                "<h1 id=\"home-title\">Find local business leads and hiring companies.</h1>",
                home,
            )
            for fragment in (
                "local-business-discovery",
                "hiring-company-discovery",
                "personalized-outreach",
            ):
                self.assertIn(f'href="/plans#{fragment}"', home)

            plans = client.get("/plans").get_data(as_text=True)
            self.assertIn(
                "<title>Lead Discovery Plans &amp; Pricing | diskubre</title>",
                plans,
            )
            self.assertIn(
                'rel="canonical" href="https://www.diskubre.example/plans"',
                plans,
            )
            for fragment in (
                "local-business-discovery",
                "hiring-company-discovery",
                "personalized-outreach",
            ):
                self.assertIn(f'id="{fragment}"', plans)

            robots = client.get("/robots.txt")
            self.assertEqual(robots.status_code, 200)
            self.assertIn("Disallow: /api/", robots.get_data(as_text=True))
            self.assertIn(
                "Sitemap: https://www.diskubre.example/sitemap.xml",
                robots.get_data(as_text=True),
            )
            sitemap = client.get("/sitemap.xml")
            self.assertEqual(sitemap.status_code, 200)
            sitemap_xml = sitemap.get_data(as_text=True)
            self.assertIn("<loc>https://www.diskubre.example/</loc>", sitemap_xml)
            self.assertIn("<loc>https://www.diskubre.example/plans</loc>", sitemap_xml)
            self.assertNotIn("/login", sitemap_xml)
            self.assertNotIn("/prospects", sitemap_xml)

    def test_private_and_auth_pages_are_not_indexable(self):
        clients_and_paths = (
            (self.module.app.test_client(), ("/login", "/register", "/verify-email",
                                              "/team-invitation")),
            (self.client, ("/overview", "/prospects", "/job-leads", "/settings")),
        )
        for client, paths in clients_and_paths:
            for path in paths:
                with self.subTest(path=path):
                    html = client.get(path).get_data(as_text=True)
                    self.assertIn(
                        '<meta name="robots" content="noindex, nofollow">',
                        html,
                    )

    def test_sitemap_requires_a_configured_https_site_origin(self):
        client = self.module.app.test_client()
        with patch.dict(os.environ, {"APP_BASE_URL": "http://diskubre.example"}):
            response = client.get("/sitemap.xml")
            self.assertEqual(response.status_code, 503)
            self.assertIn("APP_BASE_URL", response.get_data(as_text=True))
            self.assertNotIn(
                "Sitemap:",
                client.get("/robots.txt").get_data(as_text=True),
            )

    def test_workspace_controls_have_programmatic_names(self):
        class FormControlNameParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.label_depth = 0
                self.label_for = set()
                self.controls = []

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "label":
                    self.label_depth += 1
                    if attributes.get("for"):
                        self.label_for.add(attributes["for"])
                elif tag in {"input", "select", "textarea"}:
                    self.controls.append((tag, attributes, self.label_depth > 0))

            def handle_endtag(self, tag):
                if tag == "label" and self.label_depth:
                    self.label_depth -= 1

            def handle_startendtag(self, tag, attrs):
                self.handle_starttag(tag, attrs)
                self.handle_endtag(tag)

        pages = ("discovery", "prospects", "jobs", "settings")
        rendered_pages = [
            self.client.get(path).get_data(as_text=True)
            for path in ("/discovery", "/prospects", "/job-leads", "/settings")
        ]
        for page, html in zip(pages, rendered_pages):
            parser = FormControlNameParser()
            parser.feed(html)
            missing_names = []
            for tag, attributes, wrapped_by_label in parser.controls:
                if tag == "input" and (
                    attributes.get("type") == "hidden" or "hidden" in attributes
                ):
                    continue
                if (
                    not attributes.get("aria-label")
                    and not attributes.get("aria-labelledby")
                    and not wrapped_by_label
                    and attributes.get("id") not in parser.label_for
                ):
                    missing_names.append(attributes.get("id", tag))
            with self.subTest(page=page):
                self.assertEqual(missing_names, [])

        html = rendered_pages[0]
        labels = set(re.findall(r'<label\b[^>]*\bfor="([^"]+)"', html))
        for control_id in (
            "recipient-email-input",
            "job-modal-email-input",
            "profile-email-input",
            "job-country-input",
            "modal-radius",
            "custom-biz-input",
            "modal-rating-min",
            "modal-reviews-max",
            "editor",
        ):
            with self.subTest(control_id=control_id):
                self.assertIn(control_id, labels)

        for control_id, accessible_name in (
            ("table-search-input", "Search leads"),
            ("filter-sort", "Sort results"),
            ("btn-page-prev", "Previous page"),
            ("btn-page-next", "Next page"),
        ):
            with self.subTest(control_id=control_id):
                self.assertRegex(
                    html,
                    rf'<(?:input|select|button)\b[^>]*\bid="{control_id}"[^>]*'
                    rf'\baria-label="{accessible_name}"',
                )

    def test_prospect_send_copy_and_icons_are_unambiguous(self):
        html = self.client.get("/prospects").get_data(as_text=True)
        self.assertIn('id="recipient-email-input"', html)
        self.assertIn(">Send to email address</label>", html)
        self.assertIn(
            "Sent to the address shown. Defaults to the prospect's saved email.",
            unescape(html),
        )
        self.assertIn("Approve &amp; Send Email", html)
        self.assertNotIn("Approve &amp; Send Test", html)
        self.assertNotRegex(html, r"[🔍📋🤖📍✕]")
        self.assertRegex(
            html,
            r'(?s)id="btn-view-more"[^>]*>.*?<svg[^>]*aria-hidden="true".*?</svg>\s*View More Data',
        )

    def test_templates_do_not_use_inline_event_handler_attributes(self):
        class InlineHandlerParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.handlers = []

            def handle_starttag(self, tag, attrs):
                self.handlers.extend(
                    f"{tag}[{name}]" for name, _ in attrs
                    if name and name.lower().startswith("on")
                )

            handle_startendtag = handle_starttag

        template_dir = Path(__file__).resolve().parents[1] / "src" / "templates"
        for template_path in template_dir.glob("*.html"):
            parser = InlineHandlerParser()
            parser.feed(template_path.read_text(encoding="utf-8"))
            with self.subTest(template=template_path.name):
                self.assertEqual(parser.handlers, [])

    def test_shared_assets_are_available(self):
        for asset in (
            "dashboard.css",
            "tailwind.css",
            "workspace.js",
            "home.css",
            "vendor/fonts/fonts.css",
            "vendor/fonts/dm-sans-latin-wght-normal.woff2",
            "vendor/fonts/dm-sans-latin-ext-wght-normal.woff2",
            "vendor/fonts/manrope-latin-wght-normal.woff2",
            "vendor/fonts/manrope-latin-ext-wght-normal.woff2",
        ):
            with self.subTest(asset=asset):
                response = self.client.get(f"/static/{asset}")
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_self_hosted_fonts_are_preloaded_without_css_imports(self):
        repository = Path(__file__).resolve().parents[1]
        dashboard_css = (repository / "static" / "dashboard.css").read_text(encoding="utf-8")
        fonts_css = (repository / "static" / "vendor" / "fonts" / "fonts.css").read_text(encoding="utf-8")
        tailwind_css = (repository / "static" / "tailwind.css").read_text(encoding="utf-8")
        templates = (
            "base.html",
            "public_page.html",
            "auth.html",
            "verify_email.html",
            "home.html",
            "legal.html",
            "plans.html",
            "error.html",
        )
        font_files = (
            "dm-sans-latin-wght-normal.woff2",
            "manrope-latin-wght-normal.woff2",
        )

        self.assertNotIn("@import", dashboard_css)
        self.assertEqual(fonts_css.count("font-weight: 400 700;"), 2)
        self.assertEqual(fonts_css.count("font-weight: 600 800;"), 2)
        self.assertIn("font-family: 'DM Sans';", fonts_css)
        self.assertIn("font-family: 'Manrope';", fonts_css)
        self.assertNotIn(".font-medium{", tailwind_css)
        self.assertIn(".font-semibold{", tailwind_css)

        for template_name in templates:
            template = (
                repository / "src" / "templates" / template_name
            ).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertIn("filename='vendor/fonts/fonts.css'", template)
                self.assertNotIn("latin-ext", template)
                self.assertLess(template.index('rel="preload"'), template.index("<script"))
                for font_file in font_files:
                    self.assertIn(
                        f"rel=\"preload\" href=\"{{{{ url_for('static', filename='vendor/fonts/{font_file}') }}}}\" "
                        "as=\"font\" type=\"font/woff2\" crossorigin",
                        template,
                    )

        font_weights = set()
        for stylesheet in (
            dashboard_css,
            (repository / "static" / "home.css").read_text(encoding="utf-8"),
        ):
            font_weights.update(re.findall(r"\bfont-weight:\s*(\d{3})\b", stylesheet))
            font_weights.update(re.findall(r"\bfont:\s*(\d{3})\s", stylesheet))
        self.assertEqual(font_weights, {"400", "600", "700", "800"})

    def test_auth_and_settings_focus_indicators_remain_visible(self):
        repository = Path(__file__).resolve().parents[1]
        dashboard_css = (repository / "static" / "dashboard.css").read_text(encoding="utf-8")
        self.assertRegex(
            dashboard_css,
            r"\.auth-form input:focus-visible,\s*\.settings-form input:focus-visible,\s*"
            r"\.settings-form select:focus-visible,[\s\S]*?\.page-content input:focus-visible,[\s\S]*?"
            r"outline:\s*3px solid var\(--accent\)",
        )
        self.assertNotRegex(
            dashboard_css,
            r"\.auth-form input:focus\s*\{\s*outline:\s*none",
        )
        self.assertNotRegex(
            dashboard_css,
            r"\.settings-form input:focus,\s*\.settings-form select:focus\s*\{\s*outline:\s*none",
        )

    def test_theme_color_select_styling_and_mobile_safe_auth_focus(self):
        repository = Path(__file__).resolve().parents[1]
        templates = repository / "src" / "templates"
        head_templates = [
            template for template in templates.glob("*.html")
            if "<head>" in template.read_text(encoding="utf-8")
        ]
        self.assertTrue(head_templates)
        for template in head_templates:
            with self.subTest(template=template.name):
                html = template.read_text(encoding="utf-8")
                self.assertEqual(html.count('id="theme-color-meta" name="theme-color"'), 1)
                self.assertNotRegex(html, r"\sautofocus(?:\s|>)")

        auth_html = (templates / "auth.html").read_text(encoding="utf-8")
        self.assertIn('id="email" name="email" type="email"', auth_html)
        self.assertIn('autocomplete="email" value="{{ email or \'\' }}"', auth_html)
        self.assertIn('aria-describedby="password-hint" data-autofocus', auth_html)

        dashboard_css = (repository / "static" / "dashboard.css").read_text(encoding="utf-8")
        home_css = (repository / "static" / "home.css").read_text(encoding="utf-8")
        self.assertRegex(
            dashboard_css,
            r"select\s*\{[^}]*background-color:\s*var\(--surface-soft\)[^}]*color:\s*var\(--text\)",
        )
        self.assertRegex(
            dashboard_css,
            r"select option\s*\{[^}]*background-color:\s*var\(--surface\)[^}]*color:\s*var\(--text\)",
        )
        self.assertNotRegex(home_css, r"\.home-theme select\s*\{[^}]*background:\s*transparent")

    def test_registration_validation_renders_field_errors_and_preserves_safe_values(self):
        cases = [
            (
                {"email": "not-an-email", "password": "LocalPreviewOnly123!",
                 "confirm_password": "LocalPreviewOnly123!", "display_name": "Ana"},
                "email-error", "Enter a valid email address.", "email", "Ana",
            ),
            (
                {"email": "ana@example.test", "password": "password123",
                 "confirm_password": "password123", "display_name": "Ana"},
                "password-error", "That password is too common.", "password", "Ana",
            ),
            (
                {"email": "ana@example.test", "password": "LocalPreviewOnly123!",
                 "confirm_password": "different", "display_name": "Ana"},
                "confirm_password-error", "Both passwords must match.", "confirm_password", "Ana",
            ),
            (
                {"email": "ana@example.test", "password": "LocalPreviewOnly123!",
                 "confirm_password": "LocalPreviewOnly123!", "display_name": "A" * 121},
                "display_name-error", "Your name must be 120 characters or fewer.",
                "display_name", "A" * 121,
            ),
        ]
        client = with_csrf(self.module.app.test_client())
        with patch.object(self.module.limiter(), "hit"):
            for form, error_id, message, focus_field, retained_name in cases:
                response = client.post("/register", data=form)
                html = response.get_data(as_text=True)
                with self.subTest(error_id=error_id):
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(f'id="{error_id}" role="alert"', html)
                    self.assertIn(message, html)
                    self.assertRegex(
                        html,
                        rf'<input\b[^>]*\bid="{focus_field}"[^>]*aria-invalid="true"[^>]*data-autofocus',
                    )
                    self.assertIn('data-autofocus', html)
                    self.assertIn(f'value="{retained_name}"', html)
                    self.assertNotIn('value="LocalPreviewOnly123!"', html)

    def test_registration_password_meter_and_auth_script_are_rendered(self):
        html = self.module.app.test_client().get("/register").get_data(as_text=True)
        self.assertIn('src="/static/auth.js" defer', html)
        self.assertIn('id="password-strength-meter"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('id="password-strength"', html)
        self.assertIn("Avoid common passwords", html)

    def test_workspace_utility_css_is_precompiled_and_theme_aware(self):
        repository = Path(__file__).resolve().parents[1]
        dashboard_css = (repository / "static" / "dashboard.css").read_text(encoding="utf-8")
        tailwind_css = (repository / "static" / "tailwind.css").read_text(encoding="utf-8")
        templates = "\n".join(
            template.read_text(encoding="utf-8")
            for template in (repository / "src" / "templates").glob("*.html")
        )

        self.assertNotIn("tailwindcss-browser", templates)
        self.assertNotIn("[class*=", dashboard_css)
        self.assertIn(".grid-cols-2", tailwind_css)
        self.assertIn(".bg-slate-950", tailwind_css)
        self.assertIn(".pl-9", tailwind_css)
        self.assertIn(".inline-flex", tailwind_css)
        self.assertIn(".pointer-events-none", tailwind_css)
        self.assertIn(".left-2\\.5", tailwind_css)
        self.assertIn(".top-1\\/2", tailwind_css)
        self.assertIn(".-translate-y-1\\/2", tailwind_css)
        self.assertIn("background-color:var(--surface)", tailwind_css)
        self.assertIn("color:var(--muted)", tailwind_css)

    def test_type_scale_sets_a_12px_floor_and_readable_controls(self):
        repository = Path(__file__).resolve().parents[1]
        dashboard_css = (repository / "static" / "dashboard.css").read_text(encoding="utf-8")
        home_css = (repository / "static" / "home.css").read_text(encoding="utf-8")
        dashboard_html = (repository / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")

        for token, size in (
            ("--type-caption", "12px"),
            ("--type-ui", "13px"),
            ("--type-body-small", "14px"),
            ("--type-body", "16px"),
            ("--type-title-small", "18px"),
            ("--type-title", "22px"),
            ("--type-heading", "28px"),
            ("--type-display", "36px"),
        ):
            with self.subTest(token=token):
                self.assertIn(f"{token}: {size};", dashboard_css)

        for stylesheet in (dashboard_css, home_css):
            declarations = re.findall(r"\bfont(?:-size)?:\s*([^;}]+)", stylesheet)
            for declaration in declarations:
                for size in re.findall(r"(?<![\w.-])(\d+(?:\.\d+)?)px", declaration):
                    self.assertGreaterEqual(float(size), 12)

        for selector in (
            ".nav-link",
            ".primary-button, .secondary-button",
            "#discovery-tab-local, #discovery-tab-jobs",
            ".auth-submit",
        ):
            self.assertRegex(
                dashboard_css,
                re.escape(selector) + r" \{[^}]*font-size: var\(--type-body-small\)",
            )
        for selector in (".home-nav-links a, .home-sign-in", ".home-button", ".home-nav-cta"):
            self.assertRegex(
                home_css,
                re.escape(selector) + r" \{[^}]*font-size: var\(--type-body-small\)",
            )
        self.assertNotRegex(dashboard_html, r"text-\[(?:[1-9]|1[01])px\]")

    def test_light_theme_muted_colors_meet_aa_contrast(self):
        repository = Path(__file__).resolve().parents[1]
        dashboard_css = (repository / "static" / "dashboard.css").read_text(encoding="utf-8")
        light_theme = dashboard_css.split(":root[data-theme=\"dark\"]", 1)[0]

        def token(name):
            match = re.search(
                r"--" + name + r":\s*(#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?);",
                light_theme,
            )
            self.assertIsNotNone(match, f"Missing light-theme --{name} token")
            value = match.group(1)
            if len(value) == 4:
                value = "#" + "".join(channel * 2 for channel in value[1:])
            return value

        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                for channel in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast_ratio(foreground, background):
            lighter, darker = sorted(
                (luminance(foreground), luminance(background)), reverse=True
            )
            return (lighter + 0.05) / (darker + 0.05)

        for text_token in ("muted", "subtle"):
            foreground = token(text_token)
            for background_token in ("surface", "canvas", "surface-soft", "surface-hover"):
                background = token(background_token)
                with self.subTest(text_token=text_token, background_token=background_token):
                    self.assertGreaterEqual(
                        contrast_ratio(foreground, background),
                        4.5,
                    )

    def test_public_home_is_available_without_database_access(self):
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            response = self.module.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-page="home"', html)
        self.assertNotIn('id="theme-preference"', html)
        self.assertIn('id="theme-toggle"', html)
        self.assertIn('data-click-action="toggleThemePreference"', html)
        self.assertIn('aria-label="Switch to dark mode"', html)
        self.assertIn('class="theme-icon-sun"', html)
        self.assertIn('class="theme-icon-moon"', html)
        self.assertIn('href="/login"', html)
        self.assertIn('href="/register"', html)
        self.assertIn('href="/plans"', html)
        trial_cta = re.search(r'<a class="home-button" href="([^"]+)">Start your free trial', html)
        self.assertIsNotNone(trial_cta)
        self.assertEqual(urlsplit(trial_cta.group(1)).path, "/register")
        self.assertIn("7-day free trial and 30 search credits after email verification", html)
        self.assertIn("Google Maps, Yelp, Facebook, Instagram, and LinkedIn company pages", html)
        self.assertIn("Google Jobs and LinkedIn Jobs", html)
        self.assertNotIn("through Google Jobs.</p>", html)
        self.assertNotIn('id="plan-solo"', html)
        self.assertIn('id="platform"', html)
        self.assertIn('id="how-it-works"', html)
        self.assertIn('id="hero-globe"', html)
        self.assertIn('globe/globe.js', html)
        self.assertIn('Illustrative animation, not live discovery data.', html)
        self.assertNotIn('id="table-body"', html)

    def test_login_split_design_reuses_hero_globe_and_preserves_form_fields(self):
        client = self.module.app.test_client()
        response = client.get("/login?next=%2Fprospects")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('class="auth-shell login-shell"', html)
        self.assertLess(html.index('class="login-story"'), html.index('class="login-form-panel"'))
        self.assertIn('id="hero-globe"', html)
        self.assertIn('globe/globe.js', html)
        self.assertIn('globe/globe-fallback.png', html)
        self.assertIn('class="home-globe-pause"', html)
        self.assertIn('data-globe-caption', html)
        self.assertIn('login.css', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('name="next" value="/prospects"', html)
        self.assertIn('autocomplete="current-password"', html)
        self.assertIn('href="/register?next=/prospects"', html)
        for route in ("/register", "/forgot-password"):
            with self.subTest(route=route):
                page = client.get(route).get_data(as_text=True)
                self.assertNotIn('login-shell', page)
                self.assertNotIn('globe/globe.js', page)
                self.assertNotIn('login.css', page)

    def test_public_home_clears_a_removed_account_session(self):
        client = signed_in_client(self.module.app, user_id=4242)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-page="home"', response.get_data(as_text=True))
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_signed_out_invitation_uses_public_layout_and_authentication_return_path(self):
        from flask import render_template

        client = self.module.app.test_client()
        response = client.get("/team-invitation")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<body data-page="team-invitation" class="home-body">', html)
        self.assertIn('aria-label="Main navigation"', html)
        self.assertIn('aria-label="Policies and support"', html)
        self.assertNotIn('id="sidebar"', html)
        self.assertNotIn('id="sidebar-credits"', html)
        self.assertIn('href="/login?next=/team-invitation"', html)
        self.assertIn('href="/register?next=/team-invitation"', html)

        login = client.get("/login?next=%2Fteam-invitation").get_data(as_text=True)
        register = client.get("/register?next=%2Fteam-invitation").get_data(as_text=True)
        self.assertIn('href="/register?next=/team-invitation"', login)
        self.assertIn('href="/login?next=/team-invitation"', register)

        with (
            self.module.app.test_request_context("/team-invitation"),
            patch.object(self.module, "signed_in_user", return_value={"id": 1}),
        ):
            signed_in_html = render_template(
                "team_invitation.html", page="team-invitation", page_title="Team Invitation",
                page_eyebrow="YOUR NEXT CHAPTER, TOGETHER",
                page_description="Join a team while keeping your leads private.",
            )
        self.assertIn('id="accept-team-invitation" type="button" class="primary-button" disabled', signed_in_html)

    def test_public_legal_pages_and_registration_links(self):
        client = self.module.app.test_client()
        pages = [
            ("/terms", "Terms of Service"),
            ("/privacy", "Privacy Policy"),
            ("/refund-policy", "Refund Policy"),
            ("/support", "Contact &amp; Support"),
        ]
        for path, title in pages:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(f"<title>{title} | diskubre</title>", html)
                for linked_path in ("/terms", "/privacy", "/refund-policy", "/support"):
                    self.assertIn(f'href="{linked_path}"', html)

        privacy = client.get("/privacy").get_data(as_text=True)
        self.assertIn("keyed hash", privacy)
        self.assertIn("not the raw address", privacy)
        self.assertIn("IPv6 addresses are grouped by their /64 network", privacy)
        self.assertIn("retained to prevent repeated trial claims", privacy)
        support = client.get("/support").get_data(as_text=True)
        self.assertIn("A direct support email has not been configured", support)

        registration = client.get("/register")
        self.assertEqual(registration.status_code, 200)
        register_html = registration.get_data(as_text=True)
        self.assertIn('href="/terms"', register_html)
        self.assertIn('href="/privacy"', register_html)
        self.assertIn('href="/support"', register_html)

    def test_plans_are_on_a_separate_public_page_linking_to_private_checkout(self):
        clients = [self.module.app.test_client(), signed_in_client(self.module.app)]
        with patch("psycopg2.connect", side_effect=AssertionError("Unexpected database access")):
            for client in clients:
                with self.subTest(authenticated=client is clients[1]):
                    response = client.get("/plans")
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertIn('<title>Lead Discovery Plans &amp; Pricing | diskubre</title>', html)
                    self.assertIn('data-page="plans"', html)
                    for key, name in [('solo', 'Solo'), ('small_team', 'Small Team'), ('enterprise', 'Enterprise')]:
                        self.assertIn(f'<h2 id="plan-{key}">{name}</h2>', html)
                    self.assertEqual(html.count('class="plan-availability"'), 3)
                    for unreleased_feature in ("Ad Pixel Check", "Bulk CSV Enrichment", "Webhook/API Export"):
                        self.assertNotIn(unreleased_feature, html)
                    self.assertIn('id="trial-title"', html)
                    self.assertIn("Google Maps discovery + website email and phone lookup.", html)
                    self.assertIn("1 industry per submission", html)
                    self.assertIn('Best Value', html)
                    self.assertNotIn('Best value', html)
                    self.assertIn('href="/plans" aria-current="page"', html)
                    self.assertEqual(html.count('aria-current="page"'), 1)
                    self.assertIn('href="/#platform"', html)
                    self.assertIn('href="/#how-it-works"', html)
                    self.assertIn('id="theme-preference"', html)
                    for preference in ('system', 'light', 'dark'):
                        self.assertIn(f'value="{preference}"', html)
                    self.assertNotIn('globe/globe.js', html)
                    self.assertNotIn('type="submit"', html)
                    self.assertIn('data-plan-cta="solo"', html)
                    self.assertNotIn('$', html)
                    self.assertIn('New accounts receive a 7-day free trial with 30 search credits after email verification (one trial per network).', html)

    def test_plan_calls_to_action_preserve_intent_for_anonymous_and_signed_in_visitors(self):
        anon_html = self.module.app.test_client().get("/plans").get_data(as_text=True)
        signed_html = self.client.get("/plans").get_data(as_text=True)
        for plan_id in ("solo", "small_team", "enterprise"):
            pattern = rf'<a class="home-button" href="([^"]+)" data-plan-cta="{plan_id}">'
            anonymous_href = re.search(pattern, anon_html).group(1)
            anonymous_url = urlsplit(anonymous_href)
            self.assertEqual(anonymous_url.path, "/register")
            self.assertEqual(parse_qs(anonymous_url.query)["next"][0],
                             f"/billing?plan={plan_id}#buy-{plan_id}")

            signed_href = re.search(pattern, signed_html).group(1)
            signed_url = urlsplit(signed_href)
            self.assertEqual(signed_url.path, "/billing")
            self.assertEqual(parse_qs(signed_url.query)["plan"][0], plan_id)
            self.assertEqual(signed_url.fragment, f"buy-{plan_id}")

        trial_pattern = r'<a class="home-button" href="([^"]+)" data-plan-cta="trial">'
        anonymous_trial = urlsplit(re.search(trial_pattern, anon_html).group(1))
        self.assertEqual(anonymous_trial.path, "/register")
        self.assertEqual(parse_qs(anonymous_trial.query)["next"][0], "/billing#billing-trial-offer")
        signed_trial = urlsplit(re.search(trial_pattern, signed_html).group(1))
        self.assertEqual(signed_trial.path, "/billing")
        self.assertEqual(signed_trial.fragment, "billing-trial-offer")

        target = "/billing?plan=solo#buy-solo"
        auth_next = self.module.app.test_client()
        login = auth_next.get("/login", query_string={"next": target}).get_data(as_text=True)
        register = auth_next.get("/register", query_string={"next": target}).get_data(as_text=True)
        register_href = re.search(r'<a href="([^"]+)">Create an account</a>', login).group(1)
        sign_in_href = re.search(r'<a href="([^"]+)">Sign in</a>', register).group(1)
        self.assertEqual(parse_qs(urlsplit(register_href).query)["next"][0], target)
        self.assertEqual(parse_qs(urlsplit(sign_in_href).query)["next"][0], target)
        self.assertIn(f'name="next" value="{target}"', login)
        self.assertIn(f'name="next" value="{target}"', register)

    def test_plan_comparison_matches_published_offering(self):
        response = self.module.app.test_client().get("/plans")
        self.assertEqual(response.status_code, 200)
        html = unescape(response.get_data(as_text=True))
        table = re.search(r'<table class="plans-table">(.*?)</table>', html, re.S).group(1)
        rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S)
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
            ['Other Included Features', *['Website Email/Phone'] * 4],
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

    def test_public_pricing_tracks_plan_capabilities_and_checkout_prices(self):
        from dataclasses import replace
        import payments
        import plans

        updated_solo = replace(
            plans.PLANS["solo"], credits=900, seats=2, industries=4,
            daily=77, rollover=425,
        )
        updated_prices = {
            **payments.PRICES["solo"],
            1: 123400,
            12: 1234000,
            "topup": 23400,
            "pack": 120,
        }
        with (
            patch.dict(plans.PLANS, {"solo": updated_solo}),
            patch.dict(payments.PRICES, {"solo": updated_prices}),
        ):
            response = self.module.app.test_client().get("/plans")

        self.assertEqual(response.status_code, 200)
        html = unescape(response.get_data(as_text=True))
        card = re.search(
            r'<article class="plan-card[^"]*" aria-labelledby="plan-solo">(.*?)</article>',
            html, re.S,
        ).group(1)
        for expected in [
            "₱1,234<small> / month",
            "₱12,340 / year",
            "₱1,028/mo",
            "900 search credits / month",
            "90 local industry searches",
            "Up to 4 industries per submission",
            "Up to 2 users",
            "77 submissions / day",
            "Rollover up to 425 credits",
            "₱234 for 120 credits",
        ]:
            with self.subTest(expected=expected):
                self.assertIn(expected, card)
        self.assertIn("₱1,234 / month", html)
        self.assertIn("₱13.71", html)
        self.assertIn("90 / mo", html)
        self.assertIn("Up to 2 Users", html)
        self.assertIn(
            'content="Compare diskubre plans for local business leads, hiring searches, and prospect outreach. Start a 7-day free trial; plans from PHP 1,234/month.',
            html,
        )

    def test_search_credit_policy_discloses_charges_refunds_and_preview_status(self):
        response = self.module.app.test_client().get("/plans")
        html = unescape(response.get_data(as_text=True))
        self.assertNotIn("Scrapingdog", html)
        for unreleased_feature in ("Ad Pixel Check", "Bulk CSV Enrichment", "Webhook/API Export"):
            self.assertNotIn(unreleased_feature, html)
        for expected in [
            "Search credits cover discovery work.",
            "Each selected industry adds its own charge.",
            "Local and hiring discovery share the same credit balance.",
            "The total charge scales with selected roles and page depth",
            "70% of the search charge is refunded as search credits, not cash",
            "30% covers processing.",
            "the whole completed submission produces zero new leads",
            "Validation failures and searches that fail before returning results are not charged.",
            "Monthly and annual plans and credit top-ups are one-time purchases",
            "one-time purchases with no automatic renewal",
            "Solo includes Google Maps and Yelp discovery.",
            "Small Team adds Facebook, Instagram, LinkedIn company pages, and preset hiring searches.",
            "Enterprise also lets you create custom hiring services.",
            "members share the owner's search credit balance and daily submission cap.",
            '<summary>See charges, plan access, and refund details</summary>',
        ]:
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
        for obsolete in ["lead credits", "Lead Credits", "per lead", "per-lead"]:
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, html)
        self.assertNotIn("when payments are configured", html)
        self.assertNotIn("remain visible but locked", html)
        cards = re.findall(r'<article class="plan-card[^"]*".*?</article>', html, re.S)
        for card, industries, allowance in zip(cards, [3, 5, 10], [75, 250, 750]):
            self.assertIn(f"Up to {industries} industries per submission", card)
            self.assertIn(f"{allowance} local industry searches before refunds", card)
        self.assertIn("Google Maps and Yelp discovery.", cards[0])
        self.assertIn("Upgrade to Small Team to unlock Facebook, Instagram, LinkedIn company pages", cards[0])
        self.assertIn("Upgrade to Enterprise to add custom hiring services.", cards[1])
        self.assertIn("custom hiring service creation.", cards[2])

    def test_customer_discovery_copy_hides_provider_costs_and_request_details(self):
        response = self.client.get("/discovery")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for internal_detail in ("Scrapingdog", "API requests", "API credits", "indexed by Google via"):
            with self.subTest(internal_detail=internal_detail):
                self.assertNotIn(internal_detail, html)
        self.assertIn("More pages use more search credits", html)

    def test_discovery_explanations_are_collapsible_and_workspace_copy_is_plain(self):
        html = self.client.get("/discovery").get_data(as_text=True)
        for summary in (
            "How the search radius works",
            "About LinkedIn company searches",
            "How industries are matched",
            "How service matching works",
            "How country matching works",
            "About pages and search estimates",
        ):
            with self.subTest(summary=summary):
                self.assertIn(f"<summary>{summary}</summary>", html)
        for obsolete in (
            "LEAD ENGINE",
            "Interactive Target Range Matrix",
            "Inspector Panel",
            "No matching pipeline leads captured inside this view index.",
            "Select any lead row to engage workstation nodes.",
            "Shifting data view scope...",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, html)

    def test_plan_locked_controls_have_distinct_upgrade_styles(self):
        repository = Path(__file__).resolve().parents[1]
        dashboard_css = (repository / "static" / "dashboard.css").read_text(encoding="utf-8")
        html = self.client.get("/discovery").get_data(as_text=True)

        self.assertIn('id="discovery-source-options"', html)
        self.assertIn('class="source-option flex items-center', html)
        self.assertIn(".source-option.plan-locked", dashboard_css)
        self.assertIn(".plan-locked-panel", dashboard_css)
        self.assertIn(".plan-upgrade-link", dashboard_css)
        self.assertRegex(
            dashboard_css,
            r"#btn-discovery-submit\.is-plan-locked,\s*#btn-discovery-submit:disabled",
        )
        self.assertIn("#custom-service-editor.plan-locked > summary::after", dashboard_css)

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
                    self.assertIn("<title>User Management | diskubre</title>", html)
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
        self.assertRegex(html, r'id="nav-user-management" class="nav-link[^"]*" hidden')
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
