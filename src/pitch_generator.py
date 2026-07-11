import os
import sys
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

# CORRECTED: Changed 'load_file' to 'load_dotenv'
from dotenv import load_dotenv

# Resolve root .env path dynamically
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SYSTEM_PROMPT = """
You are an elite, highly professional B2B technical strategy consultant. Your job is to draft a short, compelling cold outreach message to a business owner who has a stellar local reputation but NO website.

Strict Copywriting Constraints:
1. NEVER use generic marketing fluff or buzzwords (e.g., "skyrocket", "game-changer", "synergy", "digital age", "look no further").
2. Acknowledge their exact local traction by referencing their specific review count and rating.
3. State an undeniable business penalty of lacking an online hub (e.g., losing high-intent search volume to inferior competitors who have a website).
4. Do not offer code or tech stacks (WordPress, React). Offer a business SOLUTION (e.g., automated booking, lead generation systems).
5. Length must be under 150 words. Focus entirely on a low-friction Call to Action (CTA), such as sending over a 2-minute interactive mockup.
"""

def fetch_discovered_leads(limit=5):
    query = "SELECT id, business_name, category, review_count, rating FROM prospects WHERE status = 'discovered' LIMIT %s;"
    try:
        with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (limit,))
                return cur.fetchall()
    except Exception as e:
        print(f"[DATABASE ERROR] Queue read error: {e}", file=sys.stderr)
        return []

def execute_openai_generation(user_context):
    """Hits OpenAIChat Completion Endpoint using native HTTP payload architecture."""
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini", # Hyper-efficient, ultra-low cost professional model routing
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_context}
        ],
        "temperature": 0.7
    }
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"[OPENAI FAILURE] Could not generate response content: {e}", file=sys.stderr)
        return None

def commit_pitch_to_lead(lead_id, angle, pitch_text):
    query = "UPDATE prospects SET suggested_angle = %s, ai_pitch_draft = %s, status = 'drafted' WHERE id = %s;"
    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (angle, pitch_text, lead_id))
                conn.commit()
    except Exception as e:
        print(f"[DATABASE ERROR] Pitch sync rollback: {e}", file=sys.stderr)

def run_copywriter_pipeline():
    leads = fetch_discovered_leads()
    if not leads:
        print("[AI WORKER] Queue processing idle. Zero 'discovered' rows await synthesis.")
        return

    print(f"[AI WORKER] Sending {len(leads)} targets to gpt-4o-mini for personalization...")
    for lead in leads:
        angle = f"Focus on localized customer generation and automated contact routing hooks for {lead['category']} operations."
        
        user_context = f"Business Context:\n- Name: {lead['business_name']}\n- Type: {lead['category']}\n- Rating: {lead['rating']} (From {lead['review_count']} Reviews)"
        
        pitch_content = execute_openai_generation(user_context)
        if pitch_content:
            commit_pitch_to_lead(lead['id'], angle, pitch_content)
            print(f" -> [AI ASSET GENERATED] Custom tailored pitch locked down for: {lead['business_name']}")

if __name__ == "__main__":
    run_copywriter_pipeline()