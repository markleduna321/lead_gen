"""Transactional owner wallets. Membership never changes lead ownership."""

import calendar
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import threading
import uuid

import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values

from plans import PITCH_PRICE, PLANS, PlanError, ZERO_RESULT_REFUND, capabilities, quote_search

CONTEXT = threading.local()
SCHEMA = """
CREATE TABLE IF NOT EXISTS team_wallets (
    id BIGSERIAL PRIMARY KEY,
    owner_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    plan_id TEXT NOT NULL DEFAULT 'unassigned'
        CHECK (plan_id IN ('unassigned','trial','solo','small_team','enterprise')),
    balance NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    next_grant_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS team_members (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    wallet_id BIGINT NOT NULL REFERENCES team_wallets(id),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS team_invites (
    id UUID PRIMARY KEY,
    wallet_id BIGINT NOT NULL REFERENCES team_wallets(id),
    email_key TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending','accepted','revoked','expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS team_pending_email
    ON team_invites(wallet_id, email_key) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS paid_discovery_runs (
    id UUID PRIMARY KEY,
    wallet_id BIGINT NOT NULL REFERENCES team_wallets(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    request_key UUID NOT NULL,
    fingerprint TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('local','hiring')),
    charge NUMERIC(14,2) NOT NULL CHECK (charge > 0),
    refund NUMERIC(14,2) NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK (state IN ('reserved','running','succeeded','failed')),
    new_leads INTEGER NOT NULL DEFAULT 0,
    successful_tasks INTEGER NOT NULL DEFAULT 0,
    progress JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ,
    UNIQUE(user_id, request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS paid_one_running_user ON paid_discovery_runs(user_id)
    WHERE settled_at IS NULL;
CREATE INDEX IF NOT EXISTS paid_daily_wallet ON paid_discovery_runs(wallet_id, created_at);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id BIGSERIAL PRIMARY KEY,
    wallet_id BIGINT NOT NULL REFERENCES team_wallets(id),
    actor_id INTEGER REFERENCES users(id),
    event_key TEXT NOT NULL UNIQUE,
    amount NUMERIC(14,2) NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ai_pitch_runs (
    id UUID PRIMARY KEY,
    wallet_id BIGINT NOT NULL REFERENCES team_wallets(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    lead_id INTEGER NOT NULL,
    charge NUMERIC(14,2) NOT NULL CHECK (charge > 0),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ai_pitch_one_pending_user ON ai_pitch_runs(user_id)
    WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS ai_pitch_wallet_created ON ai_pitch_runs(wallet_id, created_at);
"""


@contextmanager
def transaction(dsn):
    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            yield cur


def ensure_membership(cur, user_id):
    # Serialize account creation and invite acceptance on the same user row.
    cur.execute("SELECT id FROM users WHERE id=%s AND is_active FOR UPDATE", (user_id,))
    if not cur.fetchone():
        raise PlanError("The account is unavailable.", "account_unavailable")
    cur.execute("SELECT wallet_id FROM team_members WHERE user_id=%s", (user_id,))
    member = cur.fetchone()
    if member:
        return member["wallet_id"]
    cur.execute("""INSERT INTO team_wallets(owner_id) VALUES (%s)
                   ON CONFLICT(owner_id) DO UPDATE SET owner_id=EXCLUDED.owner_id RETURNING id""", (user_id,))
    wallet_id = cur.fetchone()["id"]
    cur.execute("INSERT INTO team_members(user_id,wallet_id) VALUES (%s,%s)", (user_id, wallet_id))
    return wallet_id


def lock_wallet(cur, user_id):
    wallet_id = ensure_membership(cur, user_id)
    cur.execute("SELECT * FROM team_wallets WHERE id=%s FOR UPDATE", (wallet_id,))
    return dict(cur.fetchone())


