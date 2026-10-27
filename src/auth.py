"""Account creation, sign-in, and the request guard that isolates user data.

Passwords are hashed with Werkzeug (scrypt by default) and never stored or
logged in clear text. Sign-in failures are deliberately indistinguishable from
unknown-email failures, and sign-up conflicts use a message that does not
confirm the address is registered.
Repeated failures are throttled per email and network by the sign-in route
(see rate_limit.py), never by locking the account itself.
"""

import functools
import re
import unicodedata
from urllib.parse import urlsplit

import psycopg2
from flask import jsonify, redirect, request, session, url_for
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

SESSION_USER_KEY = "user_id"
# Raising users.session_version signs out every session issued before it.
SESSION_VERSION_KEY = "session_version"
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

# Paths reachable without a session. Everything else requires sign-in.
PUBLIC_ENDPOINTS = frozenset({
    "home", "plans", "sign_in", "register", "static", "silence_favicon", "silence_chrome_devtools",
    "csp_report",
    "account.verify_email_page", "account.verify_email",
    "account.forgot_password", "account.reset_password",
    "teams.invitation_landing",
    "purchases.paymongo_webhook",
})

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

GENERIC_SIGN_IN_ERROR = "That email and password combination is not recognized."
# Deliberately does not say whether the address is registered.
REGISTRATION_CONFLICT_ERROR = ("We couldn't create an account with that email. "
                               "If it's yours, sign in or reset your password.")


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


def create_user(dsn: str, email: object, password: object, display_name: object = None,
                network_hash: str | None = None) -> dict:
    """Creates an unverified account. The free trial starts at email verification."""
    address = normalize_email(email)
    secret = validate_password(password)
    name = validate_display_name(display_name)
    try:
        with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, email_key, password_hash, display_name, signup_network_hash)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id, email, display_name, role, session_version;
                    """,
                    (address, address.casefold(), generate_password_hash(secret), name, network_hash),
                )
                created = cur.fetchone()
                # A duplicate is handled without a database error, so the server
                # never logs the failed statement (which carries the password hash).
                if created is None:
                    raise AuthError(REGISTRATION_CONFLICT_ERROR)
                from billing import lock_wallet
                lock_wallet(cur, created["id"])
            conn.commit()
    except psycopg2.errors.UniqueViolation as error:
        raise AuthError(REGISTRATION_CONFLICT_ERROR) from error
    return dict(created)


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
                """SELECT id, email, display_name, role, is_active, password_hash, session_version
                   FROM users WHERE email_key = %s;""",
                (key,),
            )
            user = cur.fetchone()

            if user is None:
                # Spend comparable time so timing does not disclose existence.
                check_password_hash(generate_password_hash("placeholder"), password)
                raise AuthError(GENERIC_SIGN_IN_ERROR)

            if not check_password_hash(user["password_hash"], password):
                raise AuthError(GENERIC_SIGN_IN_ERROR)

            if not user["is_active"]:
                raise AuthError("That account has been deactivated.")

            cur.execute("UPDATE users SET last_login_at = NOW() WHERE id = %s;", (user["id"],))
        conn.commit()
    return {
        "id": user["id"], "email": user["email"],
        "display_name": user["display_name"], "role": user["role"],
        "session_version": user["session_version"],
    }


def load_user(dsn: str, user_id: object) -> dict | None:
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, email, display_name, role, session_version,
                          email_verified_at IS NOT NULL AS email_verified FROM users
                   WHERE id = %s AND is_active;""",
                (user_id,),
            )
            user = cur.fetchone()
    return dict(user) if user else None


def change_password(dsn: str, user_id: int, current: object, new: object, confirm: object) -> int:
    """Replaces the password and returns the new session version.

    Raising the version signs out every other session; the caller moves the
    current session to the new version so this browser stays signed in.
    """
    if not isinstance(current, str) or not current:
        raise AuthError("Enter your current password.")
    secret = validate_password(new)
    if new != confirm:
        raise AuthError("Both new passwords must match.")
    if new == current:
        raise AuthError("Choose a password you are not already using.")
    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s AND is_active FOR UPDATE;",
                        (user_id,))
            user = cur.fetchone()
            if user is None or not check_password_hash(user["password_hash"], current):
                raise AuthError("Your current password is not correct.")
            cur.execute(
                """UPDATE users SET password_hash = %s, session_version = session_version + 1
                   WHERE id = %s RETURNING session_version;""",
                (generate_password_hash(secret), user_id),
            )
            version = cur.fetchone()["session_version"]
        conn.commit()
    return version


def end_all_sessions(dsn: str, user_id: int) -> None:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET session_version = session_version + 1 WHERE id = %s;",
                        (user_id,))
        conn.commit()


def sign_in_session(user: dict) -> None:
    session.clear()
    session[SESSION_USER_KEY] = user["id"]
    session[SESSION_VERSION_KEY] = user.get("session_version", 1)
    session.permanent = True


def session_is_current(user: dict) -> bool:
    """False once the account's sessions were revoked after this one began."""
    version = user.get("session_version", 1)
    stored = session.get(SESSION_VERSION_KEY)
    if stored is None:
        # Sessions issued before versioning existed are adopted, but only while
        # the account has never revoked sessions; afterwards they predate it.
        if version != 1:
            return False
        session[SESSION_VERSION_KEY] = version
        return True
    return stored == version


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
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return fallback
    # Browsers read "\" as "/", so "/\evil.test" would be protocol-relative.
    if "\\" in value or "%5c" in value.lower():
        return fallback
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return fallback
    return value
