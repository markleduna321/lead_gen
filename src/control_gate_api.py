import os
import sys
import csv
import io
import requests
import threading
import psycopg2
from datetime import timedelta
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template, Response, redirect, session, url_for
from dotenv import load_dotenv
from discovery_runs import DiscoveryBusyError, LocalDiscoveryRuns

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SYSTEM_LOG_BUFFER = []
USER_LOG_BUFFERS = {}
LOG_BUFFER_LOCK = threading.Lock()
LOG_CONTEXT = threading.local()
LOCAL_DISCOVERY_RUNS = LocalDiscoveryRuns()
HIRING_DISCOVERY_RUNS = LocalDiscoveryRuns("hiring")


def bind_log_user(user_id):
    """Attaches the owning account to the current worker thread.

    Discovery runs in a background thread with no request context, so the
    account has to be carried explicitly or its logs land in no buffer.
    """
    LOG_CONTEXT.user_id = user_id


def resolve_log_user():
    bound = getattr(LOG_CONTEXT, "user_id", None)
    if bound is not None:
        return bound
    try:
        from auth import current_user_id
        return current_user_id()
    except Exception:
        return None


def user_log_buffer(user_id):
    """Per-account log buffer. A shared buffer would leak activity across users."""
    if user_id is None:
        return SYSTEM_LOG_BUFFER
    with LOG_BUFFER_LOCK:
        return USER_LOG_BUFFERS.setdefault(user_id, [])


def emit_ui_log(message):
    """Appends an operational milestone directly to the user viewport queue."""
    print(message) # Keep standard terminal output intact
    buffer = user_log_buffer(resolve_log_user())
    buffer.append(message)
    LOCAL_DISCOVERY_RUNS.record_log(message)
    HIRING_DISCOVERY_RUNS.record_log(message)
    # Keep the live cache clean (trim past 50 entries)
    del buffer[:-50]

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
# Outgoing mail is per-account (see user_email.py); there is no shared mailbox.
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

app = Flask(__name__, template_folder='templates', static_folder='../static')

