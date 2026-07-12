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


# Domains to skip when searching for a company's official website
_SKIP_DOMAINS = [
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerbuilder.com", "facebook.com", "twitter.com",
    "youtube.com", "wikipedia.org", "yelp.com", "instagram.com",
    "google.com", "crunchbase.com", "bloomberg.com", "wellfound.com",
    "builtinnyc.com", "built.in", "lever.co", "greenhouse.io",
    "workday.com", "bamboohr.com", "recruitee.com",
]


def enrich_company_contact(company_name: str, location: str) -> dict:
    """
    Finds a company's official website via SerpApi Google Search,
    then crawls it for a contact email using the existing email_hunter.
    Returns dict with website_url and email (either or both may be None).
    """
    from email_hunter import attempt_domain_email_crawl

    result = {"website_url": None, "email": None}
    if not SERPAPI_KEY:
        return result

    params = {
        "engine": "google",
        "q": f'"{company_name}" official website contact',
        "api_key": SERPAPI_KEY,
        "num": 5,
    }

    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=12)
        resp.raise_for_status()
        for item in resp.json().get("organic_results", []):
            url = item.get("link", "")
            if not url.startswith("http"):
                continue
            if any(d in url for d in _SKIP_DOMAINS):
                continue
            result["website_url"] = url
            break
    except Exception as e:
        print(f"[ENRICHMENT] Website search failed for '{company_name}': {e}")

    if result["website_url"]:
        try:
            result["email"] = attempt_domain_email_crawl(result["website_url"])
        except Exception as e:
            print(f"[ENRICHMENT] Email crawl failed for '{result['website_url']}': {e}")

    return result


def save_job_leads(leads):
    from control_gate_api import emit_ui_log
    if not leads:
        emit_ui_log("[DATABASE] No job board leads found in this pass.")
        return

    insert_query = """
        INSERT INTO prospects (
            business_name, category, formatted_address, google_place_id,
            lead_type, source_platform, website_url, email,
            suggested_angle, job_via, job_salary, job_schedule,
            job_description, job_apply_link, status
        )
        VALUES %s
        ON CONFLICT (google_place_id) DO UPDATE SET
            website_url     = COALESCE(EXCLUDED.website_url,     prospects.website_url),
            email           = COALESCE(EXCLUDED.email,           prospects.email),
            suggested_angle = COALESCE(EXCLUDED.suggested_angle, prospects.suggested_angle),
            job_via         = COALESCE(EXCLUDED.job_via,         prospects.job_via),
            job_salary      = COALESCE(EXCLUDED.job_salary,      prospects.job_salary),
            job_schedule    = COALESCE(EXCLUDED.job_schedule,    prospects.job_schedule),
            job_description = COALESCE(EXCLUDED.job_description, prospects.job_description),
            job_apply_link  = COALESCE(EXCLUDED.job_apply_link,  prospects.job_apply_link);
    """
    data_tuples = [
        (
            lead["company_name"][:254],
            lead["job_title"][:254],
            lead.get("location", "Remote / International")[:499],
            lead["unique_id"],
            lead["lead_type"],
            "job_board",
            lead.get("website_url"),
            lead.get("email"),
            f"Hiring '{lead.get('job_role_signal', lead['job_title'])[:80]}' — outsourcing opportunity",
            lead.get("job_via", ""),
            lead.get("job_salary", ""),
            lead.get("job_schedule", ""),
            lead.get("job_description", ""),
            lead.get("job_apply_link", ""),
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
        emit_ui_log("💡 [HINT] Restart the app if this mentions a missing column — migrations run on startup.")


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

                detected_ext = job.get("detected_extensions") or {}
                apply_options = job.get("apply_options") or []
                description = (job.get("description") or "").strip()

                all_leads.append(
                    {
                        "company_name": company,
                        "job_title": job.get("title", role)[:254],
                        "location": job.get("location", "Remote"),
                        "unique_id": unique_id,
                        "lead_type": service_type,
                        "job_role_signal": role,
                        "job_via": (job.get("via") or "").strip()[:100],
                        "job_description": description[:600],
                        "job_salary": (detected_ext.get("salary") or detected_ext.get("salary_estimate") or "").strip()[:100],
                        "job_schedule": (detected_ext.get("schedule_type") or "").strip()[:50],
                        "job_apply_link": apply_options[0].get("link", "") if apply_options else "",
                    }
                )

            emit_ui_log(f"  ✅ [{role}] Captured {len(jobs)} hiring companies.")

        except Exception as e:
            emit_ui_log(f"❌ [JOB ENGINE ERROR] Query '{role}' failed: {e}")

    if not all_leads:
        emit_ui_log("⚠️ [JOB ENGINE] No companies found. Check your SERPAPI_API_KEY or try a different country.")
        return 0

    # --- ENRICHMENT PASS: find each company's website + contact email ---
    # Cache by company name so the same company isn't searched multiple times
    enrichment_cache = {}
    emit_ui_log(f"🔍 [ENRICHMENT] Finding contact details for {len(all_leads)} companies...")

    for lead in all_leads:
        company = lead["company_name"]
        if company not in enrichment_cache:
            emit_ui_log(f"  🌐 Searching: {company}...")
            enrich = enrich_company_contact(company, lead["location"])
            enrichment_cache[company] = enrich
            if enrich["email"]:
                emit_ui_log(f"  ✉️ Contact found → {enrich['email']}")
            elif enrich["website_url"]:
                emit_ui_log(f"  🌐 Website found → {enrich['website_url']}")
            else:
                emit_ui_log(f"  ⏭️ No public contact found for {company}")

        lead["website_url"] = enrichment_cache[company]["website_url"]
        lead["email"] = enrichment_cache[company]["email"]

    save_job_leads(all_leads)
    return len(all_leads)
