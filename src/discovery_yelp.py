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


def save_yelp_leads(leads):
    from control_gate_api import emit_ui_log

    if not leads:
        emit_ui_log("[DATABASE] No fresh Yelp leads to ingest from this pass.")
        return

    insert_query = """
        INSERT INTO prospects (
            business_name, category, phone, formatted_address,
            google_place_id, review_count, rating,
            email, website_url, screenshot_path,
            source_platform, status
        )
        VALUES %s
        ON CONFLICT (google_place_id) DO NOTHING;
    """
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                execute_values(cur, insert_query, leads)
                conn.commit()
                emit_ui_log(f"💾 [PostgreSQL] Ingested {len(leads)} Yelp leads into the database.")
    except Exception as e:
        emit_ui_log(f"❌ [DB ERROR] Yelp batch write failure: {e}")


def run_yelp_discovery(city, niche):
    """
    Uses SerpAPI Yelp engine to discover local businesses matching the niche and city.
    Applies the same email + screenshot enrichment pipeline as Google Maps discovery.
    """
    from control_gate_api import emit_ui_log
    from email_hunter import attempt_domain_email_crawl
    from screenshot_agent import capture_business_site_snapshot

    if not SERPAPI_KEY:
        emit_ui_log("❌ [YELP ENGINE ERROR] Missing SERPAPI_API_KEY in your .env configuration.")
        return

    emit_ui_log(f"⭐ Launching Yelp discovery for [{niche}] in '{city}'...")

    params = {
        "engine": "yelp",
        "find_desc": niche,
        "find_loc": city,
        "api_key": SERPAPI_KEY,
    }

    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=15)
        response.raise_for_status()
        results = response.json()
    except Exception as e:
        emit_ui_log(f"❌ [YELP ENGINE FAILURE] API request failed: {e}")
        return

    organic = results.get("organic_results", [])
    if not organic:
        emit_ui_log(f"⚠️ No Yelp results returned for '{niche}' in '{city}'.")
        return

    # Pre-fetch existing place IDs to avoid duplicate enrichment work
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT google_place_id FROM prospects WHERE google_place_id IS NOT NULL;")
                existing_ids = set(row[0] for row in cur.fetchall())
    except Exception as e:
        emit_ui_log(f"[DEDUP WARNING] Could not prefetch IDs: {e}")
        existing_ids = set()

    valid_leads = []

    for biz in organic:
        title = (biz.get("title") or "").strip()
        if not title:
            continue

        # Build a deterministic unique ID from name + city for dedup
        place_token = f"yelp_{uuid.uuid5(uuid.NAMESPACE_DNS, title.lower() + city.lower()).hex[:16]}"

        if place_token in existing_ids:
            emit_ui_log(f"[DEDUP] Skipping '{title}' — already in database.")
            continue

        # Extract fields from SerpAPI Yelp result
        rating     = float(biz.get("rating") or 0.0)
        reviews    = int(biz.get("reviews") or 0)
        phone      = (biz.get("phone") or "No Public Number Listed").strip()

        # Address: some results have 'address', others 'neighborhoods'
        address = (
            biz.get("address")
            or biz.get("neighborhoods")
            or f"General Area, {city}"
        )

        # Website comes under links.website in SerpAPI Yelp results
        links = biz.get("links") or {}
        website_link = links.get("website") or biz.get("website") or ""

        # Category label
        categories = biz.get("categories") or []
        category_label = categories[0].get("title", niche) if categories else niche

        # --- EMAIL + SCREENSHOT ENRICHMENT ---
        discovered_email = None
        screenshot_asset_url = None

        if website_link:
            emit_ui_log(f"🔗 [Yelp] Scouting '{title}' domain for contact...")
            discovered_email = attempt_domain_email_crawl(website_link)
            if discovered_email:
                emit_ui_log(f"   ↳ 🔥 Email found: {discovered_email}")
                emit_ui_log(f"   ↳ 📸 Capturing site snapshot...")
                screenshot_asset_url = capture_business_site_snapshot(website_link, place_token)
            else:
                emit_ui_log(f"   ↳ ⚠️ Site found but no public email. Saving without email.")
        else:
            emit_ui_log(f"   ↳ 🔍 No website on Yelp for '{title}'. Saving as discovered.")

        valid_leads.append((
            title,
            category_label,
            phone,
            address,
            place_token,
            reviews,
            rating,
            discovered_email,
            website_link or None,
            screenshot_asset_url,
            "yelp",
            "discovered",
        ))
        emit_ui_log(f"✅ [Yelp Target] '{title}' locked to pipeline.")

    save_yelp_leads(valid_leads)
