-- Create the application role used by the platform.
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
--        -f scripts/sql/create_app_role.sql
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

-- If the role already existed with wrong attributes, correct them.
-- Re-running this file is therefore safe.
ALTER ROLE study_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
