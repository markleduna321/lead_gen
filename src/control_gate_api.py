import os
import sys
import csv
import io
import smtplib
import requests
import threading
import psycopg2
from email.mime.text import MIMEText
from email.header import Header
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template, Response
from dotenv import load_dotenv
from discovery_runs import DiscoveryBusyError, LocalDiscoveryRuns

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SYSTEM_LOG_BUFFER = []
LOCAL_DISCOVERY_RUNS = LocalDiscoveryRuns()
HIRING_DISCOVERY_RUNS = LocalDiscoveryRuns("hiring")

def emit_ui_log(message):
    """Appends an operational milestone directly to the user viewport queue."""
    global SYSTEM_LOG_BUFFER
    print(message) # Keep standard terminal output intact
    SYSTEM_LOG_BUFFER.append(message)
    LOCAL_DISCOVERY_RUNS.record_log(message)
    HIRING_DISCOVERY_RUNS.record_log(message)
    # Keep the live cache clean (trim past 50 entries)
    if len(SYSTEM_LOG_BUFFER) > 50:
        SYSTEM_LOG_BUFFER.pop(0)

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = str(os.getenv("SMTP_USER")).lower().strip()
SMTP_PASS = os.getenv("SMTP_PASSWORD")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

app = Flask(__name__, template_folder='templates', static_folder='../static')


