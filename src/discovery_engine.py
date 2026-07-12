import os
import sys
import uuid
import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from email_hunter import attempt_domain_email_crawl
from screenshot_agent import capture_business_site_snapshot

# Initialize path mapping for environment tokens
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SERPAPI_KEY = os.getenv("SERPAPI_API_KEY")

def save_leads_to_db(leads):
    from control_gate_api import emit_ui_log
    if not leads:
        emit_ui_log("[DATABASE] No fresh, unique target segments ingested from this pass.")
        return

    insert_query = """
        INSERT INTO prospects (business_name, category, phone, formatted_address, google_place_id, review_count, rating, email, screenshot_path, status)
        VALUES %s
        ON CONFLICT (google_place_id) DO NOTHING;
    """
    
    data_tuples = [
        (
            lead['name'], 
            lead['category'], 
            lead['phone'], 
            lead['address'], 
            lead['place_id'], 
            lead['reviews'], 
            lead['rating'], 
            lead.get('email'),
            lead.get('screenshot_path'), # <-- Aligned identically to your lead dictionary read keys
            'discovered'
        )
        for lead in leads
    ]
    
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                execute_values(cur, insert_query, data_tuples)
                conn.commit()
                emit_ui_log("💾 [PostgreSQL] Live Synchronization complete. Saved fresh rows to DB.")
    except Exception as e:
        emit_ui_log(f"❌ [DB CRITICAL ERROR] Pipeline insertion breakdown: {e}")


def _find_facebook_email(business_name, address):
    """
    Searches for a business's Facebook page via SerpAPI and crawls it for a public email.
    Returns (email, facebook_url) — either value may be None.
    """
    if not SERPAPI_KEY:
        return None, None
    try:
        city = address.split(",")[-1].strip() if "," in address else address
        params = {
            "engine": "google",
            "q": f'site:facebook.com "{business_name}" "{city}"',
            "api_key": SERPAPI_KEY,
            "num": 3
        }
        response = requests.get("https://serpapi.com/search", params=params, timeout=10)
        response.raise_for_status()
        organic = response.json().get("organic_results", [])
        for result in organic:
            link = result.get("link", "")
            # Skip category hubs and sub-pages, target root business pages only
            if "facebook.com" in link and "/pages/category" not in link and "/groups/" not in link:
                email = attempt_domain_email_crawl(link)
                return email, link
    except Exception:
        pass
    return None, None


def _search_for_business_website(business_name, address):
    """
    Falls back to a SerpAPI Google search when no website is listed on Google Maps.
    Returns the first organic result URL, or None if nothing is found.
    """
    if not SERPAPI_KEY:
        return None
    try:
        params = {
            "engine": "google",
            "q": f"{business_name} {address} official website",
            "api_key": SERPAPI_KEY,
            "num": 3
        }
        response = requests.get("https://serpapi.com/search", params=params, timeout=10)
        response.raise_for_status()
        organic = response.json().get("organic_results", [])
        for result in organic:
            link = result.get("link", "")
            # Skip social media and directory aggregators — target the real business site
            if link and not any(skip in link for skip in ["facebook.com", "yelp.com", "yellowpages.com", "linkedin.com", "instagram.com", "twitter.com"]):
                return link
    except Exception:
        pass
    return None


