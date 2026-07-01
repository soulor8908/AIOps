-- AIOps Console — PostgreSQL 初始化脚本
-- 执行顺序：扩展 → 表 → 索引
-- 所有表均带 created_at / updated_at 审计字段

-- ========== 扩展 ==========
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "citext";     -- 大小写不敏感文本

-- ========== users ==========
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         CITEXT UNIQUE NOT NULL,
    username      VARCHAR(64) UNIQUE NOT NULL,
    full_name     VARCHAR(128),
    hashed_password VARCHAR(255) NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ========== prompts ==========
CREATE TABLE IF NOT EXISTS prompts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name               VARCHAR(128) NOT NULL,
    description        TEXT,
    current_version_id UUID,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_prompts_name ON prompts (name);
CREATE INDEX IF NOT EXISTS idx_prompts_active ON prompts (is_active);

-- ========== prompt_versions ==========
CREATE TABLE IF NOT EXISTS prompt_versions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id   UUID NOT NULL REFERENCES prompts (id) ON DELETE CASCADE,
    version_num INTEGER NOT NULL,
    content     TEXT NOT NULL,
    variables   JSONB NOT NULL DEFAULT '[]'::jsonb,
    change_note VARCHAR(255),
    created_by  VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (prompt_id, version_num)
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_prompt_id ON prompt_versions (prompt_id);
-- 幂等外键：仅在约束不存在时添加，避免重跑 init.sql 报错。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_prompts_current_version'
    ) THEN
        ALTER TABLE prompts
            ADD CONSTRAINT fk_prompts_current_version
            FOREIGN KEY (current_version_id) REFERENCES prompt_versions (id) ON DELETE SET NULL;
    END IF;
END $$;

-- ========== agents ==========
CREATE TABLE IF NOT EXISTS agents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR(128) NOT NULL,
    description   TEXT,
    system_prompt TEXT,
    model_alias   VARCHAR(64) NOT NULL DEFAULT 'default',
    tools         JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_turns     INTEGER NOT NULL DEFAULT 10,
    temperature   REAL NOT NULL DEFAULT 0.7,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agents_active ON agents (is_active);

-- ========== workflows ==========
CREATE TABLE IF NOT EXISTS workflows (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(128) NOT NULL,
    description TEXT,
    nodes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    edges       JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflows_active ON workflows (is_active);

-- ========== knowledge_bases ==========
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL,
    description     TEXT,
    embedding_model VARCHAR(64) NOT NULL DEFAULT 'text-embedding-3-small',
    chunk_size      INTEGER NOT NULL DEFAULT 800,
    chunk_overlap   INTEGER NOT NULL DEFAULT 100,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ========== documents ==========
CREATE TABLE IF NOT EXISTS documents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_base_id   UUID NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    title               VARCHAR(255) NOT NULL,
    source_uri          TEXT,
    mime_type           VARCHAR(64),
    size_bytes          BIGINT,
    chunk_count         INTEGER NOT NULL DEFAULT 0,
    status              VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_kb ON documents (knowledge_base_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);

-- ========== chunks ==========
-- 向量维度 1536 对齐 OpenAI text-embedding-3-small
CREATE TABLE IF NOT EXISTS chunks (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id       UUID NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    chunk_index       INTEGER NOT NULL,
    content           TEXT NOT NULL,
    embedding         VECTOR(1536),
    token_count       INTEGER,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_kb ON chunks (knowledge_base_id);
-- HNSW 向量索引，余弦距离
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ========== model_configs ==========
CREATE TABLE IF NOT EXISTS model_configs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alias         VARCHAR(64) UNIQUE NOT NULL,
    provider      VARCHAR(32) NOT NULL,
    model_name    VARCHAR(128) NOT NULL,
    api_base      TEXT,
    api_key_env   VARCHAR(64),
    max_tokens    INTEGER NOT NULL DEFAULT 4096,
    temperature   REAL NOT NULL DEFAULT 0.7,
    cost_per_1k_input   NUMERIC(10,6) NOT NULL DEFAULT 0,
    cost_per_1k_output  NUMERIC(10,6) NOT NULL DEFAULT 0,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    priority      INTEGER NOT NULL DEFAULT 100,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_models_active_priority ON model_configs (is_active, priority);

-- ========== conversations ==========
CREATE TABLE IF NOT EXISTS conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID REFERENCES users (id) ON DELETE SET NULL,
    agent_id      UUID REFERENCES agents (id) ON DELETE SET NULL,
    model_alias   VARCHAR(64),
    title         VARCHAR(255),
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    total_cost    NUMERIC(12,6) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_agent ON conversations (agent_id);
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations (created_at DESC);

-- ========== messages ==========
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            VARCHAR(16) NOT NULL,
    content         TEXT NOT NULL,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    model_alias     VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, created_at);

-- ========== eval_rules ==========
-- 评估规则定义（单条断言），与 ORM app.domains.evals.models.EvalRule 对齐
CREATE TABLE IF NOT EXISTS eval_rules (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR(128) NOT NULL,
    description   TEXT,
    judge_type    VARCHAR(32) NOT NULL DEFAULT 'exact',
    expected      TEXT,
    config        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eval_rules_judge_type ON eval_rules (judge_type);

-- ========== eval_judges ==========
-- 判官配置（LLM-as-judge 的模型与 prompt），与 ORM EvalJudge 对齐
CREATE TABLE IF NOT EXISTS eval_judges (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL,
    judge_type      VARCHAR(32) NOT NULL,
    model_alias     VARCHAR(64) NOT NULL DEFAULT 'default',
    prompt_template TEXT NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eval_judges_judge_type ON eval_judges (judge_type);

-- ========== eval_cases ==========
-- 评估用例：input + expected + 可选 metadata，与 ORM EvalCase 对齐
CREATE TABLE IF NOT EXISTS eval_cases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(128),
    input       TEXT NOT NULL,
    expected    TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eval_cases_name ON eval_cases (name);

-- ========== eval_runs ==========
-- UUID 主键，支持分布式评估任务追踪
CREATE TABLE IF NOT EXISTS eval_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL,
    description     TEXT,
    rules           JSONB NOT NULL DEFAULT '[]'::jsonb,
    cases           JSONB NOT NULL DEFAULT '[]'::jsonb,
    judge_type      VARCHAR(32) NOT NULL DEFAULT 'exact',
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    results         JSONB,
    pass_count      INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    score           REAL,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_status ON eval_runs (status);
CREATE INDEX IF NOT EXISTS idx_eval_runs_created ON eval_runs (created_at DESC);

-- ========== 触发器：updated_at 自动更新 ==========
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'users','prompts','prompt_versions','agents','workflows',
        'knowledge_bases','documents','chunks','model_configs',
        'conversations','messages','eval_rules','eval_judges',
        'eval_cases','eval_runs'
    ])
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_touch ON %s;'
            'CREATE TRIGGER trg_%s_touch BEFORE UPDATE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION touch_updated_at();',
            t, t, t, t
        );
    END LOOP;
END $$;

-- ========== 默认模型配置 ==========
INSERT INTO model_configs (alias, provider, model_name, max_tokens, priority)
VALUES
    ('default',    'openai',    'gpt-4o-mini',     4096, 100),
    ('gpt-4o',     'openai',    'gpt-4o',          4096,  90),
    ('claude-3.5', 'anthropic', 'claude-3-5-sonnet-20241022', 4096, 80)
ON CONFLICT (alias) DO NOTHING;
