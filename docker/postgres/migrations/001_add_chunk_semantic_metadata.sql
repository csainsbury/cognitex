-- Migration: Add semantic metadata columns to document_chunks
-- Run this to upgrade existing databases

-- Add new columns for enhanced semantic chunking
ALTER TABLE document_chunks
ADD COLUMN IF NOT EXISTS section_title VARCHAR(500),
ADD COLUMN IF NOT EXISTS chunk_type VARCHAR(50),
ADD COLUMN IF NOT EXISTS importance FLOAT DEFAULT 0.5,
ADD COLUMN IF NOT EXISTS contains_decision BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS contains_action_item BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS contains_risk BOOLEAN DEFAULT FALSE;

-- Create partial indexes for efficient filtering
CREATE INDEX IF NOT EXISTS idx_document_chunks_importance
    ON document_chunks(importance) WHERE importance > 0.7;
CREATE INDEX IF NOT EXISTS idx_document_chunks_decisions
    ON document_chunks(drive_id) WHERE contains_decision = true;
CREATE INDEX IF NOT EXISTS idx_document_chunks_actions
    ON document_chunks(drive_id) WHERE contains_action_item = true;

-- Update comment
COMMENT ON TABLE document_chunks IS 'Stores document chunks for semantic search with overlap and semantic metadata';