def is_active(wallet, now=None):
    now = now or datetime.now(timezone.utc)
    return (wallet["plan_id"] != "unassigned" and wallet["starts_at"] is not None
            and wallet["starts_at"] <= now < wallet["ends_at"])


def add_month(value):
    year, month = value.year + (value.month == 12), value.month % 12 + 1
    return value.replace(year=year, month=month,
                         day=min(value.day, calendar.monthrange(year, month)[1]))


def ledger(cur, wallet, actor, key, amount, reason):
    cur.execute("""INSERT INTO credit_ledger(wallet_id,actor_id,event_key,amount,reason)
                   VALUES (%s,%s,%s,%s,%s)""", (wallet["id"], actor, key, amount, reason))
    cur.execute("UPDATE team_wallets SET balance=balance+%s WHERE id=%s", (amount, wallet["id"]))
    wallet["balance"] += amount


def activate_wallet(cur, wallet, actor, plan_id, months, event_key, reason, discard_trial=False):
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7) if plan_id == "trial" else now
    if plan_id != "trial":
        for _ in range(months):
            end = add_month(end)
    next_grant = None if plan_id == "trial" else add_month(now)
    rollover = (Decimal(0) if discard_trial and wallet["plan_id"] == "trial"
                else min(wallet["balance"], Decimal(PLANS[plan_id].rollover)))
    target = Decimal(PLANS[plan_id].credits) + rollover
    cur.execute("""UPDATE team_wallets SET plan_id=%s,starts_at=%s,ends_at=%s,next_grant_at=%s
                   WHERE id=%s""", (plan_id, now, end, next_grant, wallet["id"]))
    ledger(cur, wallet, actor, event_key, target - wallet["balance"], reason)


def trial_eligible(cur, wallet, user_id):
    if (wallet["owner_id"] != user_id or wallet["plan_id"] != "unassigned"
            or wallet["balance"] != 0):
        return False
    cur.execute("SELECT 1 FROM credit_ledger WHERE wallet_id=%s AND (event_key LIKE 'trial:%%' OR event_key LIKE 'activate:%%')",
                (wallet["id"],))
    if cur.fetchone():
        return False
    cur.execute("SELECT 1 FROM purchase_orders WHERE wallet_id=%s AND status IN ('creating','pending','unknown','review')",
                (wallet["id"],))
    return cur.fetchone() is None


