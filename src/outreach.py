"""Owner-scoped outreach tracking, independent of discovery/draft workflow."""

from datetime import datetime, timedelta, timezone
import re

STATUSES = ("not_contacted", "awaiting_reply", "replied", "successful", "failed")
TABLES = {"prospects": "prospects", "jobs": "job_leads"}
# Outcome -> (status it sets, statuses it may advance from). Calls only move a
# lead forward; Interested / Not interested deliberately set the outcome.
CALL_OUTCOMES = {
    "no_answer": (None, ()),
    "voicemail": ("awaiting_reply", ("not_contacted",)),
    "connected": ("replied", ("not_contacted", "awaiting_reply")),
    "callback_requested": ("replied", ("not_contacted", "awaiting_reply")),
    "interested": ("successful", STATUSES),
    "not_interested": ("failed", STATUSES),
    "wrong_number": (None, ()),
}
CONTACT_FILTERS = {
    "called": "call_attempts > 0",
    "not_called": "call_attempts = 0",
    "callback_due": "callback_at <= NOW()",
    "invalid_number": "phone_invalid",
}
_CLOSED = ("successful", "failed", "not_contacted")
CALL_FIELDS = "call_attempts, last_called_at, callback_at, phone_invalid"


def contact_facet_sql(table):
    """One (facet, value, label, count) row per non-empty contact-activity group; takes user_id."""
    counts = ", ".join(f"COUNT(*) FILTER (WHERE {condition}) AS {name}"
                       for name, condition in CONTACT_FILTERS.items())
    values = ", ".join(f"('{name}', c.{name})" for name in CONTACT_FILTERS)
    return (f"SELECT 'contact', f.value, f.value, f.count FROM (SELECT {counts} FROM {table} "
            f"WHERE user_id = %s AND status != 'rejected') c "
            f"CROSS JOIN LATERAL (VALUES {values}) AS f(value, count) WHERE f.count > 0")
SCHEMA = """
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS outreach_status TEXT;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS outreach_status TEXT;
UPDATE prospects SET outreach_status = CASE
    WHEN status IN ('sent', 'sent_followup_1', 'followup_1', 'followup_2', 'cold') THEN 'awaiting_reply'
    WHEN status = 'closed_won' THEN 'successful'
    WHEN status = 'closed_lost' THEN 'failed'
    ELSE 'not_contacted' END WHERE outreach_status IS NULL;
UPDATE job_leads SET outreach_status = CASE
    WHEN status IN ('sent', 'sent_followup_1') THEN 'awaiting_reply'
    WHEN status = 'closed_won' THEN 'successful'
    WHEN status = 'closed_lost' THEN 'failed'
    ELSE 'not_contacted' END WHERE outreach_status IS NULL;
ALTER TABLE prospects ALTER COLUMN outreach_status SET DEFAULT 'not_contacted';
ALTER TABLE job_leads ALTER COLUMN outreach_status SET DEFAULT 'not_contacted';
CREATE TABLE IF NOT EXISTS outreach_events (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prospect_id INTEGER REFERENCES prospects(id) ON DELETE CASCADE,
    job_lead_id INTEGER REFERENCES job_leads(id) ON DELETE CASCADE,
    previous_status TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((prospect_id IS NOT NULL)::int + (job_lead_id IS NOT NULL)::int = 1)
);
CREATE INDEX IF NOT EXISTS idx_outreach_prospect ON outreach_events(user_id, prospect_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_outreach_job ON outreach_events(user_id, job_lead_id, id DESC);
ALTER TABLE outreach_events ADD COLUMN IF NOT EXISTS channel TEXT;
UPDATE outreach_events SET channel = CASE WHEN source = 'email_sent' THEN 'email' ELSE 'manual' END
    WHERE channel IS NULL;
ALTER TABLE outreach_events ALTER COLUMN channel SET DEFAULT 'manual';
ALTER TABLE outreach_events ALTER COLUMN channel SET NOT NULL;
ALTER TABLE outreach_events ADD COLUMN IF NOT EXISTS call_outcome TEXT;
ALTER TABLE outreach_events ADD COLUMN IF NOT EXISTS callback_at TIMESTAMPTZ;
ALTER TABLE outreach_events ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE outreach_events ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
UPDATE outreach_events SET occurred_at = created_at WHERE occurred_at IS NULL;
ALTER TABLE outreach_events ALTER COLUMN occurred_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE outreach_events ALTER COLUMN occurred_at SET NOT NULL;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS call_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS last_called_at TIMESTAMPTZ;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS callback_at TIMESTAMPTZ;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS call_phone TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS phone_invalid BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS call_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS last_called_at TIMESTAMPTZ;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS callback_at TIMESTAMPTZ;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS call_phone TEXT;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS phone_invalid BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_prospects_callback ON prospects(user_id, callback_at) WHERE callback_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_job_leads_callback ON job_leads(user_id, callback_at) WHERE callback_at IS NOT NULL;
"""


