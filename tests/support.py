"""Shared helpers for the authenticated test suites.

Not named test_* so unittest discovery does not collect it as a test module.
"""

import contextlib
import socket
from unittest.mock import Mock, patch
import uuid

from plans import capabilities

QA_USER_ID = 1
QA_USER = {"id": QA_USER_ID, "email": "qa@example.test",
           "display_name": "QA", "role": "member"}


def _stub_account_lookup(dsn, user_id):
    return dict(QA_USER, id=user_id) if user_id == QA_USER_ID else None


_stub_account_lookup.is_qa_stub = True


def _ensure_account_stub():
    """Keeps the account lookup stubbed for signed-in test clients.

    The request guard re-checks the account on every request so deactivated
    users lose access immediately. Two suites call ``patch.stopall()`` in
    cleanup, which would otherwise tear this stub down for every test that
    runs afterwards, so it is re-armed whenever a client signs in.
    """
    import auth
    if getattr(auth.load_user, "is_qa_stub", False):
        return
    patch("auth.load_user", _stub_account_lookup).start()


def _ensure_billing_stub():
    """Legacy discovery fixtures assume an unlimited Enterprise test account.

    Real wallet, authorization and concurrency behavior is covered against
    PostgreSQL in test_team_billing.py, without these fixture stubs.
    """
    import billing
    if getattr(billing.reserve, "is_qa_stub", False):
        return

    def reserve(*args, **kwargs):
        return {"id": str(uuid.uuid4())}, False

    reserve.is_qa_stub = True
    patch("billing.reserve", reserve).start()
    patch("billing.bind_run").start()
    patch("billing.save_progress").start()
    patch("billing.settle").start()
    patch("billing.snapshot", return_value=None).start()
    patch("billing.wallet_summary", return_value={
        "owner": True, "can_manage_team": True, "balance": "7500",
        "plan": capabilities("enterprise", True),
    }).start()


def with_csrf(client, token: str = "qa-csrf-token"):
    """Gives a test client a session CSRF token and sends it on every request."""
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    with client.session_transaction() as transaction:
        transaction["csrf_token"] = token
    return client


def sign_in(client, user_id: int = QA_USER_ID):
    """Signs a test client in without touching the database.

    Writing the session directly keeps the suites offline: the request guard
    only needs the user id, so no users table read is involved.
    """
    _ensure_account_stub()
    _ensure_billing_stub()
    with_csrf(client)
    client.environ_base["HTTP_IDEMPOTENCY_KEY"] = str(uuid.uuid4())
    with client.session_transaction() as transaction:
        transaction["user_id"] = user_id
    return client


def signed_in_client(app, user_id: int = QA_USER_ID):
    return sign_in(app.test_client(), user_id)


class _StreamedPage:
    """Adapts a simple fake response to the streaming interface net_guard.safe_get reads."""

    def __init__(self, response):
        self.status_code = response.status_code
        headers = getattr(response, "headers", None)
        self.headers = headers if isinstance(headers, dict) else {}
        text = getattr(response, "text", "")
        self.content = text.encode("utf-8") if isinstance(text, str) else b""
        self.encoding = "utf-8"

    def iter_content(self, chunk_size=65536):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _public_dns(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@contextlib.contextmanager
def guarded_web(handler):
    """Serves website fetches made through net_guard.safe_get from handler(url, **kwargs)."""
    import net_guard

    with (patch("requests.Session.get",
                side_effect=lambda url, **kwargs: _StreamedPage(handler(url, **kwargs))) as site,
          patch("socket.getaddrinfo", _public_dns),
          patch.object(net_guard, "shared_proxy", return_value=Mock(url="http://127.0.0.1:9"))):
        yield site
