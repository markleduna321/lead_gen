import os
import sys
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from discovery_profiles import canonical_social_profile, load_existing_social_ids
from discovery_industries import matching_industry_category
from serpapi_client import fetch as serpapi_fetch

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SERPAPI_KEY = os.getenv("SERPAPI_API_KEY")


def save_instagram_leads(leads, user_id):
    from control_gate_api import emit_ui_log
    from discovery_filters import attach_owner, deduplicate_prospect_rows

    if not leads:
        emit_ui_log("[DATABASE] No fresh Instagram leads to ingest from this pass.")
        return

    insert_query = """
        INSERT INTO prospects (business_name, category, phone, formatted_address, google_place_id, review_count, rating, email, profile_url, source_platform, status, user_id)
        VALUES %s
        ON CONFLICT (user_id, google_place_id) DO UPDATE SET last_discovered_at = NOW();
    """
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                execute_values(cur, insert_query,
                               attach_owner(deduplicate_prospect_rows(leads, emit_ui_log), user_id))
                conn.commit()
                emit_ui_log(f"💾 [PostgreSQL] Ingested {len(leads)} target profiles from Instagram organic sweep.")
    except Exception as e:
        emit_ui_log(f"❌ [DB ERROR] Instagram batch write failure: {e}")
        raise


def run_instagram_discovery(city, niche, user_id=None):
    """
    Uses SerpAPI Google organic search operators to surface active Instagram business
    profiles matching the target niche and city.
    """
    from control_gate_api import emit_ui_log
    from email_hunter import attempt_domain_email_crawl

    if not SERPAPI_KEY:
        emit_ui_log("❌ [INSTAGRAM ENGINE ERROR] Missing SERPAPI_API_KEY inside your environment settings.")
        raise RuntimeError("Instagram discovery requires SERPAPI_API_KEY.")

    emit_ui_log(f"📸 Launching organic Instagram index sweep for [{niche}] in '{city}'...")

    search_query = f'site:instagram.com "{niche}" "{city}"'

    params = {
        "engine": "google",
        "q": search_query,
        "api_key": SERPAPI_KEY,
        "num": 10
    }

    try:
        response = serpapi_fetch(params, timeout=30, log=emit_ui_log,
                                 label=f"Instagram search for '{niche}'")
        search_results = response.json()
        if search_results.get("error"):
            raise RuntimeError(search_results["error"])
    except Exception as e:
        emit_ui_log(f"❌ [IG ENGINE FAILURE] Search engine routing dropped: {e}")
        raise

    organic_results = search_results.get("organic_results", [])
    if not organic_results:
        emit_ui_log(f"⚠️ Zero organic Instagram profiles indexed for: {niche} in {city}")
        return

    valid_leads = []
    try:
        existing_ids = load_existing_social_ids(DB_DSN, "instagram", user_id)
    except psycopg2.Error as error:
        emit_ui_log(f"[IG DEDUP ERROR] Cannot check existing profiles; search stopped: {error}")
        raise

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
        if matching_industry_category(result, niche, "instagram", emit_ui_log) is None:
            continue
        try:
            ig_token, profile_url = canonical_social_profile("instagram", link)
        except ValueError as error:
            emit_ui_log(f"[IG PROFILE] Skipped '{business_name}': {error}")
            continue
        if ig_token in existing_ids:
            emit_ui_log(f"[DEDUP] Skipping Instagram profile '{business_name}': already discovered.")
            continue
        existing_ids.add(ig_token)

        # Try to extract email from the snippet first (some profiles expose it in meta)
        import re
        EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        snippet_emails = re.findall(EMAIL_REGEX, snippet)
        discovered_email = snippet_emails[0].lower() if snippet_emails else None

        if not discovered_email:
            # Attempt crawl on the profile page directly
            discovered_email = attempt_domain_email_crawl(profile_url)

        if discovered_email:
            emit_ui_log(f"   ↳ 🔥 Email found for '{business_name}': {discovered_email}")
        else:
            emit_ui_log(f"✅ [IG Profile Locked] '{business_name}' → Saved without email.")

        lead_tuple = (
            business_name,
            niche,
            "Check IG Profile",
            f"General Area, {city}",
            ig_token,
            None,
            None,
            discovered_email,
            profile_url,
            'instagram',
            'discovered'
        )
        valid_leads.append(lead_tuple)
        emit_ui_log(f"✅ [IG Profile Locked] '{business_name}' → Indexed via Google site operators.")

    save_instagram_leads(valid_leads, user_id)
    return len(valid_leads)
