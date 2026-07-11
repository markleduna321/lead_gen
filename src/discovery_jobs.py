import os
import sys
import uuid
import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SERPAPI_KEY = os.getenv("SERPAPI_API_KEY")

# Maps service offering to job role signals that indicate a company is hiring
# instead of outsourcing — these are your warmest BPO/freelance prospects
JOB_SIGNAL_MAP = {
    "video_editing": [
        "video editor",
        "video content creator",
        "motion graphics designer",
        "video production specialist",
    ],
    "bpo_customer_service": [
        "customer service representative",
        "customer support agent",
        "call center agent",
        "support specialist",
    ],
    "bpo_data_entry": [
        "data entry specialist",
        "data processing clerk",
        "back office processor",
        "data operations analyst",
    ],
    "social_media_management": [
        "social media manager",
        "social media specialist",
        "community manager",
        "content creator social media",
    ],
    "virtual_assistant": [
        "virtual assistant",
        "executive assistant remote",
        "administrative assistant remote",
        "online business manager",
    ],
}


def save_job_leads(leads):
    from control_gate_api import emit_ui_log
    if not leads:
        emit_ui_log("[DATABASE] No job board leads found in this pass.")
        return

    insert_query = """
        INSERT INTO prospects (
            business_name, category, formatted_address, google_place_id,
            lead_type, source_platform, website_url, status
        )
        VALUES %s
        ON CONFLICT (google_place_id) DO NOTHING;
    """
    data_tuples = [
        (
            lead["company_name"][:254],          # business_name VARCHAR(255)
            lead["job_title"][:99],              # category VARCHAR(100)
            lead.get("location", "Remote / International")[:499],
            lead["unique_id"],
            lead["lead_type"],
            "job_board",
            lead.get("job_url"),
            "discovered",
        )
        for lead in leads
    ]

    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                execute_values(cur, insert_query, data_tuples)
                conn.commit()
                emit_ui_log(f"💾 [PostgreSQL] Saved {len(data_tuples)} job board prospects to DB.")
    except Exception as e:
        emit_ui_log(f"❌ [DB ERROR] Job leads insertion failure: {e}")
        emit_ui_log("💡 [HINT] If the error mentions a missing column, restart the app — migrations run automatically on startup.")


def run_job_discovery(service_type, country=""):
    """
    Scans Google Jobs via SerpApi to find companies actively hiring for roles
    that signal they need outsourcing — video editing, BPO, VA, social media, etc.
    A company posting these jobs is a warm international outsourcing prospect.
    """
    from control_gate_api import emit_ui_log

    if not SERPAPI_KEY:
        emit_ui_log("❌ [JOB ENGINE ERROR] Missing SERPAPI_API_KEY inside your .env configuration.")
        return 0

    job_roles = JOB_SIGNAL_MAP.get(service_type)
    if not job_roles:
        emit_ui_log(
            f"❌ [JOB ENGINE] Unknown service type: '{service_type}'. "
            f"Valid options: {list(JOB_SIGNAL_MAP.keys())}"
        )
        return 0

    all_leads = []

    for role in job_roles:
        query = f"{role} {country}".strip() if country else role
        emit_ui_log(f"🛰️ [JOB ENGINE] Scanning hiring signals for: '{role}'...")

        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": SERPAPI_KEY,
        }

        try:
            response = requests.get(
                "https://serpapi.com/search", params=params, timeout=15
            )
            response.raise_for_status()
            data = response.json()
            jobs = data.get("jobs_results", [])

            for job in jobs:
                company = (job.get("company_name") or "").strip()
                if not company:
                    continue

                # Deterministic unique ID from company + role to prevent re-ingesting duplicates
                unique_id = f"job_{uuid.uuid5(uuid.NAMESPACE_DNS, company.lower() + role).hex[:16]}"

                all_leads.append(
                    {
                        "company_name": company,
                        "job_title": job.get("title", role),
                        "location": job.get("location", "Remote"),
                        "unique_id": unique_id,
                        "lead_type": service_type,
                        "job_url": job.get("share_link") or job.get("job_id") or "",
                    }
                )

            emit_ui_log(f"  ✅ [{role}] Captured {len(jobs)} hiring companies.")

        except Exception as e:
            emit_ui_log(f"❌ [JOB ENGINE ERROR] Query '{role}' failed: {e}")

    save_job_leads(all_leads)
    return len(all_leads)