# Sessions must survive a restart, so the key is configuration rather than a
# per-boot random value. Local development falls back to a fixed development
# key; any non-local deployment must supply its own.
APP_ENV = os.getenv("APP_ENV", "local").strip().lower()
_secret = os.getenv("FLASK_SECRET_KEY", "").strip()
if not _secret:
    if APP_ENV != "local":
        raise RuntimeError(
            "FLASK_SECRET_KEY must be set when APP_ENV is not 'local'. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    _secret = "local-development-only-key-do-not-use-when-hosted"
    print("[STARTUP] ⚠️ FLASK_SECRET_KEY is unset — using the local development key.", file=sys.stderr)

app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=APP_ENV not in {"local", "desktop"},
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


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
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS last_discovered_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP;",
        # Job board enrichment columns
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_via         TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_salary      TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_schedule    TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_description TEXT;",
        "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_apply_link  TEXT;",
        "CREATE INDEX IF NOT EXISTS idx_prospects_lead_score ON prospects (lead_score DESC);",
        "CREATE INDEX IF NOT EXISTS idx_prospects_discovered ON prospects (COALESCE(last_discovered_at, created_at) DESC, id DESC);",
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
            updated_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
            last_discovered_at TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_identity TEXT;",
        "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_details JSONB;",
        "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS contact_evidence JSONB;",
        "ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS last_discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;",
        "CREATE INDEX IF NOT EXISTS idx_job_leads_discovered ON job_leads (COALESCE(last_discovered_at, created_at) DESC, id DESC);",
        "ALTER TABLE job_leads DROP CONSTRAINT IF EXISTS unique_job_lead;",
        """
        CREATE TABLE IF NOT EXISTS hiring_services (
            service_id VARCHAR(50) PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            name_key TEXT NOT NULL,
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
        # Phase 5: accounts and per-user data isolation
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            email_key TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name VARCHAR(120),
            role VARCHAR(20) NOT NULL DEFAULT 'member',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            failed_login_count INT NOT NULL DEFAULT 0,
            locked_until TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS user_email_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
            smtp_host TEXT NOT NULL,
            smtp_port INTEGER NOT NULL DEFAULT 587,
            smtp_security VARCHAR(10) NOT NULL DEFAULT 'starttls',
            smtp_username TEXT NOT NULL,
            smtp_password_encrypted BYTEA NOT NULL,
            from_name VARCHAR(120),
            from_email TEXT,
            verified_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_title   VARCHAR(120);",
        "ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_company VARCHAR(120);",
        "ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_phone   VARCHAR(60);",
        "ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_website TEXT;",
        "ALTER TABLE prospects        ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;",
        "ALTER TABLE job_leads        ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;",
        "ALTER TABLE hiring_services  ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;",
        "ALTER TABLE prospects       DROP CONSTRAINT IF EXISTS unique_google_place;",
        "ALTER TABLE hiring_services DROP CONSTRAINT IF EXISTS hiring_services_name_key_key;",
        "DROP INDEX IF EXISTS idx_job_leads_identity;",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_prospects_user_place      ON prospects (user_id, google_place_id);",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_leads_user_identity   ON job_leads (user_id, job_identity);",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hiring_services_user_name ON hiring_services (user_id, name_key);",
        "CREATE INDEX IF NOT EXISTS idx_prospects_user ON prospects (user_id);",
        "CREATE INDEX IF NOT EXISTS idx_job_leads_user ON job_leads (user_id);",
    ]

    try:
        with psycopg2.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                for sql in migrations:
                    cur.execute(sql)
            conn.commit()
    except Exception as e:
        print(f"[STARTUP] Migration warning (non-fatal): {e}", file=sys.stderr)
        return
    # Printed outside the try: a console that cannot encode this line must not
    # be reported as a migration failure.
    print("[STARTUP] Schema migrations applied successfully.")


run_startup_migrations()


# ---------------------------------------------------------------------------
# Authentication — every route is private unless explicitly public
# ---------------------------------------------------------------------------

@app.before_request
def require_signed_in_user():
    """Blocks anonymous access before any view runs.

    Guarding centrally means a newly added route is private by default; it has
    to be named in auth.PUBLIC_ENDPOINTS to be reachable without a session.
    The account is re-checked every request so deactivating or deleting a user
    takes effect immediately instead of when their cookie eventually expires.
    """
    from auth import PUBLIC_ENDPOINTS, current_user_id, sign_out_session, unauthorized_response
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if current_user_id() is None:
        return unauthorized_response()
    if signed_in_user() is None:
        # The account was deactivated or removed after this session started.
        sign_out_session()
        return unauthorized_response()
    return None


def signed_in_user():
    """The current account, or None. Always use this to scope data access.

    Cached per request so the guard, the template context, and any view share
    a single lookup.
    """
    from flask import g
    from auth import current_user_id, load_user
    user_id = current_user_id()
    if user_id is None:
        return None
    cached = getattr(g, "signed_in_account", None)
    if cached is None or cached[0] != user_id:
        g.signed_in_account = (user_id, load_user(DB_DSN, user_id))
    return g.signed_in_account[1]


@app.context_processor
def inject_current_user():
    try:
        return {"current_user": signed_in_user()}
    except Exception:
        return {"current_user": None}


def active_user_id():
    """The owning account for the current request.

    Background workers must capture this value while the request context is
    alive — Flask's session is unavailable inside the discovery threads.
    """
    from auth import current_user_id
    return current_user_id()


@app.route('/login', methods=['GET', 'POST'])
def sign_in():
    from auth import AuthError, authenticate, current_user_id, safe_redirect_target, sign_in_session
    target = safe_redirect_target(request.args.get('next'))
    if current_user_id() is not None:
        return redirect(target)
    if request.method == 'POST':
        form = request.form
        try:
            user = authenticate(DB_DSN, form.get('email'), form.get('password'))
        except AuthError as error:
            return render_template('auth.html', mode='login', error=str(error),
                                   email=form.get('email', ''), next=target), 401
        except Exception as error:
            print(f"[AUTH ERROR] Sign-in failed: {error}", file=sys.stderr)
            return render_template('auth.html', mode='login', next=target,
                                   error="Sign-in is unavailable right now. Please try again."), 500
        sign_in_session(user)
        return redirect(safe_redirect_target(form.get('next'), target))
    return render_template('auth.html', mode='login', next=target)


@app.route('/register', methods=['GET', 'POST'])
def register():
    from auth import AuthError, create_user, current_user_id, safe_redirect_target, sign_in_session
    target = safe_redirect_target(request.args.get('next'))
    if current_user_id() is not None:
        return redirect(target)
    if request.method == 'POST':
        form = request.form
        if form.get('password') != form.get('confirm_password'):
            return render_template('auth.html', mode='register', error="Both passwords must match.",
                                   email=form.get('email', ''), display_name=form.get('display_name', ''),
                                   next=target), 400
        try:
            user = create_user(DB_DSN, form.get('email'), form.get('password'), form.get('display_name'))
        except AuthError as error:
            return render_template('auth.html', mode='register', error=str(error),
                                   email=form.get('email', ''), display_name=form.get('display_name', ''),
                                   next=target), 400
        except Exception as error:
            print(f"[AUTH ERROR] Registration failed: {error}", file=sys.stderr)
            return render_template('auth.html', mode='register', next=target,
                                   error="Accounts cannot be created right now. Please try again."), 500
        sign_in_session(user)
        return redirect(safe_redirect_target(form.get('next'), target))
    return render_template('auth.html', mode='register', next=target)


@app.route('/logout', methods=['POST'])
def sign_out():
    from auth import sign_out_session
    sign_out_session()
    return redirect(url_for('sign_in'))


@app.route('/api/session', methods=['GET'])
def describe_session():
    user = signed_in_user()
    return jsonify({"status": "success", "user": user})


@app.route('/api/email-settings', methods=['GET', 'POST', 'DELETE'])
def manage_email_settings():
    """Each account sends outreach from its own mailbox.

    The stored password is write-only: it is never returned to the browser,
    only a 'configured' flag and the non-secret fields.
    """
    from user_email import EmailSettingsError, delete_settings, public_settings, save_settings
    user_id = active_user_id()
    try:
        if request.method == 'POST':
            settings = save_settings(DB_DSN, user_id, request.get_json(silent=True))
            return jsonify({"status": "success", "settings": settings,
                            "message": "Mail settings saved. Send a test message to confirm them."})
        if request.method == 'DELETE':
            delete_settings(DB_DSN, user_id)
            return jsonify({"status": "success", "settings": {"configured": False},
                            "message": "Mail settings removed."})
        return jsonify({"status": "success", "settings": public_settings(DB_DSN, user_id)})
    except EmailSettingsError as error:
        return jsonify({"status": "error", "message": str(error)}), 400
    except Exception as error:
        print(f"[EMAIL SETTINGS ERROR] {request.method} failed: {error}", file=sys.stderr)
        return jsonify({"status": "error",
                        "message": "Mail settings could not be loaded or saved. Please try again."}), 500


@app.route('/api/email-settings/test', methods=['POST'])
def send_email_settings_test():
    from user_email import EmailNotConfigured, EmailSettingsError, public_settings, send_test_email
    user_id = active_user_id()
    try:
        result = send_test_email(DB_DSN, user_id)
    except EmailNotConfigured as error:
        return jsonify({"status": "error", "message": str(error)}), 409
    except EmailSettingsError as error:
        return jsonify({"status": "error", "message": str(error)}), 400
    except Exception as error:
        print(f"[EMAIL TEST ERROR] {error}", file=sys.stderr)
        return jsonify({"status": "error", "message": "The test message could not be sent."}), 500
    return jsonify({"status": "success", "settings": public_settings(DB_DSN, user_id),
                    "message": f"Test message sent to {result['recipient']}."})


@app.route('/api/discovery/logs', methods=['GET'])
def fetch_live_ui_logs():
    """Exposes current status logs to the frontend UI dashboard."""
    return jsonify({
        "status": "success",
        "logs": user_log_buffer(active_user_id()),
    })

@app.route('/api/discovery/logs/clear', methods=['POST'])
def clear_ui_logs():
    """Wipes the pipeline logs clean before initiating a new discovery pass."""
    user_log_buffer(active_user_id()).clear()
    return jsonify({"status": "success"})

@app.errorhandler(Exception)
def handle_global_exception(e):
    print(f"[SERVER CRASH ERROR] {str(e)}", file=sys.stderr)
    return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    from auth import current_user_id, sign_out_session
    if signed_in_user() is not None:
        return serve_dashboard('overview')
    if current_user_id() is not None:
        sign_out_session()
    return render_template('home.html')


@app.route('/plans', methods=['GET'])
def plans():
    return render_template('plans.html')


@app.route('/overview', defaults={'page': 'overview'}, methods=['GET'])
@app.route('/prospects', defaults={'page': 'prospects'}, methods=['GET'])
@app.route('/job-leads', defaults={'page': 'jobs'}, methods=['GET'])
@app.route('/discovery', defaults={'page': 'discovery'}, methods=['GET'])
@app.route('/settings', defaults={'page': 'settings'}, methods=['GET'])
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
        'settings': ('Settings', 'YOUR SENDING IDENTITY',
                     'Connect your own mailbox so outreach is sent from you, and set your signature.'),
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

    owner_id = active_user_id()

    def execute_search(platform, industry):
        # Returning the saved count lets the run report how much it actually found.
        if platform == 'google':
            from discovery_engine import run_discovery
            return run_discovery(target_location, industry, rating_min, max_reviews, lat, lng, radius_km, user_id=owner_id)
        if platform == 'facebook':
            from discovery_facebook import run_facebook_discovery
            return run_facebook_discovery(target_location, industry, user_id=owner_id)
        if platform == 'yelp':
            from discovery_yelp import run_yelp_discovery
            return run_yelp_discovery(target_location, industry, rating_min, max_reviews, user_id=owner_id)
        if platform == 'instagram':
            from discovery_instagram import run_instagram_discovery
            return run_instagram_discovery(target_location, industry, user_id=owner_id)
        return None

    tasks = [
        (platform, industry, lambda platform=platform, industry=industry: execute_search(platform, industry))
        for platform in dict.fromkeys(selected_sources)
        for industry in dict.fromkeys(category.strip() for category in target_categories)
    ]
    try:
        run_id = LOCAL_DISCOVERY_RUNS.start(
            tasks, emit_ui_log, owner=owner_id,
            on_thread_start=lambda: bind_log_user(owner_id),
        )
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
    run = LOCAL_DISCOVERY_RUNS.snapshot(run_id, owner=active_user_id())
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
                "SELECT business_name, category, lead_type FROM prospects WHERE id = %s AND user_id = %s;",
                (lead_id, active_user_id())
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
                   WHERE id = %s AND user_id = %s;""",
                (generated_copy, subject_line, suggested_angle_summary, lead_id, active_user_id())
            )
            conn.commit()

            return jsonify({
                "status": "success",
                "message": f"Pitch and subject line drafted for {business_name}.",
                "subject_line": subject_line
            })