def email_verified(cur, user_id):
    cur.execute("SELECT email_verified_at FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    return bool(row and row["email_verified_at"])


TRIAL_NETWORK_USED = "A free trial has already been used on this network. Choose a plan to get started."


def trial_network_free(cur, user_id):
    cur.execute("""SELECT NOT EXISTS (SELECT 1 FROM trial_network_claims c
                                      WHERE c.network_hash = u.signup_network_hash) AS free
                   FROM users u WHERE u.id=%s""", (user_id,))
    row = cur.fetchone()
    return bool(row and row["free"])


def grant_trial(cur, wallet, user_id, reason):
    """Starts the first trial when allowed.

    Returns 'started', 'unverified', 'not_eligible' or 'network_used'. The
    network claim is a unique row, so concurrent sign-ups from one network
    cannot both win; a rolled-back claim frees the network again.
    """
    cur.execute("SELECT email_verified_at, signup_network_hash FROM users WHERE id=%s", (user_id,))
    account = cur.fetchone()
    if not account or account["email_verified_at"] is None:
        return "unverified"
    if not trial_eligible(cur, wallet, user_id):
        return "not_eligible"
    if account["signup_network_hash"]:
        cur.execute("""INSERT INTO trial_network_claims(network_hash,user_id) VALUES (%s,%s)
                       ON CONFLICT (network_hash) DO NOTHING RETURNING user_id""",
                    (account["signup_network_hash"], user_id))
        if cur.fetchone() is None:
            return "network_used"
    activate_wallet(cur, wallet, user_id, "trial", 1, f"trial:{wallet['id']}", reason)
    return "started"


def claim_trial(dsn, user_id):
    with transaction(dsn) as cur:
        wallet = lock_wallet(cur, user_id)
        outcome = grant_trial(cur, wallet, user_id, "First free trial")
        if outcome == "unverified":
            raise PlanError("Verify your email address to start your free trial.", "email_unverified", 403)
        if outcome == "network_used":
            raise PlanError(TRIAL_NETWORK_USED, "trial_network_used", 409)
        if outcome != "started":
            raise PlanError("This account is not eligible for another free trial.", "trial_unavailable", 409)


def refresh_wallet(cur, wallet):
    now = datetime.now(timezone.utc)
    if not is_active(wallet, now) or not wallet["next_grant_at"] or wallet["next_grant_at"] > now:
        return
    cur.execute("SELECT 1 FROM paid_discovery_runs WHERE wallet_id=%s AND settled_at IS NULL",
                (wallet["id"],))
    if cur.fetchone():
        raise PlanError("A search is settling across a billing boundary. Try again after it completes.",
                        "billing_busy", 409)
    plan = PLANS[wallet["plan_id"]]
    while wallet["next_grant_at"] <= now and wallet["next_grant_at"] < wallet["ends_at"]:
        boundary = wallet["next_grant_at"]
        target = Decimal(plan.credits) + min(wallet["balance"], Decimal(plan.rollover))
        ledger(cur, wallet, wallet["owner_id"], f"renew:{wallet['id']}:{boundary.isoformat()}",
               target - wallet["balance"], "Monthly allowance and capped rollover")
        wallet["next_grant_at"] = add_month(boundary)
    cur.execute("UPDATE team_wallets SET next_grant_at=%s WHERE id=%s",
                (wallet["next_grant_at"], wallet["id"]))


def wallet_summary(dsn, user_id):
    with transaction(dsn) as cur:
        wallet = lock_wallet(cur, user_id)
        refresh_wallet(cur, wallet)
        active = is_active(wallet)
        cur.execute("""SELECT COALESCE((SELECT MAX(created_at) FROM credit_ledger WHERE wallet_id=%s
                            AND (event_key LIKE 'activate:%%' OR event_key LIKE 'renew:%%'
                                 OR event_key LIKE 'trial:%%')), %s) AS since""",
                    (wallet["id"], wallet["starts_at"]))
        since = cur.fetchone()["since"]
        cur.execute("""SELECT COALESCE(SUM(charge-refund),0) AS used FROM paid_discovery_runs
                       WHERE wallet_id=%s AND created_at >= %s""", (wallet["id"], since))
        used = cur.fetchone()["used"]
        cur.execute("""SELECT COALESCE(SUM(charge),0) AS used FROM ai_pitch_runs
                       WHERE wallet_id=%s AND state <> 'failed' AND created_at >= %s""",
                    (wallet["id"], since))
        used += cur.fetchone()["used"]
        total = used + wallet["balance"]
        renews = (active and wallet["next_grant_at"] is not None
                  and wallet["next_grant_at"] < wallet["ends_at"])
        return {
            "wallet_id": wallet["id"], "owner": wallet["owner_id"] == user_id,
            "balance": str(wallet["balance"]), "ends_at": wallet["ends_at"],
            "plan": capabilities(wallet["plan_id"], active),
            "usage": {
                "used": str(used), "total": str(total),
                "percent": str((used / total * 100).quantize(Decimal("0.1"))) if total else "0.0",
                "next_date": wallet["next_grant_at"] if renews else wallet["ends_at"],
                "next_event": "refresh" if renews else "expiry",
            },
            "can_manage_team": active and wallet["owner_id"] == user_id
                and PLANS[wallet["plan_id"]].seats > 1,
        }


def services_for_payload(dsn, user_id, payload):
    from hiring_services import BUILTIN_SERVICES, list_hiring_services
    selected = payload.get("service_types", [])
    if isinstance(selected, list) and any(isinstance(s, str) and s.startswith("custom_") for s in selected):
        return {s.id: s for s in list_hiring_services(dsn, user_id)}
    return BUILTIN_SERVICES


def quote(dsn, user_id, mode, payload):
    if not isinstance(payload, dict):
        raise PlanError("Search expects a JSON object.", "invalid_search", 400)
    summary = wallet_summary(dsn, user_id)
    services = services_for_payload(dsn, user_id, payload) if mode == "hiring" else None
    amount = quote_search(summary["plan"]["id"], summary["plan"]["active"], mode, payload, services)
    return {"charge": str(amount), "zero_result_refund": str(amount * ZERO_RESULT_REFUND),
            "balance": summary["balance"], "sufficient": Decimal(summary["balance"]) >= amount,
            "policy": "70% credit refund if the completed submission has zero new leads; "
                      "no charge if all discovery fails."}


def reserve(dsn, user_id, mode, payload, request_key):
    try:
        key = str(uuid.UUID(str(request_key)))
    except (ValueError, TypeError, AttributeError) as error:
        raise PlanError("A valid Idempotency-Key UUID is required.", "invalid_request_key", 400) from error
    fingerprint = hashlib.sha256(json.dumps([mode, payload], sort_keys=True).encode()).hexdigest()
    services = services_for_payload(dsn, user_id, payload) if mode == "hiring" else None
    with transaction(dsn) as cur:
        wallet = lock_wallet(cur, user_id)
        cur.execute("SELECT * FROM paid_discovery_runs WHERE user_id=%s AND request_key=%s",
                    (user_id, key))
        previous = cur.fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise PlanError("This request key was already used for a different search.",
                                "request_conflict", 409)
            return dict(previous), True
        from payments import assert_no_open_order
        assert_no_open_order(cur, wallet["id"], plans_only=True)
        refresh_wallet(cur, wallet)
        amount = quote_search(wallet["plan_id"], is_active(wallet), mode, payload, services)
        cur.execute("SELECT 1 FROM paid_discovery_runs WHERE user_id=%s AND settled_at IS NULL", (user_id,))
        if cur.fetchone():
            raise PlanError("You already have a discovery run in progress.", "discovery_busy", 409)
        cur.execute("""SELECT count(*) AS count FROM paid_discovery_runs WHERE wallet_id=%s
                       AND progress->>'launch_failed' IS DISTINCT FROM 'true'
                       AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
                    (wallet["id"],))
        if cur.fetchone()["count"] >= PLANS[wallet["plan_id"]].daily:
            raise PlanError("Your shared plan's daily submission cap has been reached (UTC).", "daily_limit", 429)
        if wallet["balance"] < amount:
            raise PlanError("The shared credit balance is insufficient for this search.", "insufficient_credits", 402)
        run_id = str(uuid.uuid4())
        cur.execute("""INSERT INTO paid_discovery_runs
                       (id,wallet_id,user_id,request_key,fingerprint,mode,charge)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (run_id, wallet["id"], user_id, key, fingerprint, mode, amount))
        run = dict(cur.fetchone())
        ledger(cur, wallet, user_id, f"reserve:{run_id}", -amount, "Discovery reservation")
        return run, False


PITCH_STALE_MINUTES = 2


def _refund_pitch(cur, wallet, user_id, row):
    target = wallet
    if row["wallet_id"] != wallet["id"]:
        cur.execute("SELECT * FROM team_wallets WHERE id=%s FOR UPDATE", (row["wallet_id"],))
        target = dict(cur.fetchone())
    ledger(cur, target, user_id, f"pitch-refund:{row['id']}", row["charge"], "AI draft refund")


def reserve_pitch(dsn, user_id, lead_id):
    """Charges one AI draft up front; returns (pitch_id, lead) for the OpenAI call.

    The charge is refunded by fail_pitch. Only one draft per user can be in
    flight; a draft left pending by a crashed worker is refunded after a few
    minutes so it cannot block the account.
    """
    with transaction(dsn) as cur:
        wallet = lock_wallet(cur, user_id)
        refresh_wallet(cur, wallet)
        if not is_active(wallet) or not PLANS[wallet["plan_id"]].ai_drafts:
            raise PlanError("AI pitch drafts start on the Solo plan. Upgrade to write pitches with AI.",
                            "pitch_locked", 403)
        cur.execute("""UPDATE ai_pitch_runs SET state='failed', settled_at=NOW()
                       WHERE user_id=%s AND state='pending'
                         AND created_at < NOW() - make_interval(mins => %s)
                       RETURNING id, wallet_id, charge""", (user_id, PITCH_STALE_MINUTES))
        for stale in cur.fetchall():
            _refund_pitch(cur, wallet, user_id, stale)
        cur.execute("SELECT 1 FROM ai_pitch_runs WHERE user_id=%s AND state='pending'", (user_id,))
        if cur.fetchone():
            raise PlanError("An AI draft is already being written. Wait for it to finish.", "pitch_busy", 409)
        cur.execute("""SELECT id, business_name, category, lead_type FROM prospects
                       WHERE id=%s AND user_id=%s""", (lead_id, user_id))
        lead = cur.fetchone()
        if not lead:
            raise PlanError("Target lead record not found.", "lead_not_found", 404)
        from payments import assert_no_open_order
        assert_no_open_order(cur, wallet["id"], plans_only=True)
        if wallet["balance"] < PITCH_PRICE:
            raise PlanError("The shared credit balance is insufficient for an AI draft.",
                            "insufficient_credits", 402)
        pitch_id = str(uuid.uuid4())
        cur.execute("""INSERT INTO ai_pitch_runs(id,wallet_id,user_id,lead_id,charge)
                       VALUES (%s,%s,%s,%s,%s)""", (pitch_id, wallet["id"], user_id, lead_id, PITCH_PRICE))
        ledger(cur, wallet, user_id, f"pitch:{pitch_id}", -PITCH_PRICE, "AI pitch draft")
        return pitch_id, dict(lead)


def finish_pitch(dsn, user_id, pitch_id, lead_id, draft, subject_line, angle):
    """Saves the draft and settles the charge; returns the wallet balance left."""
    with transaction(dsn) as cur:
        cur.execute("""UPDATE ai_pitch_runs SET state='done', settled_at=NOW()
                       WHERE id=%s AND user_id=%s AND state='pending' RETURNING wallet_id""",
                    (pitch_id, user_id))
        row = cur.fetchone()
        if not row:
            raise PlanError("The AI draft took too long and its credit was refunded. Try again.",
                            "pitch_expired", 409)
        cur.execute("""UPDATE prospects
                       SET ai_pitch_draft=%s, subject_line=%s, suggested_angle=%s, status='drafted'
                       WHERE id=%s AND user_id=%s""",
                    (draft, subject_line, angle, lead_id, user_id))
        cur.execute("SELECT balance FROM team_wallets WHERE id=%s", (row["wallet_id"],))
        return cur.fetchone()["balance"]


def fail_pitch(dsn, user_id, pitch_id):
    with transaction(dsn) as cur:
        wallet = lock_wallet(cur, user_id)
        cur.execute("""UPDATE ai_pitch_runs SET state='failed', settled_at=NOW()
                       WHERE id=%s AND user_id=%s AND state='pending'
                       RETURNING id, wallet_id, charge""", (pitch_id, user_id))
        row = cur.fetchone()
        if row:
            _refund_pitch(cur, wallet, user_id, row)


def bind_run(run_id):
    CONTEXT.run_id = str(run_id)


def insert_and_track(cur, query, rows, executor=execute_values):
    """Track only newly inserted leads, atomically with the lead write."""
    run_id = getattr(CONTEXT, "run_id", None)
    if not run_id:
        executor(cur, query, rows)
        return
    returned = executor(cur, query.rstrip().rstrip(";") + " RETURNING (xmax = 0) AS inserted",
                        rows, fetch=True)
    inserted = sum(bool(row["inserted"] if isinstance(row, dict) else row[0]) for row in returned)
    cur.execute("""UPDATE paid_discovery_runs SET new_leads=new_leads+%s
                   WHERE id=%s AND settled_at IS NULL RETURNING id""", (inserted, run_id))
    if not cur.fetchone():
        raise RuntimeError("The billing reservation is no longer active; lead write rolled back.")


def save_progress(dsn, run_id, progress):
    with transaction(dsn) as cur:
        cur.execute("""UPDATE paid_discovery_runs SET progress=%s, successful_tasks=%s, state='running'
                       WHERE id=%s AND settled_at IS NULL""",
                    (Json(progress), progress["successful_tasks"], str(run_id)))


def settle(dsn, run_id, progress=None, interrupted=False, launch_failed=False):
    # Lock ordering is always wallet -> run, matching reservations and membership actions.
    with transaction(dsn) as cur:
        cur.execute("SELECT wallet_id FROM paid_discovery_runs WHERE id=%s", (str(run_id),))
        found = cur.fetchone()
        if not found:
            raise RuntimeError("Billing reservation not found.")
        cur.execute("SELECT * FROM team_wallets WHERE id=%s FOR UPDATE", (found["wallet_id"],))
        wallet = dict(cur.fetchone())
        cur.execute("SELECT * FROM paid_discovery_runs WHERE id=%s FOR UPDATE", (str(run_id),))
        run = dict(cur.fetchone())
        if run["settled_at"]:
            return
        completed = (progress or {}).get("successful_tasks", run["successful_tasks"])
        if run["new_leads"] > 0:
            refund = Decimal("0")
        elif interrupted or not completed:
            refund = run["charge"]
        else:
            refund = (run["charge"] * ZERO_RESULT_REFUND).quantize(Decimal("0.01"))
        state = "succeeded" if not interrupted and (completed or run["new_leads"]) else "failed"
        result = dict(progress or run["progress"])
        if launch_failed and run["state"] == "reserved":
            result["launch_failed"] = True
        result.update(id=str(run_id), state=state, new_leads=run["new_leads"],
                      credit_charge=str(run["charge"]), credit_refund=str(refund),
                      net_charge=str(run["charge"] - refund))
        if interrupted:
            message = ("Search could not start. Its reservation was released."
                       if result.get("launch_failed")
                       else "Search interrupted. Billing reconciled from saved results.")
            result.setdefault("errors", []).append(message)
        ledger(cur, wallet, run["user_id"], f"settle:{run_id}", refund, "Discovery credit settlement")
        cur.execute("""UPDATE paid_discovery_runs SET refund=%s,state=%s,progress=%s,
                       successful_tasks=%s,settled_at=NOW() WHERE id=%s""",
                    (refund, state, Json(result), completed, str(run_id)))
    if getattr(CONTEXT, "run_id", None) == str(run_id):
        del CONTEXT.run_id


def snapshot(dsn, user_id, run_id):
    try:
        run_id = str(uuid.UUID(str(run_id)))
    except ValueError:
        return None
    with transaction(dsn) as cur:
        cur.execute("SELECT * FROM paid_discovery_runs WHERE id=%s AND user_id=%s", (run_id, user_id))
        run = cur.fetchone()
        if not run:
            return None
        result = dict(run["progress"])
        result.update(id=str(run["id"]), state="running" if not run["settled_at"] else run["state"],
                      new_leads=run["new_leads"], credit_charge=str(run["charge"]),
                      credit_refund=str(run["refund"]))
        for key, default in [("total", 0), ("completed", 0), ("saved", 0),
                             ("errors", []), ("warnings", []), ("logs", [])]:
            result.setdefault(key, default)
        return result
