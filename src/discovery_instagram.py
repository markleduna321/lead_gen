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


def save_instagram_leads(leads):
    from control_gate_api import emit_ui_log

    if not leads:
        emit_ui_log("[DATABASE] No fresh Instagram leads to ingest from this pass.")
        return

    insert_query = """
        INSERT INTO prospects (business_name, category, phone, formatted_address, google_place_id, review_count, rating, source_platform, status)
        VALUES %s
        ON CONFLICT (google_place_id) DO NOTHING;
    """
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                execute_values(cur, insert_query, leads)
                conn.commit()
                emit_ui_log(f"💾 [PostgreSQL] Ingested {len(leads)} target profiles from Instagram organic sweep.")
    except Exception as e:
        emit_ui_log(f"❌ [DB ERROR] Instagram batch write failure: {e}")


def run_instagram_discovery(city, niche):
    """
    Uses SerpAPI Google organic search operators to surface active Instagram business
    profiles matching the target niche and city.
    """
    from control_gate_api import emit_ui_log
    from email_hunter import attempt_domain_email_crawl

    if not SERPAPI_KEY:
        emit_ui_log("❌ [INSTAGRAM ENGINE ERROR] Missing SERPAPI_API_KEY inside your environment settings.")
        return

    emit_ui_log(f"📸 Launching organic Instagram index sweep for [{niche}] in '{city}'...")

    search_query = f'site:instagram.com "{niche}" "{city}"'

    params = {
        "engine": "google",
        "q": search_query,
        "api_key": SERPAPI_KEY,
        "num": 10
    }

    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=15)
        response.raise_for_status()
        search_results = response.json()
    except Exception as e:
        emit_ui_log(f"❌ [IG ENGINE FAILURE] Search engine routing dropped: {e}")
        return

    organic_results = search_results.get("organic_results", [])
    if not organic_results:
        emit_ui_log(f"⚠️ Zero organic Instagram profiles indexed for: {niche} in {city}")
        return

    valid_leads = []

    for result in organic_results:
        title = result.get("title", "")
        link = result.get("link", "")
        snippet = result.get("snippet", "")

        # Skip reels, explore pages, and non-profile paths
        if not link or any(skip in link for skip in ["/reel/", "/p/", "/explore/", "/stories/"]):
            continue

        # Clean up title — Instagram profiles usually follow "Name (@handle) • Instagram"
        business_name = title.split("•")[0].split("(")[0].strip()
        if not business_name:
            continue

        # Try to extract email from the snippet first (some profiles expose it in meta)
        import re
        EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        snippet_emails = re.findall(EMAIL_REGEX, snippet)
        discovered_email = snippet_emails[0].lower() if snippet_emails else None

        if not discovered_email:
            # Attempt crawl on the profile page directly
            discovered_email = attempt_domain_email_crawl(link)

        if discovered_email:
            emit_ui_log(f"   ↳ 🔥 Email found for '{business_name}': {discovered_email}")
        else:
            emit_ui_log(f"✅ [IG Profile Locked] '{business_name}' → Saved without email.")

        ig_token = f"ig_serp_{uuid.uuid4().hex[:8]}"

        lead_tuple = (
            business_name,
            niche,
            "Check IG Profile",
            f"General Area, {city}",
            ig_token,
            0,    # review_count
            0.0,  # rating
            'instagram',
            'discovered'
        )
        valid_leads.append(lead_tuple)
        emit_ui_log(f"✅ [IG Profile Locked] '{business_name}' → Indexed via Google site operators.")

    save_instagram_leads(valid_leads)
