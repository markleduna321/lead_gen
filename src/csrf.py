"""CSRF protection for every state-changing request.

One ``before_request`` check covers the whole app, so a new POST route is
protected by default. Browser code sends the session token in the
``X-CSRF-Token`` header (the fetch wrapper in workspace.js adds it to every
/api call); HTML forms send it in a hidden ``csrf_token`` field. As a second
layer, a request whose Origin or Referer names another site is rejected even
if it somehow carries a valid token.
"""

import os
import secrets
from urllib.parse import urlsplit

from flask import jsonify, redirect, render_template, request, session, url_for

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FIELD = "csrf_token"
HEADER = "X-CSRF-Token"
EXPIRED_MESSAGE = "Your page has expired. Reload before trying again."
FORM_EXPIRED_MESSAGE = "Your page expired. Please try again."

# The PayMongo webhook proves itself with a signature, and browsers cannot add
# tokens to CSP reports (which are only logged).
EXEMPT_ENDPOINTS = frozenset({"purchases.paymongo_webhook", "csp_report"})
AUTH_FORMS = {"sign_in": "login", "register": "register",
              "account.forgot_password": "forgot", "account.reset_password": "reset"}


def token():
    if FIELD not in session:
        session[FIELD] = secrets.token_urlsafe(32)
    return session[FIELD]


def _allowed_hosts():
    hosts = {request.host.lower()}
    # Behind a proxy that rewrites Host, the public address comes from config.
    public = urlsplit(os.getenv("APP_BASE_URL", "").strip())
    if public.netloc:
        hosts.add(public.netloc.lower())
    return hosts


def _cross_site():
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return False
    if source == "null":
        return True
    return urlsplit(source).netloc.lower() not in _allowed_hosts()


def _token_valid():
    expected = session.get(FIELD)
    supplied = request.headers.get(HEADER) or request.form.get(FIELD) or ""
    return bool(expected) and secrets.compare_digest(expected, supplied)


def _rejected():
    endpoint = request.endpoint
    if endpoint in AUTH_FORMS:
        from auth import safe_redirect_target
        mode = AUTH_FORMS[endpoint]
        form = request.form
        # The reset token is kept so the person can simply submit again.
        return render_template("auth.html", mode=mode, error=FORM_EXPIRED_MESSAGE,
                               email=form.get("email", ""), display_name=form.get("display_name", ""),
                               token=form.get("token", "") if mode == "reset" else "",
                               next=safe_redirect_target(form.get("next"))), 403
    if endpoint == "sign_out":
        # Never sign out on a forged or stale request; return to the workspace.
        return redirect(url_for("serve_dashboard", page="overview"))
    response = jsonify(status="error", code="csrf_failed", message=EXPIRED_MESSAGE)
    response.status_code = 403
    return response


def protect():
    # Unmatched requests (404/405) reach no view, so let routing report them.
    if (request.method in SAFE_METHODS or request.endpoint in EXEMPT_ENDPOINTS
            or request.routing_exception is not None):
        return None
    if _cross_site() or not _token_valid():
        return _rejected()
    return None


def install(app):
    """Must run after the sign-in guard, so signed-out requests get 401 first."""
    app.before_request(protect)
    app.context_processor(lambda: {"csrf_token": token})
