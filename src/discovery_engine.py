import os
import sys
import uuid
import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from email_hunter import attempt_domain_email_crawl
from screenshot_agent import capture_business_site_snapshot
from discovery_filters import filter_business_metrics, within_search_radius
from discovery_industries import matching_industry_category

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
        INSERT INTO prospects (business_name, category, phone, formatted_address, google_place_id, review_count, rating, email, screenshot_path, website_url, source_platform, status)
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
            lead.get('website_url'),
            'google_maps',
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
        raise


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
        data = response.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        organic = data.get("organic_results", [])
        for result in organic:
            link = result.get("link", "")
            # Skip category hubs and sub-pages, target root business pages only
            if "facebook.com" in link and "/pages/category" not in link and "/groups/" not in link:
                email = attempt_domain_email_crawl(link)
                return email, link
    except Exception as error:
        from control_gate_api import emit_ui_log
        emit_ui_log(f"[ENRICHMENT ERROR] Facebook contact search failed for '{business_name}': {error}")
        raise
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
        data = response.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        organic = data.get("organic_results", [])
        for result in organic:
            link = result.get("link", "")
            # Skip social media and directory aggregators — target the real business site
            if link and not any(skip in link for skip in ["facebook.com", "yelp.com", "yellowpages.com", "linkedin.com", "instagram.com", "twitter.com"]):
                return link
    except Exception as error:
        from control_gate_api import emit_ui_log
        emit_ui_log(f"[ENRICHMENT ERROR] Website search failed for '{business_name}': {error}")
        raise
    return None


def run_discovery(city, niche, rating_min: float = 0.0, max_reviews: int | None = 100, lat=None, lng=None, radius_km=10):
    """
    Queries SerpApi Google Maps framework utilizing precise geolocation coordinate pins 
    and custom radial distance tracking variables.
    """
    from control_gate_api import emit_ui_log
    if not SERPAPI_KEY:
        emit_ui_log("❌ [SERPAPI ERROR] Missing SERPAPI_API_KEY inside your .env configuration file.")
        raise RuntimeError("Google Maps discovery requires SERPAPI_API_KEY.")

    emit_ui_log(f"🛰️ Launching Google Maps scan for [{niche}] within {radius_km}km of pin ({lat or '0'}, {lng or '0'})...")

    # If coordinate mapping is hot, drop text parameters to keep radial sweep strict
    has_coordinates = lat is not None and lng is not None
    search_query = f"{niche}" if has_coordinates else f"{niche} {city}"

    params = {
        "engine": "google_maps",
        "q": search_query,
        "type": "search",
        "api_key": SERPAPI_KEY
    }

    if has_coordinates:
        params["ll"] = f"@{lat},{lng},{radius_km * 2000}m"
        params["nearby"] = True

    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=15)
        response.raise_for_status()
        search_results = response.json()
        if search_results.get("error"):
            raise RuntimeError(search_results["error"])
    except Exception as e:
        emit_ui_log(f"❌ [SERPAPI FAILURE] Handshake network drop: {e}")
        raise

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
        emit_ui_log(f"[DEDUPLICATION ERROR] Failed to cache existing keys: {e}")
        raise

    valid_leads = []

    for business in local_results:
        business_name = business.get("title")
        address = business.get("address") or business.get("formatted_address") or f"Discovered region near pin ({lat or '0'}, {lng or '0'})"
        place_token = business.get("data_id")
        if not place_token:
            fallback_identity = "\x1f".join(
                " ".join(str(value or "").casefold().split())
                for value in (business_name, address)
            )
            place_token = f"serp_{uuid.uuid5(uuid.NAMESPACE_URL, fallback_identity).hex}"

        # --- CORE DATABASE DEDUPLICATION CHECK ---
        if place_token in existing_place_ids:
            # Silently skip the lead before running costly website/screenshot operations
            print(f"[DEDUPLICATION] Skipping '{business.get('title')}' -> Already processed in active database.")
            continue
        
        website_link = business.get("website")
        phone_number = business.get("phone", "No Public Number Listed")
        if has_coordinates and not within_search_radius(business, lat, lng, radius_km, emit_ui_log):
            continue
        metrics = filter_business_metrics(business, rating_min, max_reviews, emit_ui_log)
        if metrics is None:
            continue
        rating, reviews = metrics
        category = matching_industry_category(business, niche, "google_maps", emit_ui_log)
        if category is None:
            continue
        

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

        lead = {
            "name": business_name,
            "place_id": place_token,
            "address": address,
            "phone": phone_number,
            "rating": rating,
            "reviews": reviews,
            "category": category,
            "email": discovered_email,
            "website_url": website_link,
            "screenshot_path": screenshot_asset_url # <-- Properly append column variable payload
        }
        valid_leads.append(lead)
        emit_ui_log(f"✅ [Target Acquired] '{business_name}' locked to pipeline workspace.")

    save_leads_to_db(valid_leads)


if __name__ == "__main__":
    run_discovery("Austin, TX", "Roofing Contractor")