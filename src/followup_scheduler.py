import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")

# Sequence: current status → days until next contact, next status, message template
FOLLOWUP_SEQUENCE = {
    "sent": {
        "days": 3,
        "next_status": "followup_1",
        "template": (
            "Hi {business_name} Team,\n\n"
            "I sent over a custom digital strategy breakdown for {business_name} a few days ago "
            "and wanted to make sure it didn't get buried.\n\n"
            "We work with international clients and specialize in turning strong business reputations "
            "into high-performing digital pipelines — without the enterprise price tag.\n\n"
            "Would love to show you a quick 5-minute mockup. Worth a look?\n\n"
            "Best regards,"
        ),
    },
    "followup_1": {
        "days": 4,
        "next_status": "followup_2",
        "template": (
            "Hi {business_name},\n\n"
            "Last follow-up from my end — I don't want to clutter your inbox.\n\n"
            "If the timing isn't right or you've already got this handled, no worries at all. "
            "But if you're still open to seeing what a modernized digital strategy could unlock "
            "for your pipeline, I'm just one reply away.\n\n"
            "Cheers,"
        ),
    },
    "followup_2": {
        "days": None,
        "next_status": "cold",
        "template": None,
    },
}


def schedule_followup(lead_id: int, current_status: str):
    """
    Sets followup_due_at on a lead immediately after it's sent,
    so the sweep engine knows when to stage the next message.
    """
    seq = FOLLOWUP_SEQUENCE.get(current_status)
    if not seq or not seq["days"]:
        return

    due = datetime.utcnow() + timedelta(days=seq["days"])
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE prospects SET followup_due_at = %s WHERE id = %s;",
                    (due, lead_id),
                )
                conn.commit()
    except Exception as e:
        print(
            f"[FOLLOWUP SCHEDULER] Could not schedule follow-up for lead {lead_id}: {e}"
        )


def run_followup_sweep():
    """
    Background sweep: finds all leads where followup_due_at has passed
    and auto-stages the next follow-up draft for review and dispatch.
    """
    from control_gate_api import emit_ui_log

    emit_ui_log("⏰ [FOLLOWUP ENGINE] Running overdue follow-up sweep...")

    try:
        with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, business_name, status FROM prospects
                    WHERE followup_due_at <= NOW()
                      AND status IN ('sent', 'followup_1', 'followup_2')
                    ORDER BY followup_due_at ASC;
                    """
                )
                due_leads = cur.fetchall()

                if not due_leads:
                    emit_ui_log("[FOLLOWUP ENGINE] No overdue follow-ups at this time.")
                    return 0

                processed = 0
                for lead in due_leads:
                    seq = FOLLOWUP_SEQUENCE.get(lead["status"])
                    next_status = seq["next_status"] if seq else "cold"

                    if not seq or not seq["template"]:
                        # End of sequence — mark as cold, no more follow-ups
                        cur.execute(
                            "UPDATE prospects SET status = 'cold', followup_due_at = NULL WHERE id = %s;",
                            (lead["id"],),
                        )
                        emit_ui_log(
                            f"🧊 [FOLLOWUP] Sequence ended, marked cold: {lead['business_name']}"
                        )
                    else:
                        draft = seq["template"].format(
                            business_name=lead["business_name"]
                        )
                        next_days = FOLLOWUP_SEQUENCE.get(next_status, {}).get("days")
                        next_due = (
                            datetime.utcnow() + timedelta(days=next_days)
                            if next_days
                            else None
                        )

                        cur.execute(
                            """
                            UPDATE prospects
                            SET ai_pitch_draft = %s, status = %s, followup_due_at = %s
                            WHERE id = %s;
                            """,
                            (draft, next_status, next_due, lead["id"]),
                        )
                        emit_ui_log(
                            f"📩 [FOLLOWUP] Staged '{next_status}' draft for: {lead['business_name']}"
                        )

                    processed += 1

                conn.commit()
                emit_ui_log(
                    f"✅ [FOLLOWUP ENGINE] {processed} leads processed."
                )
                return processed

    except Exception as e:
        emit_ui_log(f"❌ [FOLLOWUP ERROR] {e}")
        return 0
