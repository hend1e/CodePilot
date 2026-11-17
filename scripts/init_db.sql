-- ============================================================
-- CodePilot 数据库初始化（Docker 首次启动自动执行）
-- 约定：所有表带 tenant_id（多租户隔离）；所有时间用 TIMESTAMPTZ。
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── 用户与权限 ──
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64)  NOT NULL DEFAULT 'tenant_default',
    username        VARCHAR(64)  NOT NULL,
    email           VARCHAR(128) NOT NULL,
    password_hash   VARCHAR(256) NOT NULL,
    role            VARCHAR(16)  NOT NULL DEFAULT 'developer'
                    CHECK (role IN ('developer', 'reviewer', 'admin')),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);

-- ── 接入的代码仓库 ──
CREATE TABLE IF NOT EXISTS repos (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64)  NOT NULL DEFAULT 'tenant_default',
    provider        VARCHAR(16)  NOT NULL DEFAULT 'local'
                    CHECK (provider IN ('local', 'github', 'gitlab')),
    full_name       VARCHAR(256) NOT NULL,          -- owner/repo 或本地目录名
    default_branch  VARCHAR(128) NOT NULL DEFAULT 'main',
    clone_url       VARCHAR(512),
    local_path      VARCHAR(512),                   -- 本地索引与沙箱 checkout 用
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, full_name)
);

-- ── PR 元信息（幂等键 = repo + number + head_sha）──
CREATE TABLE IF NOT EXISTS pull_requests (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    repo_id         UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    number          INT  NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    author          VARCHAR(128) NOT NULL DEFAULT '',
    base_sha        VARCHAR(64),
    head_sha        VARCHAR(64)  NOT NULL,
    html_url        VARCHAR(512),
    status          VARCHAR(16)  NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed', 'merged')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (repo_id, number, head_sha)
);
CREATE INDEX IF NOT EXISTS idx_pr_repo ON pull_requests (repo_id, number);

-- ── 审查任务（一次图执行的工单）──
CREATE TABLE IF NOT EXISTS review_tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64)  NOT NULL DEFAULT 'tenant_default',
    repo_id         UUID REFERENCES repos(id) ON DELETE SET NULL,
    pr_id           UUID REFERENCES pull_requests(id) ON DELETE SET NULL,
    thread_id       VARCHAR(128) NOT NULL UNIQUE,     -- LangGraph checkpointer 线程
    kind            VARCHAR(16)  NOT NULL DEFAULT 'pr_review'
                    CHECK (kind IN ('pr_review', 'defect_locate', 'test_gen')),
    status          VARCHAR(20)  NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'waiting_human',
                                      'done', 'failed', 'cancelled', 'superseded')),
    source          VARCHAR(16)  NOT NULL DEFAULT 'manual',  -- manual | webhook
    idempotency_key VARCHAR(256),
    diff_ref        VARCHAR(512),                     -- 本地 diff 文件或 PR 引用
    trigger_payload JSONB,
    final           JSONB,
    error_msg       TEXT,
    degraded        BOOLEAN      NOT NULL DEFAULT FALSE,
    prompt_tokens   INT          NOT NULL DEFAULT 0,
    completion_tokens INT        NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(10, 6) NOT NULL DEFAULT 0,
    steps           INT          NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON review_tasks (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON review_tasks (status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_idempotency
    ON review_tasks (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- ── 审查发现（每条结论必须带 evidence）──
CREATE TABLE IF NOT EXISTS findings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
    fingerprint     VARCHAR(32)  NOT NULL,          -- 去重指纹
    dimension       VARCHAR(16)  NOT NULL
                    CHECK (dimension IN ('security', 'performance', 'style', 'test')),
    severity        VARCHAR(8)   NOT NULL
                    CHECK (severity IN ('block', 'warn', 'info')),
    file_path       VARCHAR(512) NOT NULL,
    line_start      INT NOT NULL DEFAULT 0,
    line_end        INT NOT NULL DEFAULT 0,
    rule_id         VARCHAR(64),
    evidence        TEXT NOT NULL,
    message         TEXT NOT NULL,
    suggestion      TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    source          VARCHAR(8)   NOT NULL DEFAULT 'rule' CHECK (source IN ('rule', 'llm')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (task_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_findings_task_sev ON findings (task_id, severity);

-- ── 单测生成与沙箱执行结果 ──
CREATE TABLE IF NOT EXISTS test_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
    patch           TEXT,
    target_files    JSONB NOT NULL DEFAULT '[]',
    status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'passed', 'failed', 'error', 'skipped')),
    passed          INT NOT NULL DEFAULT 0,
    failed          INT NOT NULL DEFAULT 0,
    coverage        REAL,
    report          JSONB,
    sandbox_image   VARCHAR(128),
    duration_ms     INT,
    error           TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_test_runs_task ON test_runs (task_id);

-- ── 人工确认（HitL）──
CREATE TABLE IF NOT EXISTS hitl_decisions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
    action          VARCHAR(16) NOT NULL CHECK (action IN ('approve', 'reject', 'edit')),
    payload         JSONB NOT NULL DEFAULT '{}',
    decided_by      VARCHAR(128) NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hitl_task ON hitl_decisions (task_id);

-- ── 缺陷定位结果 ──
CREATE TABLE IF NOT EXISTS defect_reports (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID REFERENCES review_tasks(id) ON DELETE CASCADE,
    stack_trace     TEXT NOT NULL,
    candidates      JSONB NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 0,
    suggestion      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 代码库索引：文档 / 函数级 chunk ──
CREATE TABLE IF NOT EXISTS code_documents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    repo_id         UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path            VARCHAR(1024) NOT NULL,
    lang            VARCHAR(32)  NOT NULL DEFAULT 'text',
    sha             VARCHAR(64)  NOT NULL,
    size_bytes      INT NOT NULL DEFAULT 0,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_id, path, sha)
);

CREATE TABLE IF NOT EXISTS code_chunks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    doc_id          UUID NOT NULL REFERENCES code_documents(id) ON DELETE CASCADE,
    repo_id         UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path            VARCHAR(1024) NOT NULL,
    symbol          VARCHAR(256) NOT NULL DEFAULT '',
    symbol_type     VARCHAR(32)  NOT NULL DEFAULT 'block',
    start_line      INT NOT NULL DEFAULT 1,
    end_line        INT NOT NULL DEFAULT 1,
    content         TEXT NOT NULL,
    token_estimate  INT NOT NULL DEFAULT 0,
    sparse_tsv      tsvector            -- content + 中文 bigram，由应用层写入
);
CREATE INDEX IF NOT EXISTS idx_chunks_repo ON code_chunks (repo_id);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON code_chunks USING GIN (sparse_tsv);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON code_chunks (repo_id, path);

-- ── RAG 问答审计（引用可追溯）──
CREATE TABLE IF NOT EXISTS rag_queries (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    task_id         UUID REFERENCES review_tasks(id) ON DELETE SET NULL,
    repo_id         UUID REFERENCES repos(id) ON DELETE SET NULL,
    question        TEXT NOT NULL,
    answer          TEXT,
    citations       JSONB NOT NULL DEFAULT '[]',
    degraded        BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms      INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── updated_at 自动维护 ──
CREATE OR REPLACE FUNCTION update_updated_at_column() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['users', 'repos', 'pull_requests', 'review_tasks']
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_updated_at ON %s;
             CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON %s
             FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();', t, t, t, t);
    END LOOP;
END;
$$;
