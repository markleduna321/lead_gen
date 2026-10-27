"""Find public LinkedIn company pages through Scrapingdog's Google Search API."""

import os
import re

import psycopg2
from psycopg2.extras import execute_values
from billing import insert_and_track
from dotenv import load_dotenv
from discovery_filters import attach_owner, deduplicate_prospect_rows
from discovery_industries import matching_industry_category
from discovery_profiles import canonical_social_profile, load_existing_social_ids
from scrapingdog_client import fetch as scrapingdog_fetch

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SCRAPINGDOG_KEY = os.getenv("SCRAPINGDOG_API_KEY") or os.getenv("SERPAPI_API_KEY")


def save_linkedin_leads(leads, user_id):
    from control_gate_api import emit_ui_log

    if not leads:
        emit_ui_log("[DATABASE] No fresh LinkedIn company pages to ingest from this pass.")
        return

    insert_query = """
        INSERT INTO prospects (
            business_name, category, phone, formatted_address, google_place_id,
            review_count, rating, profile_url, source_platform, status, user_id
        )
        VALUES %s
        ON CONFLICT (user_id, google_place_id) DO UPDATE SET last_discovered_at = NOW();
    """
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                rows = attach_owner(deduplicate_prospect_rows(leads, emit_ui_log), user_id)
                insert_and_track(cur, insert_query, rows, executor=execute_values)
                conn.commit()
                emit_ui_log(f"💾 [PostgreSQL] Ingested {len(rows)} LinkedIn company pages.")
    except Exception as error:
        emit_ui_log(f"❌ [DB ERROR] LinkedIn batch write failure: {error}")
        raise


def run_linkedin_discovery(city, niche, user_id=None):
    """Search indexed public LinkedIn company pages; this does not access LinkedIn accounts."""
    from control_gate_api import emit_ui_log

    if not SCRAPINGDOG_KEY:
        emit_ui_log("[LINKEDIN ERROR] Missing SCRAPINGDOG_API_KEY in the environment settings.")
        raise RuntimeError("LinkedIn discovery requires SCRAPINGDOG_API_KEY.")
    if user_id is None:
        raise ValueError("LinkedIn discovery requires a signed-in account.")
    if not isinstance(city, str) or not city.strip():
        raise ValueError("LinkedIn discovery requires a location.")
    if not isinstance(niche, str) or not niche.strip():
        raise ValueError("LinkedIn discovery requires an industry.")

    city = city.strip()
    niche = niche.strip()
    query = f'site:linkedin.com/company/ "{niche}" "{city}"'
    emit_ui_log(f"🔎 Searching indexed LinkedIn company pages for [{niche}] near '{city}'...")
    try:
        response = scrapingdog_fetch(
            {"query": query, "location": city, "page": 0, "api_key": SCRAPINGDOG_KEY},
            timeout=30,
            log=emit_ui_log,
            label=f"LinkedIn company search for '{niche}'",
        )
        results = response.json()
        if not isinstance(results, dict):
            raise RuntimeError("Scrapingdog returned an invalid LinkedIn search response.")
        if results.get("error"):
            raise RuntimeError(str(results["error"]))
    except Exception as error:
        emit_ui_log(f"❌ [LINKEDIN SEARCH FAILURE] Scrapingdog search failed: {error}")
        raise

    organic_results = results.get("organic_results", [])
    if not isinstance(organic_results, list):
        raise RuntimeError("Scrapingdog returned malformed LinkedIn search results.")
    if not organic_results:
        emit_ui_log(f"⚠️ No indexed LinkedIn company pages found for [{niche}] in '{city}'.")
        return 0

    try:
        existing_ids = load_existing_social_ids(DB_DSN, "linkedin", user_id)
    except psycopg2.Error as error:
        emit_ui_log(f"[LINKEDIN DEDUP ERROR] Cannot check existing pages; search stopped: {error}")
        raise

    leads = []
    for result in organic_results:
        if not isinstance(result, dict):
            emit_ui_log("[LINKEDIN SEARCH] Skipped malformed search result.")
            continue
        title = result.get("title")
        link = result.get("link")
        snippet = result.get("snippet")
        if not isinstance(title, str) or not isinstance(link, str):
            emit_ui_log("[LINKEDIN SEARCH] Skipped a result without a valid title or URL.")
            continue
        if not isinstance(snippet, str):
            snippet = ""
        business_name = re.sub(r"\s*(?:\||-)\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()
        if not business_name:
            continue
        if matching_industry_category(
            {"title": business_name, "snippet": snippet}, niche, "linkedin", emit_ui_log
        ) is None:
            continue
        try:
            profile_id, profile_url = canonical_social_profile("linkedin", link)
        except ValueError as error:
            emit_ui_log(f"[LINKEDIN PROFILE] Skipped '{business_name}': {error}")
            continue
        if profile_id in existing_ids:
            emit_ui_log(f"[DEDUP] Skipping LinkedIn company '{business_name}': already discovered.")
            continue
        existing_ids.add(profile_id)
        leads.append((
            business_name,
            niche,
            None,
            f"General Area, {city}",
            profile_id,
            None,
            None,
            profile_url,
            "linkedin",
            "discovered",
        ))
        emit_ui_log(f"✅ [LinkedIn Company] '{business_name}' → Indexed via Google site search.")

    save_linkedin_leads(leads, user_id)
    return len(leads)
