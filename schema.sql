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

CREATE TRIGGER update_prospects_modtime
    BEFORE UPDATE ON prospects
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();