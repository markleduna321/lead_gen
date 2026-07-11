import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")


def calculate_lead_score(lead: dict) -> int:
    """
    Scores a prospect 0-100 based on conversion-readiness signals.
    Higher score = higher priority for outreach.
    """
    score = 0

    # Rating credibility — (max 20 pts)
    rating = float(lead.get("rating") or 0)
    if rating >= 4.5:
        score += 20
    elif rating >= 4.0:
        score += 12
    elif rating >= 3.5:
        score += 6

    # Review volume — bigger operation, more budget (max 20 pts)
    reviews = int(lead.get("review_count") or 0)
    if reviews >= 500:
        score += 20
    elif reviews >= 200:
        score += 15
    elif reviews >= 100:
        score += 10
    elif reviews >= 50:
        score += 5

    # Email found — contact-ready, outreach can fire immediately (max 25 pts)
    if lead.get("email"):
        score += 25

    # Phone number on file (max 10 pts)
    phone = lead.get("phone") or ""
    if phone and phone != "No Public Number Listed":
        score += 10

    # No website = top priority for web design pitch (max 15 pts)
    has_web_presence = lead.get("website_url") or lead.get("screenshot_path")
    if not has_web_presence:
        score += 15

    # Poor website performance score (max 10 pts)
    site_score = lead.get("site_quality_score")
    if site_score is not None:
        if site_score < 35:
            score += 10
        elif site_score < 60:
            score += 5

    return min(score, 100)


def run_lead_scoring():
    """
    Calculates and writes lead_score for all prospects currently at 0 or NULL.
    Run this after any discovery pass to auto-rank the pipeline by priority.
    """
    from control_gate_api import emit_ui_log

    emit_ui_log("🧮 [LEAD SCORER] Running priority scoring pass across all prospects...")

    try:
        with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, rating, review_count, email, website_url,
                           screenshot_path, site_quality_score, phone
                    FROM prospects
                    WHERE lead_score = 0 OR lead_score IS NULL;
                    """
                )
                leads = cur.fetchall()

                if not leads:
                    emit_ui_log("[LEAD SCORER] All prospects already have a score.")
                    return 0

                updates = [
                    (calculate_lead_score(dict(lead)), lead["id"]) for lead in leads
                ]
                cur.executemany(
                    "UPDATE prospects SET lead_score = %s WHERE id = %s;", updates
                )
                conn.commit()
                emit_ui_log(
                    f"✅ [LEAD SCORER] Scored {len(updates)} prospects. "
                    f"Top score: {max(s for s, _ in updates)}/100."
                )
                return len(updates)

    except Exception as e:
        emit_ui_log(f"❌ [LEAD SCORER ERROR] {e}")
        return 0
