"""Startup environment rules (APP_ENV and FLASK_SECRET_KEY).

An unset APP_ENV means production, so a deploy that forgets the variable gets
Secure cookies, HSTS and a mandatory secret key instead of development
defaults. Only local and desktop (127.0.0.1 over HTTP) relax those rules.
"""

DEVELOPMENT_ENVS = frozenset({"local", "desktop"})
HOSTED_ENVS = frozenset({"production", "staging"})
ALLOWED_ENVS = DEVELOPMENT_ENVS | HOSTED_ENVS
DEFAULT_ENV = "production"
DEV_SECRET_KEY = "local-development-only-key-do-not-use-when-hosted"


class StartupConfigError(RuntimeError):
    pass


def resolve_app_env(environ) -> str:
    raw = (environ.get("APP_ENV") or "").strip().lower()
    if not raw:
        return DEFAULT_ENV
    if raw not in ALLOWED_ENVS:
        raise StartupConfigError(
            f"APP_ENV={raw!r} is not recognised. Use one of: "
            + ", ".join(sorted(ALLOWED_ENVS)) + "."
        )
    return raw


def is_development(app_env: str) -> bool:
    return app_env in DEVELOPMENT_ENVS


def resolve_secret_key(app_env: str, environ) -> tuple[str, bool]:
    """Returns (key, used_development_fallback)."""
    secret = (environ.get("FLASK_SECRET_KEY") or "").strip()
    if secret:
        return secret, False
    if is_development(app_env):
        return DEV_SECRET_KEY, True
    raise StartupConfigError(
        f"FLASK_SECRET_KEY must be set when APP_ENV is {app_env!r}"
        + (" (APP_ENV is unset, so production rules apply; set APP_ENV=local for development)"
           if not (environ.get("APP_ENV") or "").strip() else "")
        + ". Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )
