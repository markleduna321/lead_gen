"""Account creation, sign-in, and the request guard that isolates user data.

Passwords are hashed with Werkzeug (scrypt by default) and never stored or
logged in clear text. Sign-in failures are deliberately indistinguishable from
unknown-email failures so the form cannot be used to enumerate accounts.
"""

import functools
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import psycopg2
from flask import jsonify, redirect, request, session, url_for
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

SESSION_USER_KEY = "user_id"
MAX_FAILED_LOGINS = 8
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

# Paths reachable without a session. Everything else requires sign-in.
PUBLIC_ENDPOINTS = frozenset({
    "sign_in", "register", "static", "silence_favicon", "silence_chrome_devtools",
})

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

GENERIC_SIGN_IN_ERROR = "That email and password combination is not recognized."


class AuthError(ValueError):
    """Raised for any user-correctable credential or registration problem."""


def normalize_email(value: object) -> str:
    if not isinstance(value, str):
        raise AuthError("Enter a valid email address.")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if len(cleaned) > 255 or not _EMAIL_PATTERN.match(cleaned):
        raise AuthError("Enter a valid email address.")
    return cleaned


def email_key(value: str) -> str:
    return normalize_email(value).casefold()


def validate_password(value: object) -> str:
    if not isinstance(value, str):
        raise AuthError("Enter a password.")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise AuthError("Your password cannot contain control characters.")
    if not MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH:
        raise AuthError(
            f"Use a password between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters."
        )
    return value


def validate_display_name(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AuthError("Enter a valid name.")
    cleaned = " ".join(value.split())
    if len(cleaned) > 120:
        raise AuthError("Your name must be 120 characters or fewer.")
    return cleaned or None


def create_user(dsn: str, email: object, password: object, display_name: object = None) -> dict:
    address = normalize_email(email)
    secret = validate_password(password)
    name = validate_display_name(display_name)
    try:
        with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, email_key, password_hash, display_name)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, email, display_name, role;
                    """,
                    (address, address.casefold(), generate_password_hash(secret), name),
                )
                created = cur.fetchone()
            conn.commit()
    except psycopg2.errors.UniqueViolation as error:
        raise AuthError("An account already exists for that email address.") from error
    return dict(created)


def _lockout_remaining(locked_until) -> int:
    if locked_until is None:
        return 0
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    remaining = (locked_until - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining // 60) + 1) if remaining > 0 else 0


def authenticate(dsn: str, email: object, password: object) -> dict:
    """Returns the signed-in user, or raises AuthError with a generic message."""
    try:
        key = email_key(email)
    except AuthError:
        # Never reveal that the address was malformed rather than unknown.
        raise AuthError(GENERIC_SIGN_IN_ERROR) from None
    if not isinstance(password, str) or not password:
        raise AuthError(GENERIC_SIGN_IN_ERROR)

    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, email, display_name, role, is_active, password_hash,
                          failed_login_count, locked_until
                   FROM users WHERE email_key = %s;""",
                (key,),
            )
            user = cur.fetchone()

            if user is None:
                # Spend comparable time so timing does not disclose existence.
                check_password_hash(generate_password_hash("placeholder"), password)
                raise AuthError(GENERIC_SIGN_IN_ERROR)

            minutes = _lockout_remaining(user["locked_until"])
            if minutes:
                raise AuthError(
                    f"Too many failed attempts. Try again in {minutes} minute(s)."
                )

            if not check_password_hash(user["password_hash"], password):
                attempts = user["failed_login_count"] + 1
                locked = (
                    datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                    if attempts >= MAX_FAILED_LOGINS else None
                )
                cur.execute(
                    "UPDATE users SET failed_login_count = %s, locked_until = %s WHERE id = %s;",
                    (attempts, locked, user["id"]),
                )
                conn.commit()
                raise AuthError(GENERIC_SIGN_IN_ERROR)

            if not user["is_active"]:
                raise AuthError("That account has been deactivated.")

            cur.execute(
                """UPDATE users SET failed_login_count = 0, locked_until = NULL,
                       last_login_at = NOW() WHERE id = %s;""",
                (user["id"],),
            )
        conn.commit()
    return {
        "id": user["id"], "email": user["email"],
        "display_name": user["display_name"], "role": user["role"],
    }


def load_user(dsn: str, user_id: object) -> dict | None:
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, email, display_name, role FROM users
                   WHERE id = %s AND is_active;""",
                (user_id,),
            )
            user = cur.fetchone()
    return dict(user) if user else None


def sign_in_session(user: dict) -> None:
    session.clear()
    session[SESSION_USER_KEY] = user["id"]
    session.permanent = True


def sign_out_session() -> None:
    session.clear()


def current_user_id() -> int | None:
    user_id = session.get(SESSION_USER_KEY)
    return user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else None


def wants_json() -> bool:
    """API callers must receive JSON 401s, never an HTML sign-in page.

    The dashboard is almost entirely fetch()-driven; returning HTML would fail
    in response.json() instead of sending the operator to the sign-in screen.
    """
    return request.path.startswith("/api/")


def unauthorized_response():
    if wants_json():
        return jsonify({
            "status": "error", "code": "auth_required",
            "message": "Your session has ended. Sign in again to continue.",
        }), 401
    return redirect(url_for("sign_in", next=request.full_path if request.query_string else request.path))


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user_id() is None:
            return unauthorized_response()
        return view(*args, **kwargs)
    return wrapped


def safe_redirect_target(value: object, fallback: str = "/") -> str:
    """Only allow same-site relative paths, blocking open-redirect attempts."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return fallback
    if any(character.isspace() or ord(character) < 32 for character in value):
        return fallback
    return value