def run_discovery(city, niche, rating_min=0.0, max_reviews=100, lat=None, lng=None, radius_km=10):
    """
    Queries SerpApi Google Maps framework utilizing precise geolocation coordinate pins 
    and custom radial distance tracking variables.
    """
    from control_gate_api import emit_ui_log
    if not SERPAPI_KEY:
        emit_ui_log("❌ [SERPAPI ERROR] Missing SERPAPI_API_KEY inside your .env configuration file.")
        return

    emit_ui_log(f"🛰️ Launching Google Maps scan for [{niche}] within {radius_km}km of pin ({lat or '0'}, {lng or '0'})...")

    # If coordinate mapping is hot, drop text parameters to keep radial sweep strict
    search_query = f"{niche}" if (lat and lng) else f"{niche} {city}"

    params = {
        "engine": "google_maps",
        "q": search_query,
        "type": "search",
        "api_key": SERPAPI_KEY
    }

    if lat and lng:
        params["ll"] = f"@{lat},{lng},12z"

    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=15)
        response.raise_for_status()
        search_results = response.json()    
    except Exception as e:
        emit_ui_log(f"❌ [SERPAPI FAILURE] Handshake network drop: {e}")
        return

    local_results = search_results.get("local_results", [])
    if not local_results:
        emit_ui_log(f"⚠️ No results returned for category '{niche}' in this geographic zone.")
        return

    # NEW DEDUPLICATION EFFICIENCY UPGRADE: Fetch all existing place IDs in a single query
    # to avoid hitting the database inside a loop
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT google_place_id FROM prospects WHERE google_place_id IS NOT NULL;")
                # Store as a set for O(1) instantaneous lookup speed
                existing_place_ids = set(row[0] for row in cur.fetchall())
    except Exception as e:
        print(f"[DEDUPLICATION WARNING] Failed to cache existing keys: {e}")
        existing_place_ids = set()

    valid_leads = []

    for business in local_results:
        place_token = business.get("data_id") or f"serp_{uuid.uuid4().hex[:10]}"

        # --- CORE DATABASE DEDUPLICATION CHECK ---
        if place_token in existing_place_ids:
            # Silently skip the lead before running costly website/screenshot operations
            print(f"[DEDUPLICATION] Skipping '{business.get('title')}' -> Already processed in active database.")
            continue
        
        business_name = business.get("title")
        website_link = business.get("website")
        phone_number = business.get("phone", "No Public Number Listed")
        address = business.get("address") or business.get("formatted_address") or f"Discovered region near pin ({lat or '0'}, {lng or '0'})"
        rating = float(business.get("rating", 0.0))
        reviews = int(business.get("reviews", 0))
        

        # --- WEBSITE EXTRACTION & EMAIL HUNTER FALLBACK PASS ---
        discovered_email = None
        screenshot_asset_url = None

        if not website_link:
            emit_ui_log(f"🔍 [No Website] Running Google search fallback for '{business_name}'...")
            website_link = _search_for_business_website(business_name, address)
            if website_link:
                emit_ui_log(f"   ↳ 🌐 Found via search: {website_link}")
            else:
                emit_ui_log(f"   ↳ 📘 No website found. Checking Facebook page for contact email...")
                discovered_email, fb_url = _find_facebook_email(business_name, address)
                if discovered_email:
                    emit_ui_log(f"   ↳ 🔥 Email found on Facebook: {discovered_email}")
                elif fb_url:
                    emit_ui_log(f"   ↳ ⚠️ Facebook page found but no public email listed. Saving without email.")
                else:
                    emit_ui_log(f"   ↳ ⚠️ No website or Facebook email found. Saving without email.")

        if website_link:
            emit_ui_log(f"🔗 [Website Found] Scouting '{business_name}' domain...")
            discovered_email = attempt_domain_email_crawl(website_link)

            if not discovered_email:
                emit_ui_log(f"   ↳ ⚠️ Site active but zero public email tags found. Saving without email.")
            else:
                emit_ui_log(f"   ↳ 🔥 Contact Extracted: {discovered_email}")

                # Trigger screenshot extraction immediately via headless selenium browser agent
                emit_ui_log(f"   ↳ 📸 Capturing live website snapshot profile view...")
                screenshot_asset_url = capture_business_site_snapshot(website_link, place_token)

        # --- PIPELINE METRICS FILTER MATRIX ---
        if rating < rating_min:
            emit_ui_log(f"   ↳ ⏭️ Skipped: Rating (★ {rating}) is below threshold cutoff.")
            continue

        if reviews > max_reviews:
            emit_ui_log(f"   ↳ ⏭️ Skipped: Review count ({reviews}) exceeds maximum selection boundary.")
            continue

        lead = {
            "name": business_name,
            "place_id": place_token,
            "address": address,
            "phone": phone_number,
            "rating": rating,
            "reviews": reviews,
            "category": niche,
            "email": discovered_email,
            "screenshot_path": screenshot_asset_url # <-- Properly append column variable payload
        }
        valid_leads.append(lead)
        emit_ui_log(f"✅ [Target Acquired] '{business_name}' locked to pipeline workspace.")

    save_leads_to_db(valid_leads)


if __name__ == "__main__":
    run_discovery("Austin, TX", "Roofing Contractor")