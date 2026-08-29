-- 项目状态持久化数据库初始化脚本（Supabase PostgreSQL）
-- 用法：在 Supabase 后台 → SQL Editor → New query，粘贴本文件全部内容后 Run。
-- 幂等：可重复执行（使用 IF NOT EXISTS）。

-- gen_random_uuid()：PostgreSQL 13+ 内置；扩展兜底
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ===== projects =====
CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_projects_status      ON projects (status);
CREATE INDEX IF NOT EXISTS idx_projects_created_at  ON projects (created_at);

-- ===== tasks =====
CREATE TABLE IF NOT EXISTS tasks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT,
    owner       TEXT,
    status      TEXT NOT NULL DEFAULT 'todo',
    priority    TEXT,
    dependency  TEXT,
    due_date    DATE,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks (project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks (created_at);

-- ===== decisions =====
CREATE TABLE IF NOT EXISTS decisions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    content        TEXT NOT NULL,
    decision_maker TEXT,
    decision_date  DATE,
    status         TEXT,
    source         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decisions_project_id ON decisions (project_id);
CREATE INDEX IF NOT EXISTS idx_decisions_status     ON decisions (status);
CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions (created_at);

-- ===== risks =====
CREATE TABLE IF NOT EXISTS risks (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    severity   TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    owner      TEXT,
    impact     TEXT,
    source     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_risks_project_id ON risks (project_id);
CREATE INDEX IF NOT EXISTS idx_risks_status     ON risks (status);
CREATE INDEX IF NOT EXISTS idx_risks_created_at ON risks (created_at);

-- ===== events =====
CREATE TABLE IF NOT EXISTS events (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    event_date DATE,
    content    TEXT NOT NULL,
    source     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_project_id ON events (project_id);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at);