def phone_sql(kind):
    """The number to call: a manually entered one first, then the lead's listed number."""
    if kind == "prospects":
        return "NULLIF(BTRIM(COALESCE(call_phone, phone, '')), '')"
    return ("NULLIF(BTRIM(COALESCE(call_phone, CASE WHEN jsonb_typeof(contact_evidence->'phones') = 'array' "
            "THEN contact_evidence->'phones'->0->>'phone' END, '')), '')")


def phone_source_sql(kind):
    listed = "'listed'" if kind == "prospects" else "'listed_unverified'"
    return (f"CASE WHEN NULLIF(BTRIM(COALESCE(call_phone, '')), '') IS NOT NULL THEN 'manual' "
            f"WHEN {phone_sql(kind)} IS NOT NULL THEN {listed} END")


def validate_update(data):
    if not isinstance(data, dict):
        raise ValueError("Provide an outreach status and optional note.")
    status, expected, note = data.get("status"), data.get("expected_status"), data.get("note", "")
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("Choose a valid outreach status.")
    if not isinstance(expected, str) or expected not in STATUSES:
        raise ValueError("Reload the lead before updating its outreach status.")
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("Notes must be text with at most 2,000 characters.")
    return status, expected, note.strip()


def _parse_time(value, label):
    if value in (None, ""):
        return None
    invalid = ValueError(f"Enter a valid {label}.")
    if not isinstance(value, str) or len(value) > 40:
        raise invalid
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise invalid from None
    if parsed.tzinfo is None:
        raise invalid
    return parsed.astimezone(timezone.utc)


_PHONE = re.compile(r"[0-9+()./\- ]+(?:\s*(?:ext\.?|x)\s*[0-9]{1,6})?", re.I)


def validate_call(data, now=None):
    if not isinstance(data, dict):
        raise ValueError("Choose how the call went.")
    now = now or datetime.now(timezone.utc)
    outcome, expected = data.get("outcome"), data.get("expected_status")
    note, phone = data.get("note", ""), data.get("phone") or ""
    if not isinstance(outcome, str) or outcome not in CALL_OUTCOMES:
        raise ValueError("Choose how the call went.")
    if not isinstance(expected, str) or expected not in STATUSES:
        raise ValueError("Reload the lead before logging a call.")
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("Notes must be text with at most 2,000 characters.")
    if not isinstance(phone, str):
        raise ValueError("Enter a valid phone number, or leave it blank.")
    phone = phone.strip()
    if phone and (len(phone) > 40 or not _PHONE.fullmatch(phone) or len(re.sub(r"\D", "", phone)) < 5):
        raise ValueError("Enter a valid phone number, or leave it blank.")
    occurred = _parse_time(data.get("occurred_at"), "call time") or now
    if occurred > now + timedelta(minutes=5) or occurred < now - timedelta(days=366):
        raise ValueError("The call time must be within the past year and not in the future.")
    callback = _parse_time(data.get("callback_at"), "callback time")
    if callback and outcome != "callback_requested":
        raise ValueError("Only a callback request can have a callback time.")
    if callback and (callback <= occurred or callback > now + timedelta(days=366)):
        raise ValueError("The callback must be after the call and within the next year.")
    return {"outcome": outcome, "expected": expected, "note": note.strip(), "phone": phone,
            "occurred_at": occurred, "callback_at": callback}