def run_startup_migrations():
    """
    Applies all Phase 2 schema migrations automatically on app startup.
    Uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so it is safe to re-run.
    """
    if not DB_DSN:
        print("[STARTUP] DATABASE_URL not set — skipping migrations.", file=sys.stderr)
        return

    migrations = [
        # Widen narrow columns that can overflow with real-world job/company data
        "ALTER TABLE prospects ALTER COLUMN category TYPE VARCHAR(255);",
        "ALTER TABLE prospects ALTER COLUMN business_name TYPE VARCHAR(512);",
        # Phase 2 new columns
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS lead_score         INT           DEFAULT 0;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS site_quality_score  INT           DEFAULT NULL;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS lead_type           VARCHAR(50)   DEFAULT 'web_design';",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS subject_line        TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS followup_due_at     TIMESTAMP     DEFAULT NULL;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS website_url         TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS profile_url         TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS source_platform     VARCHAR(50)   DEFAULT 'google_maps';",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS email               TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS screenshot_path     TEXT;",
        # Job board enrichment columns
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_via         TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_salary      TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_schedule    TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_description TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_apply_link  TEXT;",
        "CREATE INDEX IF NOT EXISTS idx_prospects_lead_score ON prospects (lead_score DESC);",
        "CREATE INDEX IF NOT EXISTS idx_prospects_lead_type  ON prospects (lead_type);",
        "CREATE INDEX IF NOT EXISTS idx_prospects_followup   ON prospects (followup_due_at) WHERE followup_due_at IS NOT NULL;",
        # Phase 4: Dedicated job_leads table (idempotent — safe to re-run every startup)
        """
        CREATE TABLE IF NOT EXISTS job_leads (
            id              SERIAL PRIMARY KEY,
            company_name    VARCHAR(512)  NOT NULL,
            job_title       VARCHAR(512)  NOT NULL,
            location        TEXT,
            lead_type       VARCHAR(50)   NOT NULL,
            job_role_signal VARCHAR(255),
            job_via         TEXT,
            job_salary      TEXT,
            job_schedule    TEXT,
            job_description TEXT,
            job_apply_link  TEXT,
            website_url     TEXT,
            email           TEXT,
            suggested_angle TEXT,
            status          VARCHAR(50)   DEFAULT 'discovered',
            ai_pitch_draft  TEXT,
            subject_line    TEXT,
            source_platform VARCHAR(50)   DEFAULT 'job_board',
            job_identity    TEXT,
            job_details     JSONB,
            contact_evidence JSONB,
            created_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_identity TEXT;",
        "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_details JSONB;",
        "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS contact_evidence JSONB;",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_leads_identity ON job_leads (job_identity);",
        "ALTER TABLE job_leads DROP CONSTRAINT IF EXISTS unique_job_lead;",
        """
        CREATE TABLE IF NOT EXISTS hiring_services (
            service_id VARCHAR(50) PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            name_key TEXT NOT NULL UNIQUE,
            roles JSONB NOT NULL CHECK (jsonb_typeof(roles) = 'array' AND jsonb_array_length(roles) BETWEEN 1 AND 4),
            title_phrases JSONB NOT NULL CHECK (jsonb_typeof(title_phrases) = 'array' AND jsonb_array_length(title_phrases) BETWEEN 1 AND 12),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_job_leads_status    ON job_leads (status);",
        "CREATE INDEX IF NOT EXISTS idx_job_leads_lead_type ON job_leads (lead_type);",
        "CREATE INDEX IF NOT EXISTS idx_job_leads_email     ON job_leads (email) WHERE email IS NOT NULL;",
        "DROP TRIGGER IF EXISTS update_job_leads_modtime ON job_leads;",
        """
        CREATE TRIGGER update_job_leads_modtime
            BEFORE UPDATE ON job_leads
            FOR EACH ROW
            EXECUTE FUNCTION update_modified_column();
        """,
    ]

    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                for sql in migrations:
                    cur.execute(sql)
            conn.commit()
        print("[STARTUP] ✅ Schema migrations applied successfully.")
    except Exception as e:
        print(f"[STARTUP] ⚠️ Migration warning (non-fatal): {e}", file=sys.stderr)


run_startup_migrations()

@app.route('/api/discovery/logs', methods=['GET'])
def fetch_live_ui_logs():
    """Exposes current status logs to the frontend UI dashboard."""
    global SYSTEM_LOG_BUFFER
    return jsonify({
        "status": "success",
        "logs": SYSTEM_LOG_BUFFER
    })

@app.route('/api/discovery/logs/clear', methods=['POST'])
def clear_ui_logs():
    """Wipes the pipeline logs clean before initiating a new discovery pass."""
    global SYSTEM_LOG_BUFFER
    SYSTEM_LOG_BUFFER.clear()
    return jsonify({"status": "success"})

@app.errorhandler(Exception)
def handle_global_exception(e):
    print(f"[SERVER CRASH ERROR] {str(e)}", file=sys.stderr)
    return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', defaults={'page': 'overview'}, methods=['GET'])
@app.route('/prospects', defaults={'page': 'prospects'}, methods=['GET'])
@app.route('/job-leads', defaults={'page': 'jobs'}, methods=['GET'])
@app.route('/discovery', defaults={'page': 'discovery'}, methods=['GET'])
def serve_dashboard(page):
    from hiring_services import BUILTIN_SERVICES
    pages = {
        'overview': ('Overview', 'YOUR PIPELINE, AT A GLANCE',
                     'A clear view of your leads, outreach, and next opportunities.'),
        'prospects': ('Prospects', 'LOCAL BUSINESS PIPELINE',
                      'Review businesses, personalize your outreach, and build meaningful connections.'),
        'jobs': ('Job Leads', 'HIRING SIGNALS',
                 'Explore companies actively hiring for the services you offer.'),
        'discovery': ('Discovery', 'FIND YOUR NEXT OPPORTUNITY',
                      'Two ways to find better leads. Choose your audience and start a focused search.'),
    }
    page_title, page_eyebrow, page_description = pages[page]
    return render_template(
        'dashboard.html',
        page=page,
        page_title=page_title,
        page_eyebrow=page_eyebrow,
        page_description=page_description,
        hiring_builtin_services=[service.public_dict() for service in BUILTIN_SERVICES.values()],
        carto_basemap_api_key=os.getenv('CARTO_BASEMAP_API_KEY', '')
    )

@app.route('/api/trigger/discover', methods=['POST'])
def trigger_discovery_engine():
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Discovery expects a JSON object."}), 400
    target_location = data.get('location')
    target_categories = data.get('categories', [])
    selected_sources = data.get('sources', ['google'])
    if not isinstance(selected_sources, list) or not selected_sources:
        return jsonify({"status": "error", "message": "Select at least one supported discovery data source."}), 400
    if 'linkedin' in selected_sources:
        return jsonify({"status": "error", "message":
            "LinkedIn discovery is not available. Select Google Maps, Facebook, Instagram, or Yelp."}), 400
    if any(source not in ('google', 'facebook', 'instagram', 'yelp') for source in selected_sources):
        return jsonify({"status": "error", "message": "Unsupported discovery data source selected."}), 400
    
    try:
        raw_rating = data.get('rating_min', 0.0)
        raw_reviews = data.get('max_reviews', 100)
        rating_min = float(raw_rating)
        max_reviews = None if raw_reviews is None else int(raw_reviews)
        if isinstance(raw_rating, bool) or not 0 <= rating_min <= 5:
            raise ValueError("Minimum rating must be between 0 and 5.")
        if max_reviews is not None and (
            isinstance(raw_reviews, bool) or max_reviews < 0 or float(raw_reviews) != max_reviews
        ):
            raise ValueError("Maximum reviews must be a nonnegative integer or null for unlimited.")
    except (ValueError, TypeError, OverflowError):
        return jsonify({"status": "error", "message":
            "Invalid filters: minimum rating must be 0-5; maximum reviews must be a nonnegative integer or null for unlimited."}), 400
    
    lat = data.get('lat')
    lng = data.get('lng')
    raw_radius = data.get('radius_km', 10)
    radius_km = 10
    if 'google' in selected_sources:
        try:
            lat, lng = float(lat), float(lng)
            radius_km = int(raw_radius)
            if (
                isinstance(data.get('lat'), bool) or isinstance(data.get('lng'), bool)
                or not -90 <= lat <= 90 or not -180 <= lng <= 180
                or isinstance(raw_radius, bool) or not 2 <= radius_km <= 50
                or float(raw_radius) != radius_km
            ):
                raise ValueError("Invalid map pin or radius")
        except (ValueError, TypeError, OverflowError):
            return jsonify({"status": "error", "message":
                "Google Maps requires a valid latitude/longitude pin and a whole-number radius from 2 to 50 km."}), 400

    if not isinstance(target_location, str) or not target_location.strip():
        return jsonify({"status": "error", "message": "Location context argument missing."}), 400
    if (
        not isinstance(target_categories, list) or not target_categories
        or any(not isinstance(category, str) or not category.strip() for category in target_categories)
    ):
        return jsonify({"status": "error", "message": "Select at least one valid target industry."}), 400

    def execute_search(platform, industry):
        if platform == 'google':
            from discovery_engine import run_discovery
            run_discovery(target_location, industry, rating_min, max_reviews, lat, lng, radius_km)
        elif platform == 'facebook':
            from discovery_facebook import run_facebook_discovery
            run_facebook_discovery(target_location, industry)
        elif platform == 'yelp':
            from discovery_yelp import run_yelp_discovery
            run_yelp_discovery(target_location, industry, rating_min, max_reviews)
        elif platform == 'instagram':
            from discovery_instagram import run_instagram_discovery
            run_instagram_discovery(target_location, industry)

    tasks = [
        (platform, industry, lambda platform=platform, industry=industry: execute_search(platform, industry))
        for platform in dict.fromkeys(selected_sources)
        for industry in dict.fromkeys(category.strip() for category in target_categories)
    ]
    try:
        run_id = LOCAL_DISCOVERY_RUNS.start(tasks, emit_ui_log)
    except DiscoveryBusyError as error:
        return jsonify({"status": "error", "message": str(error)}), 409
    except Exception as error:
        emit_ui_log(f"[DISCOVERY ERROR] Worker could not start: {error}")
        return jsonify({"status": "error", "message": "Discovery worker could not start. Please try again."}), 500

    return jsonify({
        "status": "success", "run_id": run_id,
        "message": "Local discovery worker started. Poll its run status for the outcome."
    }), 202


@app.route('/api/discovery/runs/<run_id>', methods=['GET'])
def get_local_discovery_run(run_id):
    run = LOCAL_DISCOVERY_RUNS.snapshot(run_id)
    if run is None:
        return jsonify({"status": "error", "message":
            "Discovery status is unavailable. The app may have restarted or the run expired; check saved results before retrying."}), 404
    return jsonify({"status": "success", "run": run})

@app.route('/api/trigger/pitch-single/<int:lead_id>', methods=['POST'])
def trigger_single_pitch_generation(lead_id):
    if not OPENAI_KEY:
        return jsonify({"status": "error", "message": "OpenAI credentials missing inside environment config."}), 401

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT business_name, category, lead_type FROM prospects WHERE id = %s;",
                (lead_id,)
            )
            res = cur.fetchone()
            if not res:
                return jsonify({"status": "error", "message": "Target lead record not found."}), 404

            business_name = res['business_name']
            category = res['category']
            lead_type = res.get('lead_type') or 'web_design'

            print(f"[AI WORKER] Generating pitch for '{business_name}' | type: {lead_type} | category: {category}")

            from niche_prompts import (
                get_niche_parameters, get_service_parameters, OUTSOURCING_SERVICE_TYPES
            )

            ai_headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}

            # --- Route to appropriate prompt strategy based on lead type ---
            if lead_type in OUTSOURCING_SERVICE_TYPES:
                strategy = get_service_parameters(lead_type)
                system_prompt = (
                    "You are a B2B outreach specialist for a Philippine-based outsourcing and digital services agency. "
                    "Write a compelling cold outreach email to a company decision-maker. "
                    "Be direct, professional, and concise. Zero corporate fluff or buzzword stuffing. "
                    "CRITICAL: Do NOT write a subject line. Start directly with the greeting. "
                    f"Service we're pitching: {lead_type.replace('_', ' ')}. "
                    f"Core pain points to address: {strategy['pain_points']}. "
                    f"Use this terminology naturally: {strategy['buzzwords']}."
                )
                user_prompt = (
                    f"Write a cold outreach email to '{business_name}', a company actively hiring for '{category}' roles.\n\n"
                    f"Use this hook angle: {strategy['hook_angle']}\n\n"
                    f"End with this CTA: {strategy['cta']}\n\n"
                    "Keep it under 160 words. Do NOT write a subject line under any circumstances."
                )
                suggested_angle_summary = f"[{lead_type.replace('_', ' ').title()}] {strategy['hook_angle'][:80]}..."
            else:
                strategy = get_niche_parameters(category)
                system_prompt = (
                    "You are an elite B2B outreach copywriting specialist for an international web design agency. "
                    "Write a highly compelling cold outreach email from the agency to a business owner. "
                    "Keep the tone direct, professional, and entirely free of corporate fluff. "
                    "CRITICAL: Do NOT write a subject line. Start drafting directly from the greeting. "
                    f"Tailor the pitch around these industry pain points: {strategy['pain_points']}. "
                    f"Weave in this terminology naturally: {strategy['buzzwords']}."
                )
                user_prompt = (
                    f"Write a personalized outreach email for '{business_name}', a business in the '{category}' space.\n\n"
                    f"Core Strategy Hook: {strategy['hook_angle']}\n\n"
                    "Requirements:\n"
                    "1. Acknowledge their strong reputation and review history.\n"
                    "2. Address the digital visibility gap they're losing market share to.\n"
                    "3. Conclude with a low-friction CTA for a quick 5-minute sync or free mockup.\n"
                    "4. Do NOT output a subject line under any circumstances."
                )
                suggested_angle_summary = f"[Web Design] {strategy['hook_angle'][:80]}..."

            # --- Generate pitch body ---
            pitch_payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.65,
                "max_tokens": 420
            }

            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json=pitch_payload, headers=ai_headers, timeout=15
            )
            if response.status_code != 200:
                return jsonify({"status": "error", "message": f"OpenAI rejection: {response.text}"}), response.status_code

            generated_copy = response.json()["choices"][0]["message"]["content"].strip()

            if "subject:" in generated_copy.lower():
                lines = generated_copy.split('\n')
                generated_copy = '\n'.join(
                    l for l in lines if not l.lower().startswith('subject:')
                ).strip()

            # --- Generate subject line ---
            subj_payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": (
                        "Generate ONE compelling cold email subject line. "
                        "Rules: Under 8 words. No clickbait. No ALL CAPS. No generic words like 'opportunity' or 'partnership'. "
                        "Sound peer-to-peer, not like a mass sales blast. Output only the subject line, no quotes, no prefix."
                    )},
                    {"role": "user", "content": (
                        f"Business: {business_name}\n"
                        f"Service: {lead_type.replace('_', ' ')}\n"
                        f"Hook: {strategy.get('hook_angle', '')[:100]}"
                    )}
                ],
                "temperature": 0.85,
                "max_tokens": 25
            }
            subj_resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json=subj_payload, headers=ai_headers, timeout=10
            )
            subject_line = (
                subj_resp.json()["choices"][0]["message"]["content"].strip().strip('"')
                if subj_resp.status_code == 200
                else f"Quick concept for {business_name}"
            )

            cur.execute(
                """UPDATE prospects
                   SET ai_pitch_draft = %s, subject_line = %s,
                       suggested_angle = %s, status = 'drafted'
                   WHERE id = %s;""",
                (generated_copy, subject_line, suggested_angle_summary, lead_id)
            )
            conn.commit()

            return jsonify({
                "status": "success",
                "message": f"Pitch and subject line drafted for {business_name}.",
                "subject_line": subject_line
            })