# The source shown on a prospect row. Mirrors the dashboard badge, which falls
# back to the place-id prefix for rows saved before source_platform existed, so
# filtering by a source always matches the badge the user sees.
PROSPECT_SOURCE_SQL = """(CASE
    WHEN LOWER(source_platform) = 'job_board' THEN 'job_board'
    WHEN LOWER(source_platform) = 'facebook' OR LEFT(COALESCE(google_place_id, ''), 3) = 'fb_' THEN 'facebook'
    WHEN LOWER(source_platform) = 'yelp' OR LEFT(COALESCE(google_place_id, ''), 5) = 'yelp_' THEN 'yelp'
    WHEN LOWER(source_platform) = 'instagram' OR LEFT(COALESCE(google_place_id, ''), 3) = 'ig_' THEN 'instagram'
    WHEN LOWER(source_platform) = 'linkedin' OR LEFT(COALESCE(google_place_id, ''), 3) = 'ln_'
         OR LEFT(COALESCE(google_place_id, ''), 4) = 'b2b_' THEN 'linkedin'
    ELSE 'google_maps' END)"""
# Industries and job boards are free text from providers ("Law firm" vs
# "Law Firm ", "via LinkedIn" vs "LinkedIn"), so they are matched on a
# normalised key rather than the raw string.
PROSPECT_CATEGORY_KEY_SQL = "LOWER(BTRIM(COALESCE(category, '')))"
JOB_VIA_LABEL_SQL = r"BTRIM(REGEXP_REPLACE(COALESCE(job_via, ''), '^\s*via\s+', '', 'i'))"
JOB_VIA_KEY_SQL = f"LOWER({JOB_VIA_LABEL_SQL})"


