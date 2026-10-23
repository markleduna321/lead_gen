import os
import sys
import hashlib
import json
import unicodedata
from datetime import datetime, timezone
import requests
import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv
from discovery_job_filters import matches_hiring_country, matches_hiring_role, resolve_hiring_country
from discovery_job_contacts import find_company_contact, web_url
from hiring_services import BUILTIN_SERVICES, JOB_SIGNAL_MAP, HiringService, matches_service_title

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SERPAPI_KEY = os.getenv("SERPAPI_API_KEY")

HIRING_PAGE_OPTIONS = (1, 2, 3, 5)


def validate_hiring_pages(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value not in HIRING_PAGE_OPTIONS:
        raise ValueError("Pages per role must be one of 1, 2, 3, or 5.")
    return value


def enrich_company_contact(company_name: str, location: str) -> dict:
    """Match public company pages and retain contact provenance, not guessed emails."""
    from control_gate_api import emit_ui_log
    try:
        return find_company_contact(company_name, location, SERPAPI_KEY, emit_ui_log)
    except (requests.RequestException, ValueError, RuntimeError) as error:
        emit_ui_log(f"[ENRICHMENT ERROR] Company lookup failed for '{company_name}': {error}")
        raise


def normalize_job_identity_part(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def job_lead_identity(company: str, title: str, location: str, service: str) -> str:
    parts = [normalize_job_identity_part(value) for value in (company, title, location, service)]
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def merge_job_details(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for key, value in incoming.items():
        if isinstance(value, dict):
            merged[key] = merge_job_details(merged.get(key, {}), value)
        elif isinstance(value, list):
            items = list(merged.get(key, []))
            for item in value:
                if item not in items:
                    items.append(item)
            merged[key] = items
        elif key not in merged or merged[key] is None or merged[key] == "":
            merged[key] = value
    return merged


def deduplicate_job_leads(leads):
    from control_gate_api import emit_ui_log

    unique = {}
    for lead in leads:
        identity = job_lead_identity(
            lead["company_name"], lead["job_title"],
            lead.get("location") or "Unknown", lead["lead_type"],
        )
        if identity not in unique:
            unique[identity] = dict(lead, job_identity=identity)
            continue
        existing = unique[identity]
        for field in (
            "job_via", "job_salary", "job_schedule", "job_description",
            "job_apply_link",
        ):
            if not existing.get(field) and lead.get(field):
                existing[field] = lead[field]
        if (
            (not existing.get("website_url") and lead.get("website_url"))
            or (existing.get("website_url") == lead.get("website_url")
                and not existing.get("email") and lead.get("email"))
        ):
            for field in ("website_url", "email", "contact_evidence"):
                existing[field] = lead.get(field)
        if len(lead.get("job_description") or "") > len(existing.get("job_description") or ""):
            existing["job_description"] = lead["job_description"]
        existing["job_details"] = merge_job_details(
            existing.get("job_details", {}), lead.get("job_details", {})
        )
        emit_ui_log(f"[JOB DEDUP] Merged repeated posting: {lead['company_name']} / {lead['job_title']} / {lead.get('location') or 'Unknown'}.")
    return list(unique.values())


def save_job_leads(leads):
    from control_gate_api import emit_ui_log
    if not leads:
        emit_ui_log("[DATABASE] No job board leads found in this pass.")
        return
    leads = deduplicate_job_leads(leads)

    insert_query = """
        INSERT INTO job_leads (
            company_name, job_title, location, lead_type, job_role_signal,
            job_via, job_salary, job_schedule, job_description, job_apply_link,
            website_url, email, suggested_angle, status, source_platform,
            job_details, contact_evidence, job_identity
        )
        VALUES %s
        ON CONFLICT (job_identity) DO UPDATE SET
            job_title       = EXCLUDED.job_title,
            location        = EXCLUDED.location,
            website_url     = COALESCE(NULLIF(EXCLUDED.website_url, ''),     job_leads.website_url),
            email           = CASE
                WHEN job_leads.contact_evidence->>'email_status' = 'manual' THEN job_leads.email
                WHEN EXCLUDED.contact_evidence->>'status' = 'matched' THEN EXCLUDED.email
                ELSE COALESCE(NULLIF(EXCLUDED.email, ''), job_leads.email) END,
            job_via         = COALESCE(NULLIF(EXCLUDED.job_via, ''),         job_leads.job_via),
            job_salary      = COALESCE(NULLIF(EXCLUDED.job_salary, ''),      job_leads.job_salary),
            job_schedule    = COALESCE(NULLIF(EXCLUDED.job_schedule, ''),    job_leads.job_schedule),
            job_description = COALESCE(NULLIF(EXCLUDED.job_description, ''), job_leads.job_description),
            job_apply_link  = COALESCE(NULLIF(EXCLUDED.job_apply_link, ''),  job_leads.job_apply_link),
            job_details     = COALESCE(job_leads.job_details, '{}'::jsonb) || COALESCE(EXCLUDED.job_details, '{}'::jsonb),
            contact_evidence = COALESCE(EXCLUDED.contact_evidence, job_leads.contact_evidence)
                || CASE WHEN job_leads.contact_evidence->>'email_status' = 'manual'
                        THEN jsonb_build_object('email_status', 'manual', 'email_source_url', NULL,
                                                'email_updated_at', job_leads.contact_evidence->>'email_updated_at')
                        ELSE '{}'::jsonb END,
            updated_at      = NOW();
    """
    data_tuples = [
        (
            lead["company_name"][:511],
            lead["job_title"][:511],
            lead.get("location") or "Unknown",
            lead["lead_type"],
            lead.get("job_role_signal", lead["job_title"])[:254],
            lead.get("job_via", "") or "",
            lead.get("job_salary", "") or "",
            lead.get("job_schedule", "") or "",
            lead.get("job_description", "") or "",
            lead.get("job_apply_link", "") or "",
            lead.get("website_url"),
            lead.get("email"),
            f"Hiring '{lead.get('job_role_signal', lead['job_title'])[:80]}' — outsourcing opportunity",
            "discovered",
            "job_board",
            Json(lead["job_details"]) if lead.get("job_details") else None,
            Json(lead["contact_evidence"]) if lead.get("contact_evidence") else None,
            lead["job_identity"],
        )
        for lead in leads
    ]

    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                execute_values(cur, insert_query, data_tuples)
                conn.commit()
                emit_ui_log(f"💾 [PostgreSQL] Saved {len(data_tuples)} job board leads to job_leads table.")
    except Exception as e:
        emit_ui_log(f"❌ [DB ERROR] Job leads insertion failure: {e}")
        raise




def run_job_discovery(service_type, country="", pages_per_role=1, service_definition: HiringService | None = None):
    """
    Scans Google Jobs via SerpApi to find companies actively hiring for roles
    that signal they need outsourcing — video editing, BPO, VA, social media, etc.
    A company posting these jobs is a warm international outsourcing prospect.
    """
    from control_gate_api import emit_ui_log

    pages_per_role = validate_hiring_pages(pages_per_role)
    if not SERPAPI_KEY:
        emit_ui_log("❌ [JOB ENGINE ERROR] Missing SERPAPI_API_KEY inside your .env configuration.")
        raise RuntimeError("Hiring discovery requires SERPAPI_API_KEY.")

    service = service_definition or BUILTIN_SERVICES.get(service_type)
    if service is None or service.id != service_type:
        emit_ui_log(
            f"❌ [JOB ENGINE] Unknown service type: '{service_type}'. "
            f"Valid options: {list(JOB_SIGNAL_MAP.keys())}"
        )
        raise ValueError(f"Unsupported hiring service: {service_type}")
    job_roles = service.roles

    target_country = resolve_hiring_country(country)
    if target_country:
        emit_ui_log(f"[JOB TARGETING] Strict country: {target_country.name}; unknown locations and unscoped remote jobs are excluded.")
    else:
        emit_ui_log("[JOB TARGETING] No country filter. Provider defaults may bias results; this is not a worldwide search.")
    all_leads = []
    errors = []
    emit_ui_log(f"[JOB DEPTH] Up to {pages_per_role} page(s) per role / {len(job_roles) * pages_per_role} Google Jobs requests for this service; contact searches are additional.")

    for role in job_roles:
        query = f"{role} {target_country.name}" if target_country else role
        emit_ui_log(f"🛰️ [JOB ENGINE] Scanning hiring signals for: '{role}'...")

        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": SERPAPI_KEY,
        }
        if target_country:
            params.update({"gl": target_country.code, "location": target_country.name})

        page_number = 1
        try:
            seen_tokens = set()
            for page_number in range(1, pages_per_role + 1):
                emit_ui_log(f"[JOB PAGE] {role}: page {page_number}/{pages_per_role}")
                response = requests.get(
                    "https://serpapi.com/search", params=dict(params), timeout=15
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Invalid Google Jobs response")
                if data.get("error"):
                    raise RuntimeError(data["error"])
                jobs = data.get("jobs_results", [])
                if not isinstance(jobs, list):
                    raise ValueError("Invalid Google Jobs results list")

                captured = 0
                for job in jobs:
                    try:
                        lead = parse_job_posting(job, service_type, role)
                    except (ValueError, TypeError, AttributeError) as error:
                        message = f"{role}, page {page_number}: malformed posting ({error})"
                        errors.append(message)
                        emit_ui_log(f"[JOB DATA ERROR] {message}")
                        continue
                    relevant = (
                        matches_hiring_role(lead["job_title"], service_type) if service.builtin
                        else matches_service_title(lead["job_title"], service)
                    )
                    if not relevant:
                        emit_ui_log(f"[JOB TARGETING] Skipped '{lead['job_title']}': no title evidence for {service_type}.")
                        continue
                    if not matches_hiring_country(lead["location"], target_country):
                        emit_ui_log(f"[JOB TARGETING] Skipped '{lead['company_name']}' at '{lead['location']}': country mismatch or unknown eligibility.")
                        continue
                    all_leads.append(lead)
                    captured += 1

                emit_ui_log(f"  [{role}, page {page_number}] Captured {captured} valid postings from {len(jobs)} results before deduplication.")
                if page_number == pages_per_role:
                    break
                pagination = data.get("serpapi_pagination", {})
                if not isinstance(pagination, dict):
                    raise ValueError("Invalid Google Jobs pagination")
                token = pagination.get("next_page_token")
                if token is None or token == "":
                    emit_ui_log(f"[JOB PAGE] {role}: no next page; search finished.")
                    break
                if not isinstance(token, str) or not token.strip():
                    raise ValueError("Invalid Google Jobs next-page token")
                if token in seen_tokens:
                    raise ValueError("Google Jobs repeated a next-page token; stopped pagination.")
                seen_tokens.add(token)
                params["next_page_token"] = token

        except Exception as e:
            emit_ui_log(f"❌ [JOB ENGINE ERROR] Query '{role}', page {page_number} failed: {e}")
            errors.append(f"{role}, page {page_number}: {e}")

    if not all_leads:
        if errors:
            raise RuntimeError(" | ".join(errors))
        emit_ui_log("[JOB ENGINE] No matching companies returned.")
        return 0

    all_leads = deduplicate_job_leads(all_leads)
    enrichment_cache = {}
    emit_ui_log(f"🔍 [ENRICHMENT] Finding contact details for {len(all_leads)} companies...")

    for lead in all_leads:
        company = lead["company_name"]
        company_location = (
            normalize_job_identity_part(company),
            normalize_job_identity_part(lead["location"]),
        )
        if company_location not in enrichment_cache:
            emit_ui_log(f"  🌐 Searching: {company}...")
            try:
                enrich = enrich_company_contact(company, lead["location"])
            except Exception as error:
                errors.append(f"Contact enrichment for {company}: {error}")
                emit_ui_log(f"[ENRICHMENT ERROR] {company}: {error}; saving posting without contact.")
                enrich = {
                    "website_url": None, "email": None,
                    "contact_evidence": {
                        "status": "failed", "checked_at": datetime.now(timezone.utc).isoformat(),
                        "email_status": "unconfirmed",
                    },
                }
            errors.extend(enrich.get("errors", []))
            enrichment_cache[company_location] = enrich
            if enrich["email"]:
                emit_ui_log(f"  ✉️ Contact found → {enrich['email']}")
            elif enrich["website_url"]:
                emit_ui_log(f"  🌐 Website found → {enrich['website_url']}")
            else:
                emit_ui_log(f"  ⏭️ No public contact found for {company}")

        lead["website_url"] = enrichment_cache[company_location]["website_url"]
        lead["email"] = enrichment_cache[company_location]["email"]
        lead["contact_evidence"] = enrichment_cache[company_location].get("contact_evidence")

    save_job_leads(all_leads)
    if errors:
        raise RuntimeError("Some hiring searches failed; valid postings were saved. " + " | ".join(errors))
    return len(all_leads)


def parse_job_posting(job, service_type, role):
    if not isinstance(job, dict):
        raise ValueError("Posting must be an object")
    def text(value, field):
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{field} must be text")
        return value.strip()
    company = text(job.get("company_name"), "company")
    title = text(job.get("title"), "title")
    if not company or not title:
        raise ValueError("Company and job title are required")
    extensions = job.get("detected_extensions") or {}
    options = job.get("apply_options") or []
    if not isinstance(extensions, dict) or not isinstance(options, list):
        raise ValueError("Invalid employment details or application links")
    if any(not isinstance(option, dict) for option in options):
        raise ValueError("Application links must be objects")
    applications = []
    for option in options:
        link = text(option.get("link"), "application link")
        valid_link = web_url(link)
        if link and not valid_link:
            from control_gate_api import emit_ui_log
            emit_ui_log("[JOB DATA] Ignored invalid application URL.")
        if valid_link:
            application = {"title": text(option.get("title"), "application title") or "Apply",
                           "link": valid_link}
            if application not in applications:
                applications.append(application)
    highlights = job.get("job_highlights") or []
    if not isinstance(highlights, list):
        raise ValueError("Job highlights must be a list")
    for highlight in highlights:
        if not isinstance(highlight, dict) or not isinstance(highlight.get("items", []), list):
            raise ValueError("Invalid job highlights")
        text(highlight.get("title"), "highlight title")
        for item in highlight.get("items", []):
            text(item, "highlight item")
    extra = job.get("extensions") or []
    if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
        raise ValueError("Extensions must be a list of text")
    share_link = text(job.get("share_link"), "share link")
    valid_share_link = web_url(share_link)
    if share_link and not valid_share_link:
        from control_gate_api import emit_ui_log
        emit_ui_log("[JOB DATA] Ignored invalid provider share URL.")
    details = {
        "provider_job_id": text(job.get("job_id"), "posting ID"),
        "posted_at": text(extensions.get("posted_at"), "posted date"),
        "detected_extensions": extensions, "extensions": extra,
        "job_highlights": highlights, "apply_options": applications,
        "share_link": valid_share_link,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    details = {key: value for key, value in details.items() if value not in (None, "", [], {})}
    return {
        "company_name": company,
        "job_title": title,
        "location": text(job.get("location"), "location") or "Unknown",
        "lead_type": service_type, "job_role_signal": role,
        "job_via": text(job.get("via"), "via"),
        "job_description": text(job.get("description"), "description"),
        "job_salary": text(extensions.get("salary") or extensions.get("salary_estimate"), "salary"),
        "job_schedule": text(extensions.get("schedule_type"), "schedule"),
        "job_apply_link": applications[0]["link"] if applications else "",
        "job_details": details,
    }
