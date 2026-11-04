"""Owner-scoped outreach tracking, independent of discovery/draft workflow."""

STATUSES = ("not_contacted", "awaiting_reply", "replied", "successful", "failed")
TABLES = {"prospects": "prospects", "jobs": "job_leads"}
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
"""


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
    if kind == "prospects" and status in ("replied", "successful", "failed", "not_contacted"):
        cur.execute("UPDATE prospects SET followup_due_at = NULL WHERE id = %s AND user_id = %s",
                    (lead_id, owner_id))
    column = "prospect_id" if kind == "prospects" else "job_lead_id"
    cur.execute(f"INSERT INTO outreach_events (user_id, {column}, previous_status, status, note, source) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (owner_id, lead_id, previous, status, note, source))
    return "updated"
