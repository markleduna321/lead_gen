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
PROXYCURL_KEY = os.getenv("PROXYCURL_API_KEY")

def save_linkedin_leads(leads):
    if not leads:
        return
    insert_query = """
        INSERT INTO prospects (business_name, category, phone, formatted_address, google_place_id, review_count, rating, status)
        VALUES %s
        ON CONFLICT (google_place_id) DO NOTHING;
    """
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            execute_values(cur, insert_query, leads)
            conn.commit()
            print(f"[DATABASE] LinkedIn sync complete. Ingested {len(leads)} high-ticket prospects.")

def run_linkedin_discovery(city, niche):
    """
    Queries Proxycurl's LinkedIn Company Search Engine to discover regional professional practices.
    """
    if not PROXYCURL_KEY:
        print("[LINKEDIN ERROR] Missing PROXYCURL_API_KEY inside your .env configuration.", file=sys.stderr)
        return

    print(f"[LINKEDIN ENGINE] Scanning Proxycurl indexes for '{niche}' in '{city}'...")

    # Proxycurl corporate endpoint target URL mapping layout
    url = "https://nubela.co/proxycurl/api/v2/linkedin/company/search"
    headers = {"Authorization": f"Bearer {PROXYCURL_KEY}"}
    
    # Configure granular search attributes matching your operational demographics
    params = {
        "keyword": niche,
        "location": city,
        "page_size": "10"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        results = response.json()
    except Exception as e:
        print(f"[LINKEDIN FAILURE] Proxycurl communication failure: {e}", file=sys.stderr)
        return

    companies = results.get("results", [])
    valid_leads = []

    for company_node in companies:
        # Proxycurl standard mapping format
        name = company_node.get("name")
        website = company_node.get("website")
        address = company_node.get("locations", [{}])[0].get("city", city)
        
        # LinkedIn data nodes rarely expose public review metrics directly. We normalize default trackers:
        rating = 0.0
        reviews = 0
        phone = "Check Profile"
        
        ln_id = f"ln_{company_node.get('id', uuid.uuid4().hex[:8])}"

        if not website:
            lead_tuple = (name, niche, phone, address, ln_id, reviews, rating, 'discovered')
            valid_leads.append(lead_tuple)
            print(f" -> [LN TARGET ACQUIRED] '{name}' Corporate entity lacks digital properties.")

    save_linkedin_leads(valid_leads)