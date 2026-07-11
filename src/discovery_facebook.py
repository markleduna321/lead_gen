import os
import sys
import uuid
import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv


# Initialize path mapping for environment tokens
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SERPAPI_KEY = os.getenv("SERPAPI_API_KEY")

def save_facebook_leads(leads):
    from control_gate_api import emit_ui_log
    
    if not leads:
        emit_ui_log("[DATABASE] No fresh Facebook leads to ingest from this pass.")
        return
    insert_query = """
        INSERT INTO prospects (business_name, category, phone, formatted_address, google_place_id, review_count, rating, status)
        VALUES %s
        ON CONFLICT (google_place_id) DO NOTHING;
    """
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                execute_values(cur, insert_query, leads)
                conn.commit()
                emit_ui_log(f"💾 [PostgreSQL] Ingested {len(leads)} target profiles from Facebook organic logs.")
    except Exception as e:
        emit_ui_log(f"❌ [DB ERROR] Facebook batch write failure: {e}")

def run_facebook_discovery(city, niche):
    """
    Leverages SerpApi Google Organic Search operators to bypass Meta Developer platform locks,
    discovering active regional Facebook pages matching high-intent niches.
    """
    from control_gate_api import emit_ui_log
    
    if not SERPAPI_KEY:
        emit_ui_log("❌ [FACEBOOK ENGINE ERROR] Missing SERPAPI_API_KEY inside your environment settings.")
        return

    emit_ui_log(f"🛰️ Launching organic Facebook index sweep for [{niche}] in '{city}'...")

    # Force Google to return pure facebook page entries matching criteria rules
    search_query = f'site:facebook.com "{niche}" "{city}"'
    
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
        emit_ui_log(f"❌ [FB ENGINE FAILURE] Search engine routing dropped: {e}")
        return

    organic_results = search_results.get("organic_results", [])
    if not organic_results:
        emit_ui_log(f"⚠️ Zero organic Facebook links indexing for category target: {niche}")
        return

    valid_leads = []

    for result in organic_results:
        title = result.get("title", "")
        link = result.get("link", "")
        snippet = result.get("snippet", "")

        # Wipe trailing platform titles cleanly
        business_name = title.split("|")[0].split("-")[0].replace("Home", "").replace("Facebook", "").strip()
        
        # Avoid processing platform structural index paths or sub-category hubs
        if not business_name or "pages/category" in link:
            continue

        phone = "Check FB Page"
        address = f"General Area, {city}"
        rating = 0.0
        reviews = 0
        
        # Create unique identifier prefixed for UI logic source badge mapping
        fb_token = f"fb_serp_{uuid.uuid4().hex[:8]}"

        lead_tuple = (business_name, niche, phone, address, fb_token, reviews, rating, 'discovered')
        valid_leads.append(lead_tuple)
        emit_ui_log(f"✅ [FB Page Locked] '{business_name}' -> Indexed via Google site operators.")

    save_facebook_leads(valid_leads)