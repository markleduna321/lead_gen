"""Fixed-window rate limits shared by every app worker.

Counters live in PostgreSQL so limits hold across gunicorn workers and
restarts. Keys are hashed, so emails and IP addresses are never stored.
"""

from dataclasses import dataclass
import hashlib
import math
import random
import threading
import time

import psycopg2


@dataclass(frozen=True)
class Limit:
    name: str
    max_hits: int
    window: int
    message: str


LOGIN_IP = Limit("login-ip", 20, 15 * 60,
                 "Too many sign-in attempts from this network. Try again in {wait}.")
LOGIN_FAILURES = Limit("login-fail", 8, 15 * 60,
                       "Too many failed sign-in attempts. Try again in {wait}.")
REGISTER_IP = Limit("register-ip", 5, 60 * 60,
                    "Too many sign-up attempts from this network. Try again in {wait}.")
SMTP_TEST = Limit("smtp-test", 5, 10 * 60,
                  "Too many test messages. Try again in {wait}.")
DISPATCH = Limit("dispatch", 20, 60 * 60,
                 "Hourly email sending limit reached. Try again in {wait}.")
PITCH = Limit("pitch", 10, 60,
              "You're drafting pitches too quickly. Try again in {wait}.")
QUOTE = Limit("quote", 60, 60,
              "Too many price checks. Try again in {wait}.")
CSP_REPORT = Limit("csp-report", 30, 60, "Too many reports.")
PASSWORD_CHANGE = Limit("password-change", 5, 15 * 60,
                        "Too many password change attempts. Try again in {wait}.")
VERIFY_RESEND = Limit("verify-resend", 3, 60 * 60,
                      "You've asked for several verification emails. Try again in {wait}.")
VERIFY_SUBMIT = Limit("verify-submit", 30, 60 * 60,
                      "Too many verification attempts from this network. Try again in {wait}.")
RESET_REQUEST_IP = Limit("reset-request-ip", 5, 60 * 60,
                         "Too many password reset requests from this network. Try again in {wait}.")
RESET_REQUEST_EMAIL = Limit("reset-request-email", 3, 60 * 60,
                            "Too many password reset requests for this email. Try again in {wait}.")
RESET_SUBMIT = Limit("reset-submit", 10, 15 * 60,
                     "Too many password reset attempts. Try again in {wait}.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limit_hits (
    bucket TEXT PRIMARY KEY,
    hits INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_expires ON rate_limit_hits (expires_at);
"""


def describe_wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


class RateLimited(Exception):
    def __init__(self, limit: Limit, retry_after: int):
        self.limit, self.retry_after = limit, retry_after
        super().__init__(limit.message.format(wait=describe_wait(retry_after)))


class MemoryStore:
    """Single-process store for tests and database-less local runs."""

    def __init__(self, clock=time.time):
        self.clock, self.lock, self.buckets = clock, threading.Lock(), {}

    def increment(self, bucket, expires_at):
        with self.lock:
            now = self.clock()
            self.buckets = {k: v for k, v in self.buckets.items() if v[1] > now}
            hits = self.buckets.get(bucket, (0, expires_at))[0] + 1
            self.buckets[bucket] = (hits, expires_at)
            return hits

    def peek(self, bucket):
        with self.lock:
            hits, expires_at = self.buckets.get(bucket, (0, 0))
            return hits if expires_at > self.clock() else 0

    def clear(self, bucket):
        with self.lock:
            self.buckets.pop(bucket, None)


class PostgresStore:
    CLEANUP_CHANCE = 0.01

    def __init__(self, dsn):
        self.dsn = dsn

    def increment(self, bucket, expires_at):
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO rate_limit_hits (bucket, hits, expires_at)
                       VALUES (%s, 1, to_timestamp(%s))
                       ON CONFLICT (bucket) DO UPDATE SET hits = rate_limit_hits.hits + 1
                       RETURNING hits;""",
                    (bucket, expires_at),
                )
                hits = cur.fetchone()[0]
                if random.random() < self.CLEANUP_CHANCE:
                    cur.execute("DELETE FROM rate_limit_hits WHERE expires_at < NOW();")
            conn.commit()
        return hits

    def peek(self, bucket):
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT hits FROM rate_limit_hits WHERE bucket = %s AND expires_at > NOW();",
                            (bucket,))
                row = cur.fetchone()
        return row[0] if row else 0

    def clear(self, bucket):
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM rate_limit_hits WHERE bucket = %s;", (bucket,))
            conn.commit()


class Limiter:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def _window(self, limit, key):
        now = self.clock()
        index = int(now // limit.window)
        bucket = hashlib.sha256(f"{limit.name}|{key}|{index}".encode()).hexdigest()
        expires_at = (index + 1) * limit.window
        return bucket, expires_at, max(1, math.ceil(expires_at - now))

    def hit(self, limit: Limit, key) -> None:
        """Counts an attempt and raises RateLimited once the window is over its limit."""
        bucket, expires_at, retry_after = self._window(limit, key)
        if self.store.increment(bucket, expires_at) > limit.max_hits:
            raise RateLimited(limit, retry_after)

    def check(self, limit: Limit, key) -> None:
        """Raises RateLimited if the window is already full, without counting."""
        bucket, _, retry_after = self._window(limit, key)
        if self.store.peek(bucket) >= limit.max_hits:
            raise RateLimited(limit, retry_after)

    def record(self, limit: Limit, key) -> None:
        bucket, expires_at, _ = self._window(limit, key)
        self.store.increment(bucket, expires_at)

    def reset(self, limit: Limit, key) -> None:
        self.store.clear(self._window(limit, key)[0])
