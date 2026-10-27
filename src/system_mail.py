"""Transactional email sent by the app itself (Resend SMTP over TLS).

Used for team invitations, email verification and password resets; user
outreach goes through each account's own mailbox (user_email.py) instead.
On local/desktop without configuration, messages are printed to the server
console so links can still be followed during development.
"""

from email.message import EmailMessage
import os
import smtplib
import ssl
import sys
from urllib.parse import urlsplit

import app_env

RESEND_HOST = "smtp.resend.com"
RESEND_PORT = 465


class SystemMailUnavailable(RuntimeError):
    pass


def configuration() -> tuple[str, str, str]:
    """Returns (api_key, sender, base_url) or raises SystemMailUnavailable."""
    key = os.getenv("SYSTEM_SMTP_PASSWORD", "").strip()
    sender = os.getenv("SYSTEM_SMTP_FROM", "").strip()
    base = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    parsed = urlsplit(base)
    if (not key or not sender or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or parsed.scheme != "https"):
        raise SystemMailUnavailable(
            "System mail is not configured. Set SYSTEM_SMTP_PASSWORD, SYSTEM_SMTP_FROM "
            "and an https APP_BASE_URL.")
    from auth import AuthError, email_key
    try:
        email_key(sender)
    except AuthError as error:
        raise SystemMailUnavailable("SYSTEM_SMTP_FROM must be a valid verified sender address.") from error
    return key, sender, base


def _console_fallback() -> bool:
    return app_env.is_development(app_env.resolve_app_env(os.environ))


def base_url() -> str:
    """Public origin for links in emails."""
    try:
        return configuration()[2]
    except SystemMailUnavailable:
        if not _console_fallback():
            raise
        configured = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
        if configured:
            return configured
        from flask import has_request_context, request
        return request.host_url.rstrip("/") if has_request_context() else "http://127.0.0.1:5000"


def send(recipient: str, subject: str, body: str) -> None:
    try:
        key, sender, _ = configuration()
    except SystemMailUnavailable:
        if not _console_fallback():
            raise
        print(f"[DEV MAIL] To: {recipient}\nSubject: {subject}\n\n{body}\n[/DEV MAIL]", file=sys.stderr)
        return
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, recipient, subject
    message.set_content(body)
    with smtplib.SMTP_SSL(RESEND_HOST, RESEND_PORT, timeout=15, context=ssl.create_default_context()) as smtp:
        smtp.login("resend", key)
        if smtp.send_message(message):
            raise smtplib.SMTPException("Recipient rejected")
