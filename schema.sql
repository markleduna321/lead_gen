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
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS source_platform   VARCHAR(50)  DEFAULT 'google_maps';
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS email             TEXT;
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS screenshot_path   TEXT;

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
CREATE INDEX IF NOT EXISTS idx_prospects_lead_type   ON prospects (lead_type);
CREATE INDEX IF NOT EXISTS idx_prospects_followup    ON prospects (followup_due_at)
    WHERE followup_due_at IS NOT NULL;