@app.route('/api/queue', methods=['GET'])
def get_review_queue():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    search_query = request.args.get('q', '').strip()
    source_filter = request.args.get('source', '').strip()
    lead_type_filter = request.args.get('lead_type', '').strip()
    status_filter = request.args.get('status_filter', '').strip()
    sort_by = request.args.get('sort_by', 'lead_score').strip()
    offset = (page - 1) * per_page

    # Whitelist sort options — never interpolate user input directly into SQL
    SORT_MAP = {
        'lead_score': 'COALESCE(lead_score, 0) DESC, id DESC',
        'date_desc':  'id DESC',
        'date_asc':   'id ASC',
        'rating':     'rating DESC NULLS LAST, id DESC',
        'reviews':    'review_count DESC NULLS LAST, id DESC',
    }
    order_clause = SORT_MAP.get(sort_by, SORT_MAP['lead_score'])

    conditions = []
    query_params = []

    # Status — show all except rejected by default; show rejected only when explicitly selected
    if status_filter:
        conditions.append("status = %s")
        query_params.append(status_filter)
    else:
        conditions.append("status != 'rejected'")

    # Full-text search across name, category, and email
    if search_query:
        conditions.append("(business_name ILIKE %s OR category ILIKE %s OR COALESCE(email, '') ILIKE %s)")
        query_params.extend([f"%{search_query}%"] * 3)

    # Source platform filter
    if source_filter:
        conditions.append("COALESCE(source_platform, 'google_maps') = %s")
        query_params.append(source_filter)

    # Lead type filter
    if lead_type_filter:
        conditions.append("COALESCE(lead_type, 'web_design') = %s")
        query_params.append(lead_type_filter)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    metrics_query = f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status IN ('discovered', 'drafted')) as pending,
            COUNT(*) FILTER (WHERE status IN ('sent', 'sent_followup_1')) as sent,
            COUNT(*) FILTER (WHERE status IN ('followup_1', 'followup_2')) as followup
        FROM prospects
        {where_clause};
    """

    data_query = f"""
        SELECT id, business_name, category,
               COALESCE(lead_type, 'web_design') as lead_type,
               COALESCE(source_platform, 'google_maps') as source_platform,
               rating, review_count,
               COALESCE(lead_score, 0) as lead_score,
               site_quality_score,
               suggested_angle, subject_line, ai_pitch_draft,
               phone, email, formatted_address,
               website_url, profile_url, google_place_id, screenshot_path,
               followup_due_at, status,
               job_via, job_salary, job_schedule, job_description, job_apply_link
        FROM prospects
        {where_clause}
        ORDER BY {order_clause}
        LIMIT %s OFFSET %s;
    """

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(metrics_query, tuple(query_params))
            metrics = cur.fetchone()

            extended_params = query_params + [per_page, offset]
            cur.execute(data_query, tuple(extended_params))
            records = cur.fetchall()

            return jsonify({
                "status": "success",
                "current_page": page,
                "per_page": per_page,
                "leads": records,
                "metrics": {
                    "global_total": metrics['total'] if metrics else 0,
                    "global_pending": metrics['pending'] if metrics else 0,
                    "global_sent": metrics['sent'] if metrics else 0,
                    "global_followup": metrics['followup'] if metrics else 0
                }
            })

@app.route('/api/dispatch/<int:lead_id>', methods=['POST'])
def dispatch_lead_pitch(lead_id):
    data = request.get_json() or {}
    final_pitch_text = data.get('final_pitch')
    test_destination_email = data.get('destination_email')
    
    if not final_pitch_text:
        return jsonify({"status": "error", "message": "Payload generation body required."}), 400

    # Safety structural baseline check: strip out placeholder strings if user forgot to alter them
    final_pitch_text = final_pitch_text.replace("[Your Name]", "Mark Harvey")
    final_pitch_text = final_pitch_text.replace("[Your Agency Name]", "AsuraTECH Solutions")
    final_pitch_text = final_pitch_text.replace("[Your Contact Information]", "harvey@asuratechsolutions.com")

    final_recipient = test_destination_email if test_destination_email else SMTP_USER

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT business_name, status, subject_line FROM prospects WHERE id = %s;",
                (lead_id,)
            )
            res = cur.fetchone()
            if not res:
                return jsonify({"status": "error", "message": "Target lead entry absent."}), 404

            business_name = res['business_name']
            current_status = res['status']
            stored_subject = res.get('subject_line') or ''

            if current_status == 'sent':
                from followup_scheduler import schedule_followup
                schedule_followup(lead_id, 'sent')
                return jsonify({"status": "success", "message": "Follow-up scheduled. Use the follow-up sweep to stage the draft."})

            # Production Flag: set to False to prefix subject with [TEST RUN]
            IS_PRODUCTION_ENVIRONMENT = True

            prefix_label = "" if IS_PRODUCTION_ENVIRONMENT else "[TEST RUN] "

            if current_status == 'followup_1':
                fallback_subject = f"Quick follow-up for {business_name}"
            else:
                fallback_subject = f"Digital strategy concept for {business_name}"

            raw_subject = stored_subject if stored_subject else fallback_subject
            subject_header = f"{prefix_label}{raw_subject}"
            next_pipeline_status = 'sent' if current_status not in ('followup_1', 'followup_2') else 'sent_followup_1'

            # --- DYNAMIC SIGNATURE HIGH FIDELITY LAYOUT INTEGRATION ---
            signature_html = """
            <br><br>
            <div>
              <table cellpadding="0" cellspacing="0" border="0" style="font-family: 'Segoe UI', Arial, sans-serif; color: #333333; line-height: 1.6; font-size: 14px;">
                <tbody>
                  <tr>
                    <td style="padding-right: 25px; text-align: center; vertical-align: top;">
                      <img src="https://lh3.googleusercontent.com/d/1drlL-FYwOEXGSpMl41xnbIgg06QV74Bv" alt="Mark Harvey" width="96" style="border-radius: 50%; display: block; margin: 0 auto 12px auto; border: 2px solid #0056b3;">
                      <img src="https://lh3.googleusercontent.com/d/1amHhkxXJCqP5xywg_66B1HhYArPOrJE7" alt="AsuraTECH Solutions Logo" width="96" style="display: block; margin: 0 auto;">
                      <br>
                    </td>
                    <td style="border-left: 2px solid #0056b3; padding-left: 25px; vertical-align: top;">
                      <h3 style="margin: 0 0 2px 0; font-size: 20px; color: #111111; font-weight: 700;">Mark Harvey</h3>
                      <p style="margin: 0; font-size: 15px; color: #0056b3; font-weight: 600;">Solutions Architect</p>
                      <p style="margin: 2px 0 12px 0; font-size: 13px; color: #666666; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;">AsuraTECH Solutions</p>
                      <table cellpadding="0" cellspacing="0" border="0" style="font-size: 13px; color: #555555; line-height: 1.8;">
                        <tbody>
                          <tr>
                            <td style="padding-right: 12px; vertical-align: middle;"><img src="https://img.icons8.com/ios-filled/16/0056b3/phone.png" style="display:block; border:0;" width="14" height="14"></td>
                            <td style="vertical-align: middle;">0995 982 2419</td>
                          </tr>
                          <tr>
                            <td style="padding-right: 12px; vertical-align: middle;"><img src="https://img.icons8.com/ios-filled/16/0056b3/new-post.png" style="display:block; border:0;" width="14" height="14"></td>
                            <td style="vertical-align: middle;">
                              <a href="mailto:harvey@asuratechsolutions.com" style="text-decoration: none; color: #333333; font-weight: 500;">harvey@asuratechsolutions.com</a>
                            </td>
                          </tr>
                          <tr>
                            <td style="padding-right: 12px; vertical-align: middle;"><img src="https://img.icons8.com/ios-filled/16/0056b3/globe.png" style="display:block; border:0;" width="14" height="14"></td>
                            <td style="vertical-align: middle;">
                              <a href="https://asuratechsolutions.com" style="text-decoration: none; color: #333333; font-weight: 500;">asuratechsolutions.com</a>
                            </td>
                          </tr>
                          <tr>
                            <td colspan="2" style="padding-top: 14px;">
                              <a href="https://facebook.com" style="text-decoration: none; margin-right: 12px; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/facebook-new.png" alt="Facebook" width="18" height="18" style="display: block; border: none;">
                              </a>
                              <a href="https://linkedin.com" style="text-decoration: none; margin-right: 12px; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/linkedin.png" alt="LinkedIn" width="18" height="18" style="display: block; border: none;">
                              </a>
                              <a href="https://x.com" style="text-decoration: none; margin-right: 12px; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/x.png" alt="X" width="18" height="18" style="display: block; border: none;">
                              </a>
                              <a href="https://instagram.com" style="text-decoration: none; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/instagram-new.png" alt="Instagram" width="18" height="18" style="display: block; border: none;">
                              </a>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            """

            # Remove absolute duplicate subject string fragments from body content dynamically if any slipped through text editor
            if "subject:" in final_pitch_text.lower():
                lines = final_pitch_text.split('\n')
                lines = [line for line in lines if not line.lower().startswith('subject:')]
                final_pitch_text = '\n'.join(lines).strip()

            html_formatted_body = final_pitch_text.replace("\n", "<br>")
            
            full_html_payload = f"""
            <html>
              <body style="font-family: Calibri, Arial, sans-serif; font-size: 15px; color: #222222; line-height: 1.5;">
                {html_formatted_body}
                {signature_html}
              </body>
            </html>
            """

            msg = MIMEText(full_html_payload, 'html', 'utf-8')
            msg['Subject'] = Header(subject_header, 'utf-8')
            msg['From'] = f'"AsuraTECH Solutions" <{SMTP_USER}>'
            msg['To'] = final_recipient

            clean_pass = SMTP_PASS.replace(" ", "").replace("-", "").strip()
            
            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=12) as server:
                    server.login(SMTP_USER, clean_pass)
                    server.sendmail(SMTP_USER, [final_recipient], msg.as_string())
            else:
                with smtplib.SMTP(SMTP_HOST, 587, timeout=12) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(SMTP_USER, clean_pass)
                    server.sendmail(SMTP_USER, [final_recipient], msg.as_string())
            
            cur.execute(
                "UPDATE prospects SET ai_pitch_draft = %s, email = %s, status = %s WHERE id = %s;",
                (final_pitch_text, final_recipient, next_pipeline_status, lead_id)
            )
            conn.commit()

            # Auto-schedule the follow-up timer after a successful send
            from followup_scheduler import schedule_followup
            schedule_followup(lead_id, next_pipeline_status)

            return jsonify({"status": "success", "message": f"Email sent to {final_recipient}. Follow-up auto-scheduled."})

@app.route('/api/reject/<int:lead_id>', methods=['POST'])
def reject_lead(lead_id):
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE prospects SET status = 'rejected' WHERE id = %s;", (lead_id,))
            conn.commit()
            return jsonify({"status": "success", "message": "Lead archived out of active queue framework."})

@app.route('/api/prospect/update-email/<int:lead_id>', methods=['POST'])
def update_prospect_email(lead_id):
    data = request.get_json() or {}
    new_email = data.get('email', '').strip()

    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE prospects SET email = %s WHERE id = %s;",
                (new_email if new_email else None, lead_id)
            )
            conn.commit()
            print(f"[DATABASE RECORD LINK] Updated contact target email address for Lead ID: #{lead_id} -> {new_email}")
            return jsonify({"status": "success", "message": "Lead profile updated successfully."})

@app.route('/favicon.ico')
def silence_favicon():
    return '', 204

@app.route('/.well-known/appspecific/<path:filename>')
def silence_chrome_devtools(filename):
    return '', 204


# ---------------------------------------------------------------------------
# NEW ROUTES — International Lead Gen Expansion
# ---------------------------------------------------------------------------

@app.route('/api/hiring-services', methods=['GET', 'POST'])
def hiring_service_catalog():
    from hiring_services import DuplicateHiringService, create_hiring_service, list_hiring_services
    try:
        if request.method == "POST":
            service = create_hiring_service(DB_DSN, request.get_json(silent=True))
            return jsonify({"status": "success", "service": service.public_dict()}), 201
        return jsonify({"status": "success", "services": [
            service.public_dict() for service in list_hiring_services(DB_DSN)
        ]})
    except DuplicateHiringService as error:
        return jsonify({"status": "error", "message": str(error)}), 409
    except ValueError as error:
        emit_ui_log(f"[HIRING SERVICES ERROR] {error}")
        return jsonify({"status": "error", "message": str(error)}), 400 if request.method == "POST" else 500
    except Exception as error:
        emit_ui_log(f"[HIRING SERVICES ERROR] {request.method} failed: {error}")
        return jsonify({"status": "error", "message":
            "Hiring services could not be saved or loaded. Restart the app after migration, then reload the list before retrying."}), 500


@app.route('/api/trigger/discover-jobs', methods=['POST'])
def trigger_job_discovery():
    """
    Scans Google Jobs for companies actively hiring roles that signal outsourcing need.
    service_types: list of preset or persisted custom service IDs
    country: optional country filter string (e.g. 'United States', 'Australia')
    pages_per_role: optional page budget, one of 1 (default), 2, 3, or 5
    """
    from discovery_jobs import validate_hiring_pages
    from hiring_services import BUILTIN_SERVICES, list_hiring_services
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Hiring discovery expects a JSON object."}), 400
    service_types = data.get('service_types', [])
    country = data.get('country', '')
    try:
        pages_per_role = validate_hiring_pages(data.get("pages_per_role", 1))
    except ValueError as error:
        return jsonify({"status": "error", "message": str(error)}), 400

    if (
        not isinstance(service_types, list) or not service_types
        or any(not isinstance(service, str) for service in service_types)
    ):
        return jsonify({"status": "error", "message": "Select at least one supported service offering."}), 400
    if not isinstance(country, str):
        return jsonify({"status": "error", "message": "Country must be text or left blank."}), 400
    country = country.strip()
    from discovery_job_filters import resolve_hiring_country
    try:
        resolve_hiring_country(country)
    except ValueError as error:
        return jsonify({"status": "error", "message": str(error)}), 400
    service_types = list(dict.fromkeys(service_types))
    services = dict(BUILTIN_SERVICES)
    if any(service not in services for service in service_types):
        if any(service not in services and not service.startswith("custom_") for service in service_types):
            return jsonify({"status": "error", "message": "Select supported service offerings."}), 400
        try:
            services = {service.id: service for service in list_hiring_services(DB_DSN)}
        except Exception as error:
            emit_ui_log(f"[HIRING SERVICES ERROR] Could not load service definitions: {error}")
            return jsonify({"status": "error", "message": "Service definitions could not be loaded. Restart after migration and retry."}), 500
    if any(service not in services for service in service_types):
        return jsonify({"status": "error", "message": "A selected service no longer exists. Reload the service list."}), 400

    def scan(service):
        from discovery_jobs import run_job_discovery
        definition = services[service]
        if definition.builtin:
            run_job_discovery(service, country, pages_per_role=pages_per_role)
        else:
            run_job_discovery(service, country, pages_per_role=pages_per_role, service_definition=definition)

    tasks = [("google_jobs", service, lambda service=service: scan(service)) for service in service_types]
    try:
        run_id = HIRING_DISCOVERY_RUNS.start(tasks, emit_ui_log)
    except DiscoveryBusyError as error:
        return jsonify({"status": "error", "message": str(error)}), 409
    except Exception as error:
        emit_ui_log(f"[JOB DISCOVERY ERROR] Worker could not start: {error}")
        return jsonify({"status": "error", "message": "Hiring worker could not start. Please try again."}), 500
    return jsonify({
        "status": "success", "run_id": run_id,
        "pages_per_role": pages_per_role,
        "max_job_requests": sum(len(services[service].roles) for service in service_types) * pages_per_role,
        "message": f"Job board discovery started for: {', '.join(service_types)}"
    }), 202


@app.route('/api/discovery/job-runs/<run_id>', methods=['GET'])
def get_hiring_discovery_run(run_id):
    run = HIRING_DISCOVERY_RUNS.snapshot(run_id)
    if run is None:
        return jsonify({"status": "error", "message":
            "Hiring status is unavailable. The app may have restarted or the run expired; check Job Leads before retrying."}), 404
    return jsonify({"status": "success", "run": run})


@app.route('/api/job-leads', methods=['GET'])
def get_job_leads():
    """
    Returns paginated job-board leads from the dedicated job_leads table.
    Query params:
      - page, per_page         : pagination (default 1, 20)
      - q                      : full-text search (company_name, job_title, email)
      - lead_type              : filter by service type
      - status                 : filter by status (default: all except 'rejected')
      - sort_by                : 'date_desc' (default), 'date_asc', 'company'
    """
    page       = int(request.args.get('page', 1))
    per_page   = int(request.args.get('per_page', 20))
    search_q   = request.args.get('q', '').strip()
    lead_type  = request.args.get('lead_type', '').strip()
    status_f   = request.args.get('status', '').strip()
    sort_by    = request.args.get('sort_by', 'date_desc').strip()
    offset     = (page - 1) * per_page

    SORT_MAP = {
        'date_desc': 'id DESC',
        'date_asc':  'id ASC',
        'company':   'company_name ASC',
    }
    order_clause = SORT_MAP.get(sort_by, 'id DESC')

    conditions = []
    params = []

    if status_f:
        conditions.append("status = %s")
        params.append(status_f)
    else:
        conditions.append("status != 'rejected'")

    if search_q:
        conditions.append(
            "(company_name ILIKE %s OR job_title ILIKE %s OR COALESCE(email, '') ILIKE %s)"
        )
        params.extend([f"%{search_q}%"] * 3)

    if lead_type:
        conditions.append("lead_type = %s")
        params.append(lead_type)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    metrics_sql = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status IN ('discovered', 'drafted')) AS pending,
            COUNT(*) FILTER (WHERE status IN ('sent', 'sent_followup_1'))  AS sent,
            COUNT(*) FILTER (WHERE email IS NOT NULL AND email != '')       AS with_email
        FROM job_leads {where};
    """
    data_sql = f"""
        SELECT id, company_name, job_title, location, lead_type, job_role_signal,
               job_via, job_salary, job_schedule, job_description, job_apply_link,
               website_url, email, suggested_angle, ai_pitch_draft, subject_line,
               status, source_platform, created_at, updated_at, job_details, contact_evidence
        FROM job_leads
        {where}
        ORDER BY {order_clause}
        LIMIT %s OFFSET %s;
    """

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(metrics_sql, tuple(params))
            metrics = cur.fetchone()
            cur.execute(data_sql, tuple(params) + (per_page, offset))
            records = cur.fetchall()

    return jsonify({
        "status":       "success",
        "current_page": page,
        "per_page":     per_page,
        "leads":        records,
        "metrics": {
            "total":      metrics["total"]      if metrics else 0,
            "pending":    metrics["pending"]    if metrics else 0,
            "sent":       metrics["sent"]       if metrics else 0,
            "with_email": metrics["with_email"] if metrics else 0,
        }
    })


