"""Email verification, password reset links, and one free trial per network.

Tokens are random, single-use, and stored only as SHA-256 hashes. Links carry
them in the URL fragment, so they never reach server or proxy access logs and
mail scanners that prefetch links cannot consume them.

The sign-up network is stored only as an HMAC (keyed by TRIAL_IP_HASH_SECRET)
of the IPv4 address or IPv6 /64, never as a raw address. Local and desktop
runs skip the network rule because every user arrives from 127.0.0.1.
"""

from datetime import timedelta
import hashlib
import hmac
import ipaddress
import os
import secrets

from werkzeug.security import generate_password_hash

import app_env
import billing
import system_mail
from auth import AuthError, email_key, validate_password

VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
INVALID_LINK = "This link is invalid or has expired. Request a new one."
OUTCOME_MESSAGES = {
    "started": "Your email is verified and your 7-day free trial has started with 30 search credits.",
    "network_used": ("Your email is verified. A free trial has already been used on this network, "
                     "so choose a plan to start searching."),
    "not_eligible": "Your email is verified.",
    "unverified": "Your email is verified.",
    "already_verified": "Your email is already verified.",
}

SCHEMA = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'users'
                     AND column_name = 'email_verified_at') THEN
        ALTER TABLE users ADD COLUMN email_verified_at TIMESTAMPTZ;
        -- Accounts created before verification existed are trusted as they are.
        UPDATE users SET email_verified_at = COALESCE(created_at::timestamptz, NOW());
    END IF;
END $$;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_network_hash TEXT;
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('verify','reset')),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS auth_tokens_user ON auth_tokens(user_id, purpose);
CREATE TABLE IF NOT EXISTS trial_network_claims (
    network_hash TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def check_hosted_configuration(environ=os.environ) -> None:
    """Hosted deployments must be able to send links and hash networks."""
    env = app_env.resolve_app_env(environ)
    if app_env.is_development(env):
        return
    if not (environ.get("TRIAL_IP_HASH_SECRET") or "").strip():
        raise app_env.StartupConfigError(
            f"TRIAL_IP_HASH_SECRET must be set when APP_ENV is {env!r}. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\"")
    try:
        system_mail.configuration()
    except system_mail.SystemMailUnavailable as error:
        raise app_env.StartupConfigError(
            f"{error} Email verification and password resets need it when APP_ENV is {env!r}.") from error


def network_key(ip) -> str:
    """IPv4 address, or the /64 for IPv6 (one household or phone gets a whole /64)."""
    try:
        address = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return str(ip)
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def network_hash(ip, environ=os.environ) -> str | None:
    """None means the network rule does not apply (local and desktop)."""
    env = app_env.resolve_app_env(environ)
    if app_env.is_development(env):
        return None
    secret = (environ.get("TRIAL_IP_HASH_SECRET") or "").strip()
    if not secret:
        raise app_env.StartupConfigError("TRIAL_IP_HASH_SECRET is not set.")
    return hmac.new(secret.encode(), network_key(ip).encode(), hashlib.sha256).hexdigest()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _issue(cur, user_id: int, purpose: str) -> str:
    cur.execute("DELETE FROM auth_tokens WHERE expires_at < NOW() - INTERVAL '7 days'")
    if purpose == "reset":
        # Only the newest reset link works.
        cur.execute("""UPDATE auth_tokens SET used_at=NOW()
                       WHERE user_id=%s AND purpose='reset' AND used_at IS NULL""", (user_id,))
    token = secrets.token_urlsafe(32)
    cur.execute("""INSERT INTO auth_tokens(token_hash,user_id,purpose,expires_at)
                   VALUES (%s,%s,%s,NOW()+%s)""",
                (_digest(token), user_id, purpose, VERIFY_TTL if purpose == "verify" else RESET_TTL))
    return token


def _consume(cur, token, purpose: str) -> int | None:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        return None
    cur.execute("""SELECT t.user_id FROM auth_tokens t JOIN users u ON u.id=t.user_id
                   WHERE t.token_hash=%s AND t.purpose=%s AND t.used_at IS NULL
                     AND t.expires_at > NOW() AND u.is_active
                   FOR UPDATE OF t""", (_digest(token), purpose))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("UPDATE auth_tokens SET used_at=NOW() WHERE token_hash=%s", (_digest(token),))
    return row["user_id"]


def _grant_after_verification(cur, user_id: int) -> str:
    wallet = billing.lock_wallet(cur, user_id)
    return billing.grant_trial(cur, wallet, user_id, "New account free trial")


def send_verification(dsn: str, user_id: int, base: str) -> bool:
    """Emails a fresh verification link. False if the address is already verified."""
    with billing.transaction(dsn) as cur:
        cur.execute("SELECT email, email_verified_at FROM users WHERE id=%s AND is_active", (user_id,))
        user = cur.fetchone()
        if user is None or user["email_verified_at"] is not None:
            return False
        token = _issue(cur, user_id, "verify")
    system_mail.send(
        user["email"], "Verify your AsuraTECH email",
        "Confirm this email address to finish setting up your AsuraTECH account:\n"
        f"{base}/verify-email#{token}\n\n"
        "The link expires in 24 hours. New accounts get a 7-day free trial once verified "
        "(one per network). Ignore this email if you did not create an account.")
    return True


def verify_email(dsn: str, token) -> str:
    """Marks the address verified and starts the trial when allowed. Returns an outcome key."""
    with billing.transaction(dsn) as cur:
        user_id = _consume(cur, token, "verify")
        if user_id is None:
            raise AuthError(INVALID_LINK)
        cur.execute("""UPDATE users SET email_verified_at=NOW()
                       WHERE id=%s AND email_verified_at IS NULL RETURNING id""", (user_id,))
        if cur.fetchone() is None:
            return "already_verified"
        return _grant_after_verification(cur, user_id)


def request_password_reset(dsn: str, address, base: str) -> None:
    """Sends a reset link if the account exists. Never reveals whether it does."""
    try:
        key = email_key(address)
    except AuthError:
        return
    with billing.transaction(dsn) as cur:
        cur.execute("SELECT id, email FROM users WHERE email_key=%s AND is_active", (key,))
        user = cur.fetchone()
        if user is None:
            return
        token = _issue(cur, user["id"], "reset")
    system_mail.send(
        user["email"], "Reset your AsuraTECH password",
        "Someone asked to reset the password for this AsuraTECH account.\n\n"
        f"Choose a new password here:\n{base}/reset-password#{token}\n\n"
        "The link expires in 1 hour and works once. Resetting signs out every device. "
        "If you did not ask for this, ignore this email; your password stays the same.")


def reset_password(dsn: str, token, new, confirm) -> None:
    secret = validate_password(new)
    if new != confirm:
        raise AuthError("Both passwords must match.")
    with billing.transaction(dsn) as cur:
        user_id = _consume(cur, token, "reset")
        if user_id is None:
            raise AuthError(INVALID_LINK)
        cur.execute("""UPDATE users SET password_hash=%s, session_version=session_version+1
                       WHERE id=%s""", (generate_password_hash(secret), user_id))
        cur.execute("""UPDATE auth_tokens SET used_at=NOW()
                       WHERE user_id=%s AND purpose='reset' AND used_at IS NULL""", (user_id,))
        # The link reached this inbox, so the address is proven too.
        cur.execute("""UPDATE users SET email_verified_at=NOW()
                       WHERE id=%s AND email_verified_at IS NULL RETURNING id""", (user_id,))
        if cur.fetchone() is not None:
            _grant_after_verification(cur, user_id)
