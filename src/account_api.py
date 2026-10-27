"""Email verification and forgot-password routes. CSRF protection is app-wide."""

import sys
import threading

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

import account_email
import rate_limit
import system_mail
from auth import AuthError, current_user_id, sign_out_session

bp = Blueprint("account", __name__)

RESET_SENT = ("If an account exists for that email, we've sent a link to reset your password. "
              "It expires in 1 hour. Check your spam folder if it doesn't arrive.")
RESET_DONE = "Your password was reset and every device was signed out. Sign in with your new password."


def dsn():
    return current_app.config["BILLING_DSN"]


def limiter():
    return current_app.config["RATE_LIMITER"]


def client_ip():
    return request.remote_addr or "unknown"


def run_in_background(work):
    """Reset mail is sent off the request, so response time doesn't reveal accounts."""
    threading.Thread(target=work, daemon=True).start()


@bp.route("/verify-email")
def verify_email_page():
    return render_template("verify_email.html")


@bp.route("/api/auth/verify-email", methods=["POST"])
def verify_email():
    limiter().hit(rate_limit.VERIFY_SUBMIT, client_ip())
    data = request.get_json(silent=True)
    try:
        outcome = account_email.verify_email(dsn(), data.get("token") if isinstance(data, dict) else None)
    except AuthError as error:
        return jsonify(status="error", code="invalid_link", message=str(error)), 400
    return jsonify(status="success", outcome=outcome, message=account_email.OUTCOME_MESSAGES[outcome])


@bp.route("/api/auth/resend-verification", methods=["POST"])
def resend_verification():
    user_id = current_user_id()
    limiter().hit(rate_limit.VERIFY_RESEND, str(user_id))
    try:
        sent = account_email.send_verification(dsn(), user_id, system_mail.base_url())
    except Exception as error:
        print(f"[AUTH ERROR] Verification email failed: {type(error).__name__}", file=sys.stderr)
        return jsonify(status="error", code="mail_failed",
                       message="We couldn't send the email right now. Try again in a few minutes."), 503
    if not sent:
        return jsonify(status="success", verified=True, message="Your email is already verified.")
    return jsonify(status="success", verified=False,
                   message="Verification email sent. Check your inbox and spam folder.")


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("auth.html", mode="forgot")
    address = request.form.get("email", "")
    try:
        limiter().hit(rate_limit.RESET_REQUEST_IP, client_ip())
        limiter().hit(rate_limit.RESET_REQUEST_EMAIL, " ".join(address.split()).casefold())
    except rate_limit.RateLimited as error:
        return render_template("auth.html", mode="forgot", error=str(error), email=address), 429, \
            {"Retry-After": str(error.retry_after)}
    try:
        base = system_mail.base_url()
    except system_mail.SystemMailUnavailable:
        return render_template("auth.html", mode="forgot", email=address,
                               error="Password reset is unavailable right now. Please try again later."), 503
    database = dsn()

    def send():
        try:
            account_email.request_password_reset(database, address, base)
        except Exception as error:
            print(f"[AUTH ERROR] Password reset email failed: {type(error).__name__}", file=sys.stderr)

    run_in_background(send)
    return render_template("auth.html", mode="forgot", sent=True, notice=RESET_SENT)


@bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if request.method == "GET":
        return render_template("auth.html", mode="reset")
    form = request.form
    try:
        limiter().hit(rate_limit.RESET_SUBMIT, client_ip())
        account_email.reset_password(dsn(), form.get("token"), form.get("password"),
                                     form.get("confirm_password"))
    except rate_limit.RateLimited as error:
        return render_template("auth.html", mode="reset", error=str(error), token=form.get("token", "")), \
            429, {"Retry-After": str(error.retry_after)}
    except AuthError as error:
        return render_template("auth.html", mode="reset", error=str(error), token=form.get("token", "")), 400
    sign_out_session()
    return redirect(url_for("sign_in", reset="done"))