@app.route('/api/job-leads/<int:lead_id>/reject', methods=['POST'])
def reject_job_lead(lead_id):
    """Soft-reject a job lead — moves it out of the active queue."""
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE job_leads SET status = 'rejected' WHERE id = %s;", (lead_id,))
            conn.commit()
    return jsonify({"status": "success", "message": "Job lead rejected."})


@app.route('/api/job-leads/<int:lead_id>/update-email', methods=['POST'])
def update_job_lead_email(lead_id):
    """Updates the contact email for a specific job lead."""
    data      = request.get_json() or {}
    new_email = data.get('email', '').strip()
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE job_leads SET email = %s,
                    contact_evidence = COALESCE(contact_evidence, '{}'::jsonb)
                        || jsonb_build_object('email_status', 'manual', 'email_source_url', NULL,
                                              'email_updated_at', CURRENT_TIMESTAMP)
                    WHERE id = %s;""",
                (new_email or None, lead_id)
            )
            conn.commit()
    return jsonify({"status": "success", "message": "Job lead email updated."})


@app.route('/api/trigger/pitch-bulk', methods=['POST'])
def trigger_bulk_pitch_generation():
    """
    Generates AI pitches (body + subject line) for all 'discovered' leads with no draft.
    Processes highest-scored leads first. Default batch size: 10.
    """
    if not OPENAI_KEY:
        return jsonify({"status": "error", "message": "OpenAI API key missing."}), 401

    data = request.get_json() or {}
    limit = int(data.get('limit', 10))

    def bulk_pitch_worker():
        from niche_prompts import (
            get_niche_parameters, get_service_parameters, OUTSOURCING_SERVICE_TYPES
        )
        emit_ui_log(f"🤖 [BULK PITCH] Starting AI generation for up to {limit} leads...")

        try:
            with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, business_name, category, lead_type
                        FROM prospects
                        WHERE status = 'discovered'
                          AND (ai_pitch_draft IS NULL OR ai_pitch_draft = '')
                        ORDER BY lead_score DESC, id DESC
                        LIMIT %s;
                        """,
                        (limit,)
                    )
                    leads = cur.fetchall()

                    if not leads:
                        emit_ui_log("[BULK PITCH] No pending leads in discovery queue.")
                        return

                    ai_headers = {
                        "Authorization": f"Bearer {OPENAI_KEY}",
                        "Content-Type": "application/json"
                    }
                    success = 0

                    for lead in leads:
                        lead_type = lead.get('lead_type') or 'web_design'
                        category = lead.get('category', '')
                        business_name = lead.get('business_name', '')

                        if lead_type in OUTSOURCING_SERVICE_TYPES:
                            strategy = get_service_parameters(lead_type)
                            system_prompt = (
                                "You are a B2B outreach specialist for a Philippine-based outsourcing agency. "
                                "Write a compelling cold outreach email to a company decision-maker. "
                                "Direct, professional, concise. Zero fluff. "
                                "CRITICAL: No subject line. Start with the greeting. "
                                f"Service: {lead_type.replace('_', ' ')}. "
                                f"Pain points: {strategy['pain_points']}. "
                                f"Terminology: {strategy['buzzwords']}."
                            )
                            user_prompt = (
                                f"Write a cold outreach email to '{business_name}', "
                                f"actively hiring for '{category}' roles.\n\n"
                                f"Hook: {strategy['hook_angle']}\n"
                                f"CTA: {strategy['cta']}\n\n"
                                "Under 160 words. No subject line."
                            )
                            suggested_angle = f"[{lead_type.replace('_', ' ').title()}] {strategy['hook_angle'][:80]}..."
                        else:
                            strategy = get_niche_parameters(category)
                            system_prompt = (
                                "You are a B2B outreach specialist for an international web design agency. "
                                "Write a compelling cold outreach email to a business owner. "
                                "Direct, professional. Zero fluff. "
                                "CRITICAL: No subject line. Start with the greeting. "
                                f"Pain points: {strategy['pain_points']}. "
                                f"Terminology: {strategy['buzzwords']}."
                            )
                            user_prompt = (
                                f"Cold outreach email for '{business_name}' in '{category}'.\n\n"
                                f"Hook: {strategy['hook_angle']}\n\n"
                                "1. Acknowledge their reputation. "
                                "2. Address their digital gap. "
                                "3. Low-friction CTA. No subject line."
                            )
                            suggested_angle = f"[Web Design] {strategy['hook_angle'][:80]}..."

                        try:
                            pitch_payload = {
                                "model": "gpt-4o-mini",
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}
                                ],
                                "temperature": 0.65,
                                "max_tokens": 420
                            }
                            resp = requests.post(
                                "https://api.openai.com/v1/chat/completions",
                                json=pitch_payload, headers=ai_headers, timeout=15
                            )
                            if resp.status_code != 200:
                                emit_ui_log(f"⚠️ GPT error for {business_name}: {resp.status_code}")
                                continue

                            pitch_body = resp.json()['choices'][0]['message']['content'].strip()
                            if "subject:" in pitch_body.lower():
                                pitch_body = '\n'.join(
                                    l for l in pitch_body.split('\n')
                                    if not l.lower().startswith('subject:')
                                ).strip()

                            # Subject line generation
                            subj_payload = {
                                "model": "gpt-4o-mini",
                                "messages": [
                                    {"role": "system", "content": (
                                        "Generate ONE cold email subject line. Under 8 words. "
                                        "No clickbait. No ALL CAPS. Peer-to-peer tone. "
                                        "Output only the text, no quotes."
                                    )},
                                    {"role": "user", "content": (
                                        f"Business: {business_name}\n"
                                        f"Service: {lead_type.replace('_', ' ')}\n"
                                        f"Hook: {strategy.get('hook_angle', '')[:80]}"
                                    )}
                                ],
                                "temperature": 0.85,
                                "max_tokens": 25
                            }
                            subj_resp = requests.post(
                                "https://api.openai.com/v1/chat/completions",
                                json=subj_payload, headers=ai_headers, timeout=10
                            )
                            subject_line = (
                                subj_resp.json()['choices'][0]['message']['content'].strip().strip('"')
                                if subj_resp.status_code == 200
                                else f"Quick concept for {business_name}"
                            )

                            cur.execute(
                                """UPDATE prospects
                                   SET ai_pitch_draft = %s, subject_line = %s,
                                       suggested_angle = %s, status = 'drafted'
                                   WHERE id = %s;""",
                                (pitch_body, subject_line, suggested_angle, lead['id'])
                            )
                            conn.commit()
                            success += 1
                            emit_ui_log(f"✅ [BULK PITCH] Drafted: {business_name}")

                        except Exception as e:
                            emit_ui_log(f"⚠️ [BULK PITCH] Failed on {business_name}: {e}")
                            continue

                    emit_ui_log(f"🏁 [BULK PITCH] Complete — {success}/{len(leads)} leads drafted.")

        except Exception as e:
            emit_ui_log(f"❌ [BULK PITCH CRITICAL ERROR] {e}")

    thread = threading.Thread(target=bulk_pitch_worker)
    thread.daemon = True
    thread.start()

    return jsonify({
        "status": "success",
        "message": f"Bulk pitch generation started for up to {limit} leads."
    }), 202


