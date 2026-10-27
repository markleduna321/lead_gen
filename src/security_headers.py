"""Browser security headers applied to every response.

The enforced CSP only allows this site plus the map tile, geocoding and R2
screenshot hosts. It still needs 'unsafe-inline' because templates use inline
handlers; a stricter report-only policy measures what a nonce-based CSP would
break, so it can be enforced once those handlers are gone.
"""

from flask import request

from app_env import DEVELOPMENT_ENVS
HSTS_MAX_AGE = 365 * 24 * 60 * 60
CSP_REPORT_PATH = "/api/csp-report"

_SOURCES = {
    "default-src": "'self'",
    "img-src": "'self' data: blob: https://basemaps.cartocdn.com https://*.r2.cloudflarestorage.com",
    "font-src": "'self'",
    "connect-src": "'self' https://photon.komoot.io",
    "object-src": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
    "frame-ancestors": "'none'",
}


def _policy(script_src, extra=()):
    directives = dict(_SOURCES)
    directives["script-src"] = script_src
    directives["style-src"] = "'self' 'unsafe-inline'"
    return "; ".join([f"{name} {value}" for name, value in directives.items()] + list(extra))


ENFORCED_CSP = _policy("'self' 'unsafe-inline'")
REPORT_ONLY_CSP = _policy("'self'", [f"report-uri {CSP_REPORT_PATH}"])

STATIC_HEADERS = {
    "Content-Security-Policy": ENFORCED_CSP,
    "Content-Security-Policy-Report-Only": REPORT_ONLY_CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def install(app, app_env):
    send_hsts = app_env not in DEVELOPMENT_ENVS

    @app.after_request
    def apply_security_headers(response):
        for name, value in STATIC_HEADERS.items():
            # A route that sets a stricter value of its own keeps it.
            response.headers.setdefault(name, value)
        if send_hsts and request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", f"max-age={HSTS_MAX_AGE}")
        return response

    return apply_security_headers
