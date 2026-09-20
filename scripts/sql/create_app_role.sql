-- Create the two database roles used by the platform.
--
-- WHY THIS IS NOT PART OF THE MIGRATION:
--   Creating a role needs a password, and a password is a secret - putting
--   one in a migration file means committing it to the repository, which is
--   a project red line. So role creation is an operational action with the
--   password passed in as a psql variable and never written to disk.
--
-- USAGE (local development):
--   psql -U postgres -h 127.0.0.1 -p 5432 -d study_platform ^
--        -v app_password='dev-only-app-password' ^
--        -v worker_password='dev-only-worker-password' ^
--        -f scripts/sql/create_app_role.sql
--
-- WHY THERE ARE TWO ROLES (the credentials boundary):
--   study_app    - serves HTTP requests. It may read/write only rows that
--                  belong to the tenant/project it established, so a bug in
--                  the application layer cannot leak another tenant's data.
--   study_worker - drains the durable ingestion queue. It must discover
--                  "which tenant has work" BEFORE any tenant context exists,
--                  so it needs a cross-tenant view of the queue table.
--
--   The two boundaries must not be merged: giving study_app the worker
--   policy means any connection can call
--   set_config('app.worker_id', 'x') - a custom GUC is NOT a credential -
--   and read/modify every tenant's jobs. That is why the worker policy is
--   limited TO study_worker (see 0008_worker_role_boundary.py).
--
-- WHY THESE TWO ATTRIBUTES MATTER:
--   NOSUPERUSER   - a superuser bypasses row level security entirely
--   NOBYPASSRLS   - without this the role simply ignores every policy
--   Either one alone makes RLS pure theatre, so both are required.
--
-- Pure ASCII on purpose (see the note in scripts/pg_start.cmd).

SELECT format(
    'CREATE ROLE study_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'study_app')
\gexec

SELECT format(
    'CREATE ROLE study_worker LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
    :'worker_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'study_worker')
\gexec

-- If a role already existed with wrong attributes, correct them.
-- Re-running this file is therefore safe.
ALTER ROLE study_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE study_worker NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
