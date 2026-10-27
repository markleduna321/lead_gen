import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import app_env
from app_env import StartupConfigError

KEY = "k" * 48


class ResolveAppEnvTests(unittest.TestCase):
    def test_unset_or_blank_means_production(self):
        for environ in [{}, {"APP_ENV": ""}, {"APP_ENV": "   "}]:
            with self.subTest(environ=environ):
                self.assertEqual(app_env.resolve_app_env(environ), "production")

    def test_known_values_are_normalised(self):
        for raw, expected in [("local", "local"), (" Desktop ", "desktop"),
                              ("PRODUCTION", "production"), ("staging", "staging")]:
            with self.subTest(raw=raw):
                self.assertEqual(app_env.resolve_app_env({"APP_ENV": raw}), expected)

    def test_unknown_values_refuse_to_start(self):
        for raw in ["prod", "dev", "development", "test", "locale"]:
            with self.subTest(raw=raw), self.assertRaisesRegex(StartupConfigError, "not recognised"):
                app_env.resolve_app_env({"APP_ENV": raw})

    def test_only_local_and_desktop_are_development(self):
        self.assertTrue(app_env.is_development("local"))
        self.assertTrue(app_env.is_development("desktop"))
        self.assertFalse(app_env.is_development("production"))
        self.assertFalse(app_env.is_development("staging"))


class ResolveSecretKeyTests(unittest.TestCase):
    def test_configured_key_is_used_everywhere(self):
        for env in sorted(app_env.ALLOWED_ENVS):
            with self.subTest(env=env):
                self.assertEqual(app_env.resolve_secret_key(env, {"FLASK_SECRET_KEY": f" {KEY} "}),
                                 (KEY, False))

    def test_development_falls_back_to_dev_key(self):
        for env in ["local", "desktop"]:
            with self.subTest(env=env):
                self.assertEqual(app_env.resolve_secret_key(env, {}), (app_env.DEV_SECRET_KEY, True))

    def test_hosted_modes_require_a_key(self):
        for env in ["production", "staging"]:
            with self.subTest(env=env), self.assertRaisesRegex(StartupConfigError, "FLASK_SECRET_KEY must be set"):
                app_env.resolve_secret_key(env, {"APP_ENV": env, "FLASK_SECRET_KEY": "  "})

    def test_unset_app_env_error_explains_the_default(self):
        with self.assertRaisesRegex(StartupConfigError, "APP_ENV is unset.*APP_ENV=local"):
            app_env.resolve_secret_key("production", {})


IMPORT_SCRIPT = """
import contextlib, io, json, sys
from unittest.mock import patch
sys.path.insert(0, "src")
outcome = None
with patch("dotenv.load_dotenv"), \\
     patch("psycopg2.connect", side_effect=RuntimeError("no database in this test")), \\
     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    try:
        import control_gate_api as api
    except Exception as exc:
        outcome = {"error": type(exc).__name__, "message": str(exc)}
    else:
        client = api.app.test_client()
        hsts = client.get("/login", base_url="https://app.test").headers.get("Strict-Transport-Security")
        outcome = {"env": api.APP_ENV, "secure": api.app.config["SESSION_COOKIE_SECURE"],
                   "dev_key": api.app.secret_key == "local-development-only-key-do-not-use-when-hosted",
                   "hsts": bool(hsts)}
print(json.dumps(outcome))
"""


HOSTED = {"TRIAL_IP_HASH_SECRET": "test-network-secret", "SYSTEM_SMTP_PASSWORD": "fake-key",
          "SYSTEM_SMTP_FROM": "system@example.test", "APP_BASE_URL": "https://app.example.test"}


class AppStartupTests(unittest.TestCase):
    """Imports the real app in a clean interpreter, as a deploy would."""

    def start(self, **env):
        import json
        base = {k: v for k, v in os.environ.items()
                if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONPATH", "PYTHONIOENCODING"}}
        result = subprocess.run([sys.executable, "-B", "-c", IMPORT_SCRIPT], cwd=ROOT,
                                env={**base, **env}, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_unset_app_env_without_key_refuses_to_start(self):
        outcome = self.start()
        self.assertEqual(outcome["error"], "StartupConfigError")
        self.assertIn("FLASK_SECRET_KEY", outcome["message"])

    def test_unset_app_env_with_key_runs_with_production_rules(self):
        self.assertEqual(self.start(FLASK_SECRET_KEY=KEY, **HOSTED),
                         {"env": "production", "secure": True, "dev_key": False, "hsts": True})

    def test_typo_refuses_to_start(self):
        outcome = self.start(APP_ENV="prod", FLASK_SECRET_KEY=KEY)
        self.assertEqual(outcome["error"], "StartupConfigError")

    def test_staging_uses_production_rules(self):
        self.assertEqual(self.start(APP_ENV="staging", FLASK_SECRET_KEY=KEY, **HOSTED),
                         {"env": "staging", "secure": True, "dev_key": False, "hsts": True})

    def test_hosted_without_trial_secret_or_system_mail_refuses_to_start(self):
        for missing, needle in [("TRIAL_IP_HASH_SECRET", "TRIAL_IP_HASH_SECRET"),
                                ("SYSTEM_SMTP_PASSWORD", "SYSTEM_SMTP"), ("APP_BASE_URL", "APP_BASE_URL")]:
            with self.subTest(missing=missing):
                env = {k: v for k, v in HOSTED.items() if k != missing}
                outcome = self.start(APP_ENV="production", FLASK_SECRET_KEY=KEY, **env)
                self.assertEqual(outcome["error"], "StartupConfigError")
                self.assertIn(needle, outcome["message"])

    def test_local_and_desktop_run_without_key(self):
        for env in ["local", "desktop"]:
            with self.subTest(env=env):
                self.assertEqual(self.start(APP_ENV=env),
                                 {"env": env, "secure": False, "dev_key": True, "hsts": False})

    def test_desktop_launcher_forces_desktop_mode(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertLess(source.index("os.environ['APP_ENV'] = 'desktop'"), source.index("from control_gate_api"))


if __name__ == "__main__":
    unittest.main()