def read_filter_options(cur, facet_sql, params):
    """Runs a UNION of (facet, value, label, count) rows into {facet: [option]}."""
    cur.execute(facet_sql, tuple(params))
    options = {}
    for row in cur.fetchall() or []:
        if not isinstance(row, dict) or not row.get('facet') or not row.get('value'):
            continue
        options.setdefault(row['facet'], []).append({
            'value': row['value'],
            'label': row.get('label') or row['value'],
            'count': int(row.get('count') or 0),
        })
    return options


@app.route('/api/queue', methods=['GET'])
def get_review_queue():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    search_query = request.args.get('q', '').strip()
    source_filter = request.args.get('source', '').strip().lower()
    category_filter = request.args.get('category', '').strip()
    lead_type_filter = request.args.get('lead_type', '').strip()
    status_filter = request.args.get('status_filter', '').strip()
    sort_by = request.args.get('sort_by', 'date_desc').strip()
    offset = (page - 1) * per_page

    # Whitelist sort options — never interpolate user input directly into SQL
    SORT_MAP = {
        'lead_score': 'COALESCE(lead_score, 0) DESC, COALESCE(last_discovered_at, created_at) DESC, id DESC',
        'date_desc':  'COALESCE(last_discovered_at, created_at) DESC, id DESC',
        'date_asc':   'COALESCE(last_discovered_at, created_at) ASC, id ASC',
        'rating':     'rating DESC NULLS LAST, id DESC',
        'reviews':    'review_count DESC NULLS LAST, id DESC',
    }
    order_clause = SORT_MAP.get(sort_by, SORT_MAP['date_desc'])

    # Ownership is the first condition so every queue read is scoped to the
    # signed-in account, including the metrics totals.
    conditions = ["user_id = %s"]
    query_params = [active_user_id()]
    if status_filter:
        conditions.append("status = %s")
        query_params.append(status_filter)
    else:
        conditions.append("status != 'rejected'")
    # Facet counts follow the status scope so they agree with the rows listed.
    scope_where = " AND ".join(conditions)
    scope_params = list(query_params)

    # Search every text field the prospect table and inspector show.
    if search_query:
        conditions.append(
            "(business_name ILIKE %s OR COALESCE(category, '') ILIKE %s OR COALESCE(email, '') ILIKE %s"
            " OR COALESCE(formatted_address, '') ILIKE %s OR COALESCE(phone, '') ILIKE %s)"
        )
        query_params.extend([f"%{search_query}%"] * 5)

    if source_filter:
        conditions.append(f"{PROSPECT_SOURCE_SQL} = %s")
        query_params.append(source_filter)

    if category_filter:
        conditions.append(f"{PROSPECT_CATEGORY_KEY_SQL} = LOWER(%s)")
        query_params.append(category_filter)

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
               {PROSPECT_SOURCE_SQL} as source_platform,
               rating, review_count,
               COALESCE(lead_score, 0) as lead_score,
               site_quality_score,
               suggested_angle, subject_line, ai_pitch_draft,
               phone, email, formatted_address,
               website_url, profile_url, google_place_id, screenshot_path,
               followup_due_at, status, last_discovered_at,
               job_via, job_salary, job_schedule, job_description, job_apply_link
        FROM prospects
        {where_clause}
        ORDER BY {order_clause}
        LIMIT %s OFFSET %s;
    """

    facet_query = f"""
        SELECT 'source' AS facet, {PROSPECT_SOURCE_SQL} AS value, {PROSPECT_SOURCE_SQL} AS label, COUNT(*) AS count
        FROM prospects WHERE {scope_where} GROUP BY 2
        UNION ALL
        SELECT 'category', {PROSPECT_CATEGORY_KEY_SQL}, MIN(BTRIM(category)), COUNT(*)
        FROM prospects WHERE {scope_where} AND {PROSPECT_CATEGORY_KEY_SQL} <> ''
        GROUP BY {PROSPECT_CATEGORY_KEY_SQL}
        UNION ALL
        SELECT 'status', status, status, COUNT(*)
        FROM prospects WHERE user_id = %s AND COALESCE(status, '') <> '' GROUP BY status
        ORDER BY 1, 4 DESC, 3;
    """

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(metrics_query, tuple(query_params))
            metrics = cur.fetchone()

            extended_params = query_params + [per_page, offset]
            cur.execute(data_query, tuple(extended_params))
            records = cur.fetchall()

            filter_options = read_filter_options(
                cur, facet_query, scope_params + scope_params + [active_user_id()])

            return jsonify({
                "status": "success",
                "current_page": page,
                "per_page": per_page,
                "leads": records,
                "filter_options": filter_options,
                "metrics": {
                    "global_total": metrics['total'] if metrics else 0,
                    "global_pending": metrics['pending'] if metrics else 0,
                    "global_sent": metrics['sent'] if metrics else 0,
                    "global_followup": metrics['followup'] if metrics else 0
                }
            })

@app.route('/api/dispatch/<int:lead_id>', methods=['POST'])
def dispatch_lead_pitch(lead_id):
    from user_email import EmailNotConfigured, EmailSettingsError, sender_identity
    data = request.get_json() or {}
    final_pitch_text = data.get('final_pitch')
    test_destination_email = data.get('destination_email')
    
    if not final_pitch_text:
        return jsonify({"status": "error", "message": "Payload generation body required."}), 400

    owner_id = active_user_id()
    try:
        sender = sender_identity(DB_DSN, owner_id)
    except EmailNotConfigured as error:
        return jsonify({"status": "error", "code": "email_not_configured",
                        "message": str(error)}), 409
    except EmailSettingsError as error:
        return jsonify({"status": "error", "message": str(error)}), 400

    # Placeholders resolve to the signed-in sender, not a hardcoded identity.
    final_pitch_text = final_pitch_text.replace("[Your Name]", sender["name"])
    final_pitch_text = final_pitch_text.replace("[Your Agency Name]", sender["name"])
    final_pitch_text = final_pitch_text.replace("[Your Contact Information]", sender["email"])

    final_recipient = test_destination_email if test_destination_email else sender["email"]

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT business_name, status, subject_line FROM prospects WHERE id = %s AND user_id = %s;",
                (lead_id, active_user_id())
            )
            res = cur.fetchone()
            if not res:
                return jsonify({"status": "error", "message": "Target lead entry absent."}), 404

            business_name = res['business_name']
            current_status = res['status']
            stored_subject = res.get('subject_line') or ''

            if current_status == 'sent':
                from followup_scheduler import schedule_followup
                schedule_followup(lead_id, 'sent', active_user_id())
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

            # The signature is rendered from the sending account's own settings;
            # a shared one would sign every user's outreach as the same person.
            from user_email import build_signature
            signature_html = build_signature(DB_DSN, owner_id)

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

            from user_email import send_email
            try:
                send_email(DB_DSN, owner_id, final_recipient, subject_header, full_html_payload)
            except EmailNotConfigured as error:
                return jsonify({"status": "error", "code": "email_not_configured",
                                "message": str(error)}), 409
            except EmailSettingsError as error:
                return jsonify({"status": "error", "message": str(error)}), 400
            
            cur.execute(
                "UPDATE prospects SET ai_pitch_draft = %s, email = %s, status = %s WHERE id = %s AND user_id = %s;",
                (final_pitch_text, final_recipient, next_pipeline_status, lead_id, active_user_id())
            )
            conn.commit()

            # Auto-schedule the follow-up timer after a successful send
            from followup_scheduler import schedule_followup
            schedule_followup(lead_id, next_pipeline_status, active_user_id())

            return jsonify({"status": "success", "message": f"Email sent to {final_recipient}. Follow-up auto-scheduled."})

@app.route('/api/reject/<int:lead_id>', methods=['POST'])
def reject_lead(lead_id):
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE prospects SET status = 'rejected' WHERE id = %s AND user_id = %s;", (lead_id, active_user_id()))
            if cur.rowcount == 0:
                return jsonify({"status": "error", "message": "That lead is not in your workspace."}), 404
            conn.commit()
            return jsonify({"status": "success", "message": "Lead archived out of active queue framework."})

@app.route('/api/prospect/update-email/<int:lead_id>', methods=['POST'])
def update_prospect_email(lead_id):
    data = request.get_json() or {}
    new_email = data.get('email', '').strip()

    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE prospects SET email = %s WHERE id = %s AND user_id = %s;",
                (new_email if new_email else None, lead_id, active_user_id())
            )
            if cur.rowcount == 0:
                return jsonify({"status": "error", "message": "That lead is not in your workspace."}), 404
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
            service = create_hiring_service(DB_DSN, request.get_json(silent=True), active_user_id())
            return jsonify({"status": "success", "service": service.public_dict()}), 201
        return jsonify({"status": "success", "services": [
            service.public_dict() for service in list_hiring_services(DB_DSN, active_user_id())
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
            services = {service.id: service for service in list_hiring_services(DB_DSN, active_user_id())}
        except Exception as error:
            emit_ui_log(f"[HIRING SERVICES ERROR] Could not load service definitions: {error}")
            return jsonify({"status": "error", "message": "Service definitions could not be loaded. Restart after migration and retry."}), 500
    if any(service not in services for service in service_types):
        return jsonify({"status": "error", "message": "A selected service no longer exists. Reload the service list."}), 400

    owner_id = active_user_id()

    def scan(service):
        from discovery_jobs import run_job_discovery
        definition = services[service]
        if definition.builtin:
            return run_job_discovery(service, country, pages_per_role=pages_per_role, user_id=owner_id)
        return run_job_discovery(service, country, pages_per_role=pages_per_role,
                                 service_definition=definition, user_id=owner_id)

    tasks = [("google_jobs", service, lambda service=service: scan(service)) for service in service_types]
    try:
        run_id = HIRING_DISCOVERY_RUNS.start(
            tasks, emit_ui_log, owner=owner_id,
            on_thread_start=lambda: bind_log_user(owner_id),
        )
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
    run = HIRING_DISCOVERY_RUNS.snapshot(run_id, owner=active_user_id())
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
      - q                      : search company, job title, email, location, job board
      - lead_type              : filter by service type
      - via                    : filter by job board ("via LinkedIn" == "LinkedIn")
      - status                 : filter by status (default: all except 'rejected')
      - sort_by                : 'date_desc' (default), 'date_asc', 'company'
    """
    page       = int(request.args.get('page', 1))
    per_page   = int(request.args.get('per_page', 20))
    search_q   = request.args.get('q', '').strip()
    lead_type  = request.args.get('lead_type', '').strip()
    via_f      = request.args.get('via', '').strip()
    status_f   = request.args.get('status', '').strip()
    sort_by    = request.args.get('sort_by', 'date_desc').strip()
    offset     = (page - 1) * per_page

    SORT_MAP = {
        'date_desc': 'COALESCE(last_discovered_at, created_at) DESC, id DESC',
        'date_asc':  'COALESCE(last_discovered_at, created_at) ASC, id ASC',
        'company':   'LOWER(company_name) ASC, id DESC',
    }
    order_clause = SORT_MAP.get(sort_by, SORT_MAP['date_desc'])

    # Ownership first so the metrics totals are scoped too, not just the rows.
    conditions = ["user_id = %s"]
    params = [active_user_id()]

    if status_f:
        conditions.append("status = %s")
        params.append(status_f)
    else:
        conditions.append("status != 'rejected'")
    scope_where = " AND ".join(conditions)
    scope_params = list(params)

    if search_q:
        conditions.append(
            "(company_name ILIKE %s OR job_title ILIKE %s OR COALESCE(email, '') ILIKE %s"
            " OR COALESCE(location, '') ILIKE %s OR COALESCE(job_via, '') ILIKE %s)"
        )
        params.extend([f"%{search_q}%"] * 5)

    if lead_type:
        conditions.append("lead_type = %s")
        params.append(lead_type)

    if via_f:
        conditions.append(f"{JOB_VIA_KEY_SQL} = LOWER(%s)")
        params.append(via_f)

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
               status, source_platform, created_at, updated_at, last_discovered_at,
               job_details, contact_evidence
        FROM job_leads
        {where}
        ORDER BY {order_clause}
        LIMIT %s OFFSET %s;
    """
    facet_sql = f"""
        SELECT 'service' AS facet, lead_type AS value, lead_type AS label, COUNT(*) AS count
        FROM job_leads WHERE {scope_where} GROUP BY lead_type
        UNION ALL
        SELECT 'via', {JOB_VIA_KEY_SQL}, MIN({JOB_VIA_LABEL_SQL}), COUNT(*)
        FROM job_leads WHERE {scope_where} AND {JOB_VIA_KEY_SQL} <> ''
        GROUP BY {JOB_VIA_KEY_SQL}
        UNION ALL
        SELECT 'status', status, status, COUNT(*)
        FROM job_leads WHERE user_id = %s AND COALESCE(status, '') <> '' GROUP BY status
        ORDER BY 1, 4 DESC, 3;
    """

    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(metrics_sql, tuple(params))
            metrics = cur.fetchone()
            cur.execute(data_sql, tuple(params) + (per_page, offset))
            records = cur.fetchall()
            filter_options = read_filter_options(
                cur, facet_sql, scope_params + scope_params + [active_user_id()])

    return jsonify({
        "status":       "success",
        "current_page": page,
        "per_page":     per_page,
        "leads":        records,
        "filter_options": filter_options,
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
            cur.execute("UPDATE job_leads SET status = 'rejected' WHERE id = %s AND user_id = %s;", (lead_id, active_user_id()))
            if cur.rowcount == 0:
                return jsonify({"status": "error", "message": "That job lead is not in your workspace."}), 404
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
                    WHERE id = %s AND user_id = %s;""",
                (new_email or None, lead_id, active_user_id())
            )
            if cur.rowcount == 0:
                return jsonify({"status": "error", "message": "That job lead is not in your workspace."}), 404
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
    owner_id = active_user_id()

    def bulk_pitch_worker():
        from niche_prompts import (
            get_niche_parameters, get_service_parameters, OUTSOURCING_SERVICE_TYPES
        )
        bind_log_user(owner_id)
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
                          AND user_id = %s
                        ORDER BY lead_score DESC, id DESC
                        LIMIT %s;
                        """,
                        (owner_id, limit)
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
                                   WHERE id = %s AND user_id = %s;""",
                                (pitch_body, subject_line, suggested_angle, lead['id'], owner_id)
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
    query = f"SELECT {col_str} FROM prospects WHERE user_id = %s"
    params = [active_user_id()]

    if status_filter:
        query += " AND status = %s"
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
    owner_id = active_user_id()

    def score_worker():
        from lead_scorer import run_lead_scoring
        bind_log_user(owner_id)
        run_lead_scoring(owner_id)

    thread = threading.Thread(target=score_worker)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Lead scoring pass started."}), 202


@app.route('/api/trigger/check-websites', methods=['POST'])
def trigger_website_checker():
    """Runs Google PageSpeed audit on prospects that have a website but no quality score yet."""
    owner_id = active_user_id()

    def checker_worker():
        from website_checker import run_website_quality_check
        bind_log_user(owner_id)
        run_website_quality_check(owner_id)

    thread = threading.Thread(target=checker_worker)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Website quality audit started."}), 202


@app.route('/api/trigger/followup-sweep', methods=['POST'])
def trigger_followup_sweep():
    """Runs the follow-up scheduler sweep — stages next-in-sequence drafts for overdue leads."""
    owner_id = active_user_id()

    def sweep_worker():
        from followup_scheduler import run_followup_sweep
        bind_log_user(owner_id)
        run_followup_sweep(owner_id)

    thread = threading.Thread(target=sweep_worker)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Follow-up sweep started."}), 202


if __name__ == '__main__':
    # Debug is opt-in: the Werkzeug debugger is a remote code execution path,
    # which is unacceptable once the app serves more than one account.
    debug = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes"}
    app.run(host=os.getenv("APP_HOST", "127.0.0.1"),
            port=int(os.getenv("APP_PORT", "5000")), debug=debug)