def record_update(cur, kind, lead_id, owner_id, status, note="", source="manual", expected=None):
    table = TABLES[kind]
    cur.execute(f"SELECT outreach_status FROM {table} WHERE id = %s AND user_id = %s FOR UPDATE",
                (lead_id, owner_id))
    lead = cur.fetchone()
    if not lead:
        return "missing"
    previous = lead.get("outreach_status") or "not_contacted"
    if expected is not None and previous != expected:
        return "conflict"
    if previous == status and not note and source == "manual":
        return "unchanged"
    cur.execute(f"UPDATE {table} SET outreach_status = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND user_id = %s", (status, lead_id, owner_id))
    if status in _CLOSED:
        cur.execute(f"UPDATE {table} SET callback_at = NULL WHERE id = %s AND user_id = %s",
                    (lead_id, owner_id))
    if kind == "prospects" and status in ("replied", "successful", "failed", "not_contacted"):
        cur.execute("UPDATE prospects SET followup_due_at = NULL WHERE id = %s AND user_id = %s",
                    (lead_id, owner_id))
    column = "prospect_id" if kind == "prospects" else "job_lead_id"
    cur.execute(f"INSERT INTO outreach_events (user_id, {column}, previous_status, status, note, source, channel) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (owner_id, lead_id, previous, status, note, source,
                 "email" if source == "email_sent" else "manual"))
    return "updated"


def record_call(cur, kind, lead_id, owner_id, call):
    table = TABLES[kind]
    cur.execute(f"SELECT outreach_status, {phone_sql(kind)} AS phone FROM {table} "
                "WHERE id = %s AND user_id = %s FOR UPDATE", (lead_id, owner_id))
    lead = cur.fetchone()
    if not lead:
        return "missing"
    previous = lead.get("outreach_status") or "not_contacted"
    if previous != call["expected"]:
        return "conflict"
    target, advances_from = CALL_OUTCOMES[call["outcome"]]
    status = target if target and previous in advances_from else previous
    listed = lead.get("phone") or ""
    called = call["phone"] or listed
    changes_number = bool(call["phone"]) and call["phone"] != listed
    # A new callback replaces the old one; any other answered call settles it.
    if call["outcome"] == "callback_requested":
        callback_sql, callback = "%s", [call["callback_at"]]
    elif call["outcome"] in ("no_answer", "wrong_number") and status not in _CLOSED:
        callback_sql, callback = "callback_at", []
    else:
        callback_sql, callback = "NULL", []
    # Reaching someone (or dialing a new number) clears a wrong-number flag; no answer proves nothing.
    if call["outcome"] == "wrong_number":
        invalid_sql, invalid = "%s", [True]
    elif call["outcome"] != "no_answer" or changes_number:
        invalid_sql, invalid = "%s", [False]
    else:
        invalid_sql, invalid = "phone_invalid", []
    cur.execute(
        f"UPDATE {table} SET outreach_status = %s, call_attempts = call_attempts + 1, "
        "last_called_at = GREATEST(COALESCE(last_called_at, %s), %s), "
        f"callback_at = {callback_sql}, phone_invalid = {invalid_sql}, "
        "call_phone = CASE WHEN %s THEN %s ELSE call_phone END, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = %s AND user_id = %s",
        [status, call["occurred_at"], call["occurred_at"], *callback, *invalid,
         changes_number, call["phone"], lead_id, owner_id])
    if kind == "prospects" and status in ("replied", "successful", "failed"):
        cur.execute("UPDATE prospects SET followup_due_at = NULL WHERE id = %s AND user_id = %s",
                    (lead_id, owner_id))
    column = "prospect_id" if kind == "prospects" else "job_lead_id"
    cur.execute(
        f"INSERT INTO outreach_events (user_id, {column}, previous_status, status, note, source, channel, "
        "call_outcome, callback_at, phone, occurred_at) "
        "VALUES (%s, %s, %s, %s, %s, 'call', 'call', %s, %s, %s, %s)",
        (owner_id, lead_id, previous, status, call["note"], call["outcome"], call["callback_at"],
         called or None, call["occurred_at"]))
    return "updated"
