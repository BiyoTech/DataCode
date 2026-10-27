-- Database initialization for the containerized Postgres instances.
--
-- Scope is deliberately narrow: only what the SERVICES THEMSELVES cannot
-- create. Platform tables (workspaces, environments, quotas, ...) are owned
-- by control-service's alembic chain — writing them here would create a
-- second DDL source of truth that drifts from the migrations and wins the
-- race against them on a fresh volume.
--
-- This file runs ONCE per fresh volume (docker-entrypoint-initdb.d), on:
--   * the infra stack's postgres (deploy/docker-compose.infra.yml)
--   * the single-machine stack's postgres (merged infra file)
-- The cell/global stacks use postgres:16-alpine's own POSTGRES_DB env var.

-- Dagster's run/event/schedule storage. Kept in a separate database on
-- purpose: Dagster owns and migrates this schema itself, and those migrations
-- must not interleave with the services' alembic chain.
CREATE DATABASE dagster OWNER datacode;

-- Gitea's own storage. The gitea service (infra stack, "gitea" profile) is
-- configured with GITEA__database__NAME=gitea and, like Dagster, migrates its
-- schema itself. It cannot create the database — its user is not a superuser —
-- so without this line `make infra` starts a gitea that crash-loops on
-- "database gitea does not exist".
CREATE DATABASE gitea OWNER datacode;

-- Extensions for the main datacode database.
\c datacode;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
