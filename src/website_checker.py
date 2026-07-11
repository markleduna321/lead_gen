import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
# Optional — free tier works without a key, but a key increases rate limits significantly
PAGESPEED_KEY = os.getenv("PAGESPEED_API_KEY", "")


def check_website_quality(url: str) -> dict:
    """
    Queries Google PageSpeed Insights API (free) for a mobile performance score.
    Returns score 0-100. Lower score = slower/worse site = hotter redesign lead.
    """
    if not url or not str(url).startswith("http"):
        return {"score": None, "error": "Invalid or missing URL"}

    params = {
        "url": url,
        "strategy": "mobile",
        "category": "performance",
    }
    if PAGESPEED_KEY:
        params["key"] = PAGESPEED_KEY

    try:
        response = requests.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params=params,
            timeout=25,
        )
        response.raise_for_status()
        data = response.json()
        score = int(
            data["lighthouseResult"]["categories"]["performance"]["score"] * 100
        )
        fcp = data["lighthouseResult"]["audits"]["first-contentful-paint"]["displayValue"]
        return {"score": score, "fcp": fcp, "error": None}
    except Exception as e:
        return {"score": None, "error": str(e)}


def run_website_quality_check():
    """
    Audits all prospects that have a website_url but have not been scored yet.
    Processes up to 20 at a time to stay within API rate limits.
    """
    from control_gate_api import emit_ui_log

    emit_ui_log(
        "🔍 [SITE CHECKER] Running performance audit on unchecked prospect websites..."
    )

    try:
        with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, website_url FROM prospects
                    WHERE website_url IS NOT NULL
                      AND site_quality_score IS NULL
                    ORDER BY lead_score DESC
                    LIMIT 20;
                    """
                )
                leads = cur.fetchall()

                if not leads:
                    emit_ui_log("[SITE CHECKER] No unchecked websites in queue.")
                    return 0

                checked = 0
                for lead in leads:
                    result = check_website_quality(lead["website_url"])
                    if result["score"] is not None:
                        cur.execute(
                            "UPDATE prospects SET site_quality_score = %s WHERE id = %s;",
                            (result["score"], lead["id"]),
                        )
                        label = (
                            "🔴 Poor"
                            if result["score"] < 40
                            else ("🟡 Average" if result["score"] < 70 else "🟢 Good")
                        )
                        emit_ui_log(
                            f"  {label} {result['score']}/100 → {lead['website_url']}"
                        )
                        checked += 1
                    else:
                        emit_ui_log(
                            f"  ⚠️ Could not audit: {lead['website_url']} ({result['error']})"
                        )

                conn.commit()
                emit_ui_log(
                    f"✅ [SITE CHECKER] Audit complete — {checked}/{len(leads)} sites scored."
                )
                return checked

    except Exception as e:
        emit_ui_log(f"❌ [SITE CHECKER ERROR] {e}")
        return 0
