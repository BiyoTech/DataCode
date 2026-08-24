-- Database initialization for DataCode platform.
-- Creates necessary databases and extensions.

-- Database for Gitea
CREATE DATABASE gitea OWNER datacode;

-- Database for SQLMesh state (separate from platform metadata)
CREATE DATABASE sqlmesh_state OWNER datacode;

-- Extensions for the main datacode database
\c datacode;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Extensions for sqlmesh_state database
\c sqlmesh_state;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =====================================================
-- Platform metadata tables (datacode database)
-- =====================================================

\c datacode;

-- Git 授权凭证表
CREATE TABLE IF NOT EXISTS git_credentials (
    credential_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL,
    user_id UUID,  -- NULL = 租户级共享凭证
    provider VARCHAR(50) NOT NULL CHECK (provider IN ('github', 'gitlab', 'gitea', 'custom')),
    auth_type VARCHAR(20) NOT NULL CHECK (auth_type IN ('oauth', 'token', 'ssh')),
    name VARCHAR(255) NOT NULL,
    -- OAuth 相关（加密存储）
    oauth_access_token TEXT,
    oauth_refresh_token TEXT,
    oauth_expires_at TIMESTAMP,
    -- Personal Access Token（加密存储）
    pat_token TEXT,
    -- SSH Key（加密存储）
    ssh_private_key TEXT,
    ssh_public_key TEXT,
    -- 通用字段
    git_host VARCHAR(500),
    git_username VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    last_verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(tenant_id, user_id, provider, auth_type, name)
);

-- 项目仓库绑定表
CREATE TABLE IF NOT EXISTS project_repositories (
    binding_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL,
    project_id UUID NOT NULL,
    credential_id UUID REFERENCES git_credentials(credential_id),
    repo_url VARCHAR(500) NOT NULL,
    repo_owner VARCHAR(255),
    repo_name VARCHAR(255),
    default_branch VARCHAR(255) DEFAULT 'main',
    branch_strategy VARCHAR(50) DEFAULT 'branch_per_user' CHECK (branch_strategy IN ('direct', 'fork', 'branch_per_user')),
    fork_repo_url VARCHAR(500),
    webhook_id VARCHAR(255),
    webhook_secret TEXT,  -- 加密存储
    sync_status VARCHAR(50) DEFAULT 'active' CHECK (sync_status IN ('active', 'error', 'syncing')),
    last_sync_at TIMESTAMP,
    created_by UUID,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_git_credentials_tenant ON git_credentials(tenant_id);
CREATE INDEX IF NOT EXISTS idx_git_credentials_user ON git_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_project_repositories_tenant ON project_repositories(tenant_id);
CREATE INDEX IF NOT EXISTS idx_project_repositories_project ON project_repositories(project_id);