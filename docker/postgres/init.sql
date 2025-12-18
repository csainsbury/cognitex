-- Enable pgvector extension for embeddings
CREATE EXTENSION IF NOT EXISTS vector;

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Tasks table with full workflow status
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    energy_cost INTEGER CHECK (energy_cost >= 1 AND energy_cost <= 10),
    due_date TIMESTAMPTZ,
    source_type VARCHAR(50),  -- 'email', 'calendar', 'manual', 'inferred'
    source_id VARCHAR(255),   -- Reference to source (gmail_id, gcal_id, etc.)
    project_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_due_date ON tasks(due_date);
CREATE INDEX idx_tasks_source ON tasks(source_type, source_id);

-- Goals table (OKR-style)
CREATE TABLE goals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    timeframe VARCHAR(50) NOT NULL,  -- 'yearly', 'quarterly', 'monthly', 'weekly'
    domain VARCHAR(50),              -- 'work', 'personal', 'health', etc.
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    progress INTEGER DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    key_results JSONB DEFAULT '[]',
    parent_id UUID REFERENCES goals(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_goals_timeframe ON goals(timeframe);
CREATE INDEX idx_goals_status ON goals(status);

-- Energy logs for time-series tracking
CREATE TABLE energy_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    energy_level INTEGER NOT NULL CHECK (energy_level >= 1 AND energy_level <= 10),
    predicted_level INTEGER CHECK (predicted_level >= 1 AND predicted_level <= 10),
    notes TEXT,
    source VARCHAR(50) DEFAULT 'manual'  -- 'manual', 'inferred', 'calendar'
);

CREATE INDEX idx_energy_logs_time ON energy_logs(logged_at);

-- Draft actions awaiting approval
CREATE TABLE draft_actions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    action_type VARCHAR(50) NOT NULL,  -- 'email_reply', 'calendar_event', 'task_create'
    payload JSONB NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',  -- 'pending', 'approved', 'rejected', 'expired'
    confidence FLOAT,
    source_context JSONB,  -- Reference to what triggered this draft
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

CREATE INDEX idx_draft_actions_status ON draft_actions(status);
CREATE INDEX idx_draft_actions_type ON draft_actions(action_type);

-- Embeddings store for semantic search
CREATE TABLE embeddings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_type VARCHAR(50) NOT NULL,  -- 'email', 'task', 'goal', 'person', 'document'
    entity_id VARCHAR(255) NOT NULL,
    content_hash VARCHAR(64),          -- To detect when re-embedding needed
    embedding vector(768),             -- Together.ai m2-bert embedding dimension
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(entity_type, entity_id)
);

CREATE INDEX idx_embeddings_entity ON embeddings(entity_type, entity_id);

-- Create HNSW index for fast similarity search
CREATE INDEX idx_embeddings_vector ON embeddings USING hnsw (embedding vector_cosine_ops);

-- Document content store for full-text indexed files
CREATE TABLE document_content (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    drive_id VARCHAR(255) NOT NULL UNIQUE,
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    char_count INTEGER NOT NULL,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_document_content_drive_id ON document_content(drive_id);
CREATE INDEX idx_document_content_hash ON document_content(content_hash);

-- Full-text search index on document content
CREATE INDEX idx_document_content_fts ON document_content USING gin(to_tsvector('english', content));

-- Sync state for incremental Gmail/GCal sync
CREATE TABLE sync_state (
    id VARCHAR(100) PRIMARY KEY,       -- 'gmail', 'gcal', etc.
    last_sync_at TIMESTAMPTZ,
    history_id VARCHAR(255),           -- Gmail history ID
    sync_token VARCHAR(500),           -- GCal sync token
    metadata JSONB DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit log for tracking all changes
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_type VARCHAR(50) NOT NULL,
    entity_id VARCHAR(255) NOT NULL,
    action VARCHAR(50) NOT NULL,       -- 'create', 'update', 'delete', 'approve', 'reject'
    changes JSONB,
    actor VARCHAR(100) DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_log_time ON audit_log(created_at);

-- Function to auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply auto-update trigger to relevant tables
CREATE TRIGGER update_tasks_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_goals_updated_at BEFORE UPDATE ON goals
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_sync_state_updated_at BEFORE UPDATE ON sync_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Agent Episodic Memory for long-term storage
CREATE TABLE IF NOT EXISTS agent_memory (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,  -- 'decision', 'interaction', 'feedback', 'preference'
    content TEXT NOT NULL,
    entities TEXT[] DEFAULT '{}',
    importance INTEGER DEFAULT 3 CHECK (importance >= 1 AND importance <= 5),
    embedding vector(768),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    accessed_at TIMESTAMPTZ DEFAULT NOW(),
    access_count INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_type ON agent_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_agent_memory_created ON agent_memory(created_at);
CREATE INDEX IF NOT EXISTS idx_agent_memory_importance ON agent_memory(importance);

-- Vector similarity search index for memory retrieval
CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
    ON agent_memory USING hnsw (embedding vector_cosine_ops);

-- Code content store for GitHub repository files
CREATE TABLE IF NOT EXISTS code_content (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    file_id VARCHAR(500) NOT NULL UNIQUE,  -- repo_id:path format
    repo_name VARCHAR(255) NOT NULL,       -- owner/repo format
    path VARCHAR(500) NOT NULL,            -- File path within repo
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    char_count INTEGER NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_code_content_file_id ON code_content(file_id);
CREATE INDEX IF NOT EXISTS idx_code_content_repo ON code_content(repo_name);
CREATE INDEX IF NOT EXISTS idx_code_content_path ON code_content(path);

-- Full-text search index on code content
CREATE INDEX IF NOT EXISTS idx_code_content_fts ON code_content USING gin(to_tsvector('english', content));

-- Document chunks for semantic search with overlapping context
CREATE TABLE IF NOT EXISTS document_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    drive_id VARCHAR(255) NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(drive_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_drive_id ON document_chunks(drive_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_hash ON document_chunks(content_hash);
CREATE INDEX IF NOT EXISTS idx_document_chunks_fts ON document_chunks USING gin(to_tsvector('english', content));

COMMENT ON TABLE document_chunks IS 'Stores document chunks for semantic search with overlap';
