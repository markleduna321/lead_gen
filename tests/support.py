"""Shared helpers for the authenticated test suites.

Not named test_* so unittest discovery does not collect it as a test module.
"""

from unittest.mock import patch

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


def sign_in(client, user_id: int = QA_USER_ID):
    """Signs a test client in without touching the database.

    Writing the session directly keeps the suites offline: the request guard
    only needs the user id, so no users table read is involved.
    """
    _ensure_account_stub()
    with client.session_transaction() as transaction:
        transaction["user_id"] = user_id
    return client


def signed_in_client(app, user_id: int = QA_USER_ID):
    return sign_in(app.test_client(), user_id)