@app.route('/api/export', methods=['GET'])
def export_leads_csv():
    """
    Exports all prospects to a downloadable CSV file.
    Optional query param: ?status=drafted  to filter by status.
    Sorted by lead_score descending.
    """
    status_filter = request.args.get('status', '').strip()

    columns = [
        'id', 'business_name', 'category', 'lead_type', 'source_platform',
        'rating', 'review_count', 'lead_score', 'site_quality_score',
        'email', 'phone', 'formatted_address', 'website_url', 'profile_url',
        'subject_line', 'ai_pitch_draft', 'suggested_angle',
        'status', 'followup_due_at', 'created_at'
    ]
    col_str = ', '.join(columns)
    query = f"SELECT {col_str} FROM prospects"
    params = []

    if status_filter:
        query += " WHERE status = %s"
        params.append(status_filter)

    query += " ORDER BY lead_score DESC, id DESC;"

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, '') for k in columns})

    output.seek(0)
    filename = f"leads_{status_filter or 'all'}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route('/api/trigger/score-leads', methods=['POST'])
def trigger_lead_scoring():
    """Runs the lead scoring engine on all unscored prospects."""
    def score_worker():
        from lead_scorer import run_lead_scoring
        run_lead_scoring()

    thread = threading.Thread(target=score_worker)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Lead scoring pass started."}), 202


@app.route('/api/trigger/check-websites', methods=['POST'])
def trigger_website_checker():
    """Runs Google PageSpeed audit on prospects that have a website but no quality score yet."""
    def checker_worker():
        from website_checker import run_website_quality_check
        run_website_quality_check()

    thread = threading.Thread(target=checker_worker)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Website quality audit started."}), 202


@app.route('/api/trigger/followup-sweep', methods=['POST'])
def trigger_followup_sweep():
    """Runs the follow-up scheduler sweep — stages next-in-sequence drafts for overdue leads."""
    def sweep_worker():
        from followup_scheduler import run_followup_sweep
        run_followup_sweep()

    thread = threading.Thread(target=sweep_worker)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Follow-up sweep started."}), 202


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)