-- Phase 1: Core Prospects Schema
CREATE TABLE IF NOT EXISTS prospects (
    id SERIAL PRIMARY KEY,
    business_name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    phone VARCHAR(50),
    formatted_address TEXT,
    google_place_id VARCHAR(255) NOT NULL,
    review_count INT DEFAULT 0,
    rating NUMERIC(2,1),
    suggested_angle TEXT,
    ai_pitch_draft TEXT,
    status VARCHAR(50) DEFAULT 'discovered', -- discovered, evaluated, drafted, sent, rejected, closed_won, closed_lost
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Bumped every time a discovery run surfaces the business, including re-finds
    -- that reuse an existing row, so the newest discoveries sort to the top.
    last_discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- Safety constraint to prevent reprocessing the same business entity
    CONSTRAINT unique_google_place UNIQUE (google_place_id)
);

-- Indexing for rapid dashboard lookup and status-based worker queues
CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_prospects_place_id ON prospects(google_place_id);

-- Simple trigger function to handle the updated_at timestamp modification
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_prospects_modtime ON prospects;
CREATE TRIGGER update_prospects_modtime
    BEFORE UPDATE ON prospects
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

-- Phase 2: International Lead Gen Expansion Migration
-- Run these ALTER statements once on an existing database to apply the upgrade.
-- The CREATE TABLE above already includes all columns for fresh installs.
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS lead_score        INT          DEFAULT 0;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS site_quality_score INT         DEFAULT NULL;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS lead_type         VARCHAR(50)  DEFAULT 'web_design';
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS subject_line      TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS followup_due_at   TIMESTAMP    DEFAULT NULL;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS website_url       TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS profile_url       TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS source_platform   VARCHAR(50)  DEFAULT 'google_maps';
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS email             TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS screenshot_path   TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS last_discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

-- Phase 3: Job Board Enrichment Columns
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_via          TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_salary       TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_schedule     TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_description  TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS job_apply_link   TEXT;

-- Widen columns that overflow with real-world job/company data
ALTER TABLE prospects ALTER COLUMN category      TYPE VARCHAR(255);
ALTER TABLE prospects ALTER COLUMN business_name TYPE VARCHAR(512);

-- New indexes for priority sorting and follow-up queue
CREATE INDEX IF NOT EXISTS idx_prospects_lead_score  ON prospects (lead_score DESC);
CREATE INDEX IF NOT EXISTS idx_prospects_discovered   ON prospects (COALESCE(last_discovered_at, created_at) DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_prospects_lead_type   ON prospects (lead_type);
CREATE INDEX IF NOT EXISTS idx_prospects_followup    ON prospects (followup_due_at)
    WHERE followup_due_at IS NOT NULL;

-- Phase 4: Dedicated Job Board Leads Table
-- Job leads are structurally different from web-design prospects:
-- New leads use a normalized company/title/location/service identity.
CREATE TABLE IF NOT EXISTS job_leads (
    id              SERIAL PRIMARY KEY,
    company_name    VARCHAR(512)  NOT NULL,
    job_title       VARCHAR(512)  NOT NULL,
    location        TEXT,
    lead_type       VARCHAR(50)   NOT NULL,      -- e.g. 'video_editing', 'bpo_customer_service'
    job_role_signal VARCHAR(255),                -- the job role query that surfaced this company
    job_via         TEXT,                        -- job board name (LinkedIn, Indeed, etc.)
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
    job_identity    TEXT,                        -- NULL for preserved legacy rows
    job_details     JSONB,
    contact_evidence JSONB,
    created_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    -- Bumped every time a discovery run surfaces the posting, including re-finds
    -- that reuse an existing row, so the newest discoveries sort to the top.
    last_discovered_at TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_identity TEXT;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS job_details JSONB;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS contact_evidence JSONB;
ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS last_discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE job_leads DROP CONSTRAINT IF EXISTS unique_job_lead;

CREATE TABLE IF NOT EXISTS hiring_services (
    service_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(80) NOT NULL,
    name_key TEXT NOT NULL,
    roles JSONB NOT NULL CHECK (jsonb_typeof(roles) = 'array' AND jsonb_array_length(roles) BETWEEN 1 AND 4),
    title_phrases JSONB NOT NULL CHECK (jsonb_typeof(title_phrases) = 'array' AND jsonb_array_length(title_phrases) BETWEEN 1 AND 12),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_job_leads_discovered
    ON job_leads (COALESCE(last_discovered_at, created_at) DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_job_leads_status    ON job_leads (status);
CREATE INDEX IF NOT EXISTS idx_job_leads_lead_type ON job_leads (lead_type);
CREATE INDEX IF NOT EXISTS idx_job_leads_email     ON job_leads (email) WHERE email IS NOT NULL;

-- Reuse the same updated_at trigger function defined above
DROP TRIGGER IF EXISTS update_job_leads_modtime ON job_leads;
CREATE TRIGGER update_job_leads_modtime
    BEFORE UPDATE ON job_leads
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

-- ---------------------------------------------------------------------------
-- Phase 5: Accounts and per-user data isolation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    email_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(120),
    role VARCHAR(20) NOT NULL DEFAULT 'member',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    -- Raised to sign out every existing session (password change, sign out everywhere).
    session_version INTEGER NOT NULL DEFAULT 1
);

-- SMTP passwords are encrypted (reversible), never hashed: they must be
-- replayed to the mail server. The key lives in SMTP_ENCRYPTION_KEY.
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

ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_title   VARCHAR(120);
ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_company VARCHAR(120);
ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_phone   VARCHAR(60);
ALTER TABLE user_email_settings ADD COLUMN IF NOT EXISTS signature_website TEXT;

-- Ownership columns. Added nullable so the migration stays additive; the
-- one-time scripts/reset_multiuser.py promotes them to NOT NULL.
ALTER TABLE prospects        ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;
ALTER TABLE job_leads        ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;
ALTER TABLE hiring_services  ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users (id) ON DELETE CASCADE;

-- Uniqueness becomes per-user: two accounts may independently own the same
-- business, job posting, or service name.
ALTER TABLE prospects       DROP CONSTRAINT IF EXISTS unique_google_place;
ALTER TABLE hiring_services DROP CONSTRAINT IF EXISTS hiring_services_name_key_key;
DROP INDEX IF EXISTS idx_job_leads_identity;
CREATE UNIQUE INDEX IF NOT EXISTS idx_prospects_user_place      ON prospects (user_id, google_place_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_leads_user_identity   ON job_leads (user_id, job_identity);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hiring_services_user_name ON hiring_services (user_id, name_key);
CREATE INDEX IF NOT EXISTS idx_prospects_user ON prospects (user_id);
CREATE INDEX IF NOT EXISTS idx_job_leads_user ON job_leads (user_id);