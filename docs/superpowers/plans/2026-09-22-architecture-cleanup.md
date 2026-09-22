# Architecture Cleanup Implementation Plan

**Goal:** Remove verified legacy exposure and dependency cycles without changing supported product behavior.

**Safety boundary:** Preserve all database migrations, production data, public product APIs, authentication flows, and development adapters. Keep compatibility re-exports where existing tests or callers rely on old import paths.

## Task 1: Separate stable and legacy HTTP routes

- Add a production-surface regression test proving legacy development endpoints return 404 in production.
- Keep `/healthz`, `/me`, and `/mastery` on the stable router.
- Mount the legacy router only outside production.
- Run API and authentication tests.

## Task 2: Make PostgreSQL genuinely optional at import time

- Add a subprocess test that blocks `psycopg` and imports `app.main` in development mode.
- Move PostgreSQL idempotency and audit adapters out of API/domain modules.
- Delay PostgreSQL adapter imports until `use_postgres` is selected.
- Preserve the existing public imports as compatibility aliases where they do not reintroduce database imports.
- Run import-direction, memory-adapter, and PostgreSQL contract tests.

## Task 3: Remove direct package cycles

- Move identity-owned session and invitation models into `app.identity.models`.
- Retain product-model compatibility re-exports while migrating production imports.
- Move learning validation rules into a neutral domain module used by both adapters.
- Add targeted architecture invariants for `api -> db`, `audit -> db`, `identity -> product`, and `db -> memory implementation`.
- Run identity, product, learning, and architecture tests.

## Task 4: Use one provider configuration source

- Add a regression test that builds a provider from an explicit settings mapping without reading process environment variables.
- Store provider URL and secret in `DeploymentSettings`; hide the secret from repr.
- Make the provider factory consume only the settings object.
- Run provider, deployment, worker, and paid-dispatch tests.

## Task 5: Documentation and verification

- Correct stale README claims about deployment, authentication, PostgreSQL, frontend, and the real teaching provider.
- Record findings and progress, without deleting historical evidence or backups.
- Run the full backend suite, PostgreSQL subset, Ruff, mypy, compile checks, frontend syntax checks, contract checks, and `git diff --check`.
- Deploy only after all local gates pass and a fresh production backup exists.

