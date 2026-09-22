"""Open registration credentials and password-authenticated sessions.

Revision 0012 adds a deliberately narrow database trust boundary:

* password hashes live in ``account_credentials`` and neither the web role nor
  the worker role can read that table directly;
* registration, credential lookup, and login completion are the only public
  entry points, all ``SECURITY DEFINER`` with a ``pg_catalog`` search path;
* session provenance is explicit and enforced by CHECK/composite-FK constraints;
* pre-0012 sessions are labelled ``invitation`` only after every row is proven
  to have been created by the invitation exchange transaction.

The downgrade is for disposable test databases only.  It restores the 0011
exchange function and removes password sessions before dropping their schema.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"
WORKER_ROLE = "study_worker"
MAX_SESSION_TTL_DAYS = 30
DEFAULT_PROJECT_NAME = "我的学习项目"

REGISTER_ACCOUNT_SIGNATURE = (
    "public.register_account(text, text, text, integer, timestamptz)"
)
LOOKUP_ACCOUNT_SIGNATURE = "public.lookup_account_for_login(text)"
COMPLETE_LOGIN_SIGNATURE = (
    "public.complete_account_login(text, bigint, timestamptz, text, integer)"
)
PLATFORM_RESERVE_SIGNATURE = (
    "public.reserve_platform_paid(text, date, text, text, bigint)"
)
PLATFORM_MARK_IN_FLIGHT_SIGNATURE = "public.mark_platform_paid_in_flight(text)"
PLATFORM_RELEASE_HELD_SIGNATURE = "public.release_platform_paid_held(text)"
PLATFORM_RELEASE_FAILED_SIGNATURE = "public.release_platform_paid_failed(text)"
PLATFORM_SETTLE_SIGNATURE = "public.settle_platform_paid(text, bigint)"
EXCHANGE_INVITATION_SIGNATURE = (
    "public.exchange_invitation(text, text, timestamptz)"
)


def _assert_roles_exist() -> None:
    op.execute(
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'role {APP_ROLE} does not exist; create it before migrating';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
        RAISE EXCEPTION 'role {WORKER_ROLE} does not exist; create it before migrating';
    END IF;
END
$$
"""
    )


def _credential_statements() -> list[str]:
    return [
        """
CREATE TABLE public.platform_budget_config (
    config_id boolean PRIMARY KEY DEFAULT true CHECK (config_id),
    monthly_cap_micro bigint NOT NULL DEFAULT 0 CHECK (monthly_cap_micro >= 0),
    paid_dispatch_enabled boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
)
""",
        "INSERT INTO public.platform_budget_config (config_id) VALUES (true)",
        """
CREATE TABLE public.platform_paid_reservations (
    reservation_id text PRIMARY KEY,
    billing_period date NOT NULL,
    provider text NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 100),
    price_version text NOT NULL CHECK (char_length(price_version) BETWEEN 1 AND 100),
    max_spend_micro bigint NOT NULL CHECK (max_spend_micro > 0),
    state text NOT NULL CHECK (state IN ('held','in_flight','settled','released')),
    actual_spend_micro bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    CONSTRAINT platform_paid_actual_check
        CHECK (actual_spend_micro IS NULL OR (actual_spend_micro >= 0 AND actual_spend_micro <= max_spend_micro)),
    CONSTRAINT platform_paid_settled_fields_check
        CHECK ((state = 'settled') = (actual_spend_micro IS NOT NULL))
)
""",
        "CREATE INDEX platform_paid_reservations_period_state_idx ON public.platform_paid_reservations (billing_period, state)",
        "ALTER TABLE public.platform_budget_config ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.platform_budget_config FORCE ROW LEVEL SECURITY",
        "ALTER TABLE public.platform_paid_reservations ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.platform_paid_reservations FORCE ROW LEVEL SECURITY",
        "REVOKE ALL ON TABLE public.platform_budget_config FROM PUBLIC",
        f"REVOKE ALL ON TABLE public.platform_budget_config FROM {APP_ROLE}",
        f"REVOKE ALL ON TABLE public.platform_budget_config FROM {WORKER_ROLE}",
        "REVOKE ALL ON TABLE public.platform_paid_reservations FROM PUBLIC",
        f"REVOKE ALL ON TABLE public.platform_paid_reservations FROM {APP_ROLE}",
        f"REVOKE ALL ON TABLE public.platform_paid_reservations FROM {WORKER_ROLE}",
        """
CREATE TABLE public.account_credentials (
    credential_id       text PRIMARY KEY,
    tenant_id           text NOT NULL,
    principal_id        text NOT NULL,
    username            text NOT NULL,
    username_normalized text COLLATE "C" NOT NULL,
    password_hash       text NOT NULL,
    hash_version        integer NOT NULL,
    security_generation bigint NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    last_login_at       timestamptz,
    disabled_at         timestamptz,
    CONSTRAINT account_credentials_username_length_check
        CHECK (char_length(username) BETWEEN 1 AND 16),
    CONSTRAINT account_credentials_normalized_length_check
        CHECK (char_length(username_normalized) BETWEEN 1 AND 16),
    CONSTRAINT account_credentials_password_hash_check
        CHECK (password_hash LIKE '$argon2id$%'),
    CONSTRAINT account_credentials_hash_version_check
        CHECK (hash_version > 0),
    CONSTRAINT account_credentials_security_generation_check
        CHECK (security_generation > 0),
    CONSTRAINT account_credentials_disabled_time_check
        CHECK (disabled_at IS NULL OR disabled_at >= created_at),
    CONSTRAINT account_credentials_tenant_principal_fk
        FOREIGN KEY (tenant_id, principal_id)
        REFERENCES public.principals (tenant_id, principal_id),
    CONSTRAINT account_credentials_tenant_principal_uq
        UNIQUE (tenant_id, principal_id),
    CONSTRAINT account_credentials_tenant_principal_credential_uq
        UNIQUE (tenant_id, principal_id, credential_id)
)
""",
        """
CREATE UNIQUE INDEX account_credentials_username_normalized_uq
    ON public.account_credentials (username_normalized COLLATE "C")
""",
        "ALTER TABLE public.account_credentials ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.account_credentials FORCE ROW LEVEL SECURITY",
        """
CREATE POLICY account_credentials_isolation ON public.account_credentials
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND principal_id = current_setting('app.principal_id', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND principal_id = current_setting('app.principal_id', true)
    )
""",
        "REVOKE ALL ON TABLE public.account_credentials FROM PUBLIC",
        f"REVOKE ALL ON TABLE public.account_credentials FROM {APP_ROLE}",
        f"REVOKE ALL ON TABLE public.account_credentials FROM {WORKER_ROLE}",
    ]


def _session_upgrade_statements() -> list[str]:
    return [
        "ALTER TABLE public.user_sessions ADD COLUMN auth_method text",
        "ALTER TABLE public.user_sessions ADD COLUMN credential_id text",
        "ALTER TABLE public.user_sessions ADD COLUMN security_generation bigint",
        # ``now()`` is transaction-stable.  The 0003/0004/0006 exchange function
        # writes invitations.consumed_at and user_sessions.issued_at in the same
        # transaction, so equality is the durable provenance link available in
        # the pre-0012 schema.  Anything else is unknown and must abort.
        """
DO $$
DECLARE
    v_unknown_count bigint;
BEGIN
    WITH session_provenance AS (
        SELECT
            tenant_id,
            principal_id,
            issued_at,
            count(*)::bigint AS session_count
          FROM public.user_sessions
         GROUP BY tenant_id, principal_id, issued_at
    ),
    invitation_provenance AS (
        SELECT
            tenant_id,
            consumed_by AS principal_id,
            consumed_at AS issued_at,
            count(*)::bigint AS invitation_count
          FROM public.invitations
         WHERE consumed_at IS NOT NULL
           AND consumed_by IS NOT NULL
           AND consumed_by = invitee_principal_id
         GROUP BY tenant_id, consumed_by, consumed_at
    )
    SELECT COALESCE(
               sum(s.session_count - COALESCE(i.invitation_count, 0)),
               0
           )::bigint
      INTO v_unknown_count
      FROM session_provenance AS s
      LEFT JOIN invitation_provenance AS i
        ON i.tenant_id = s.tenant_id
       AND i.principal_id = s.principal_id
       AND i.issued_at = s.issued_at
     WHERE s.session_count > COALESCE(i.invitation_count, 0);

    IF v_unknown_count <> 0 THEN
        RAISE EXCEPTION 'unverified pre-0012 user_sessions: % row(s)', v_unknown_count
            USING ERRCODE = 'check_violation';
    END IF;
END
$$
""",
        """
UPDATE public.user_sessions
   SET auth_method = 'invitation',
       security_generation = 1
""",
        "ALTER TABLE public.user_sessions ALTER COLUMN auth_method SET NOT NULL",
        "ALTER TABLE public.user_sessions ALTER COLUMN security_generation SET NOT NULL",
        """
ALTER TABLE public.user_sessions
    ADD CONSTRAINT user_sessions_security_generation_check
    CHECK (security_generation > 0)
""",
        """
ALTER TABLE public.user_sessions
    ADD CONSTRAINT user_sessions_auth_shape_check
    CHECK (
        (auth_method = 'invitation' AND credential_id IS NULL)
        OR
        (auth_method = 'password' AND credential_id IS NOT NULL)
    )
""",
        """
ALTER TABLE public.user_sessions
    ADD CONSTRAINT user_sessions_credential_tenant_principal_fk
    FOREIGN KEY (tenant_id, principal_id, credential_id)
    REFERENCES account_credentials (tenant_id, principal_id, credential_id)
""",
        """
CREATE OR REPLACE FUNCTION public.invalidate_credential_sessions()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
BEGIN
    IF NEW.security_generation <> OLD.security_generation
       OR (OLD.disabled_at IS NULL AND NEW.disabled_at IS NOT NULL)
    THEN
        UPDATE public.user_sessions
           SET revoked_at = COALESCE(revoked_at, now())
         WHERE credential_id = NEW.credential_id
           AND revoked_at IS NULL;
    END IF;
    RETURN NEW;
END;
$fn$;
""",
        """
CREATE TRIGGER account_credentials_invalidate_sessions
AFTER UPDATE OF security_generation, disabled_at ON public.account_credentials
FOR EACH ROW EXECUTE FUNCTION public.invalidate_credential_sessions()
""",
        "REVOKE ALL ON FUNCTION public.invalidate_credential_sessions() FROM PUBLIC",
        f"REVOKE ALL ON FUNCTION public.invalidate_credential_sessions() FROM {APP_ROLE}",
        f"REVOKE ALL ON FUNCTION public.invalidate_credential_sessions() FROM {WORKER_ROLE}",
    ]


def _platform_budget_functions() -> list[str]:
    """Durable platform-cap transitions used by the paid-provider worker."""
    row_type = "reservation_id text, billing_period date, provider text, price_version text, max_spend_micro bigint, state text, actual_spend_micro bigint"
    return [
        f"""
CREATE OR REPLACE FUNCTION public.reserve_platform_paid(
    p_reservation_id text,
    p_billing_period date,
    p_provider text,
    p_price_version text,
    p_max_spend_micro bigint
)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_cap bigint;
    v_enabled boolean;
    v_exposure numeric;
BEGIN
    IF p_reservation_id IS NULL OR char_length(p_reservation_id) = 0
       OR p_billing_period IS NULL
       OR p_billing_period <> date_trunc('month', now() AT TIME ZONE 'UTC')::date
       OR p_provider IS NULL OR char_length(p_provider) NOT BETWEEN 1 AND 100
       OR p_price_version IS NULL OR char_length(p_price_version) NOT BETWEEN 1 AND 100
       OR p_max_spend_micro IS NULL OR p_max_spend_micro <= 0
    THEN
        RAISE EXCEPTION 'invalid platform reservation arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT monthly_cap_micro, paid_dispatch_enabled
      INTO v_cap, v_enabled
      FROM public.platform_budget_config
     WHERE config_id = true
     FOR UPDATE;
    IF NOT FOUND OR NOT v_enabled THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.billing_period = p_billing_period
       AND r.provider = p_provider
       AND r.price_version = p_price_version
       AND r.max_spend_micro = p_max_spend_micro
       AND r.state IN ('held', 'in_flight');
    IF FOUND THEN
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.platform_paid_reservations AS r
         WHERE r.reservation_id = p_reservation_id
    ) THEN
        RAISE EXCEPTION 'platform reservation idempotency conflict'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COALESCE(SUM(
               CASE WHEN r.state = 'settled' AND r.billing_period = p_billing_period
                    THEN r.actual_spend_micro ELSE 0 END
           ), 0)
         + COALESCE(SUM(
               CASE WHEN r.state = 'held' AND r.billing_period = p_billing_period
                    THEN r.max_spend_micro ELSE 0 END
           ), 0)
         + COALESCE(SUM(
               CASE WHEN r.state = 'in_flight' THEN r.max_spend_micro ELSE 0 END
           ), 0)
      INTO v_exposure
      FROM public.platform_paid_reservations AS r;
    IF v_exposure + p_max_spend_micro > v_cap THEN
        RETURN;
    END IF;

    INSERT INTO public.platform_paid_reservations (
        reservation_id, billing_period, provider, price_version,
        max_spend_micro, state
    ) VALUES (
        p_reservation_id, p_billing_period, p_provider, p_price_version,
        p_max_spend_micro, 'held'
    )
    RETURNING platform_paid_reservations.reservation_id,
              platform_paid_reservations.billing_period,
              platform_paid_reservations.provider,
              platform_paid_reservations.price_version,
              platform_paid_reservations.max_spend_micro,
              platform_paid_reservations.state,
              platform_paid_reservations.actual_spend_micro
         INTO reservation_id, billing_period, provider, price_version,
              max_spend_micro, state, actual_spend_micro;
    RETURN NEXT;
END;
$fn$;
""",
        f"""
CREATE OR REPLACE FUNCTION public.mark_platform_paid_in_flight(p_reservation_id text)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'in_flight';
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'in_flight'
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'held'
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
END;
$fn$;
""",
        f"""
CREATE OR REPLACE FUNCTION public.release_platform_paid_held(p_reservation_id text)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'released';
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'released'
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'held'
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
END;
$fn$;
""",
        f"""
CREATE OR REPLACE FUNCTION public.release_platform_paid_failed(p_reservation_id text)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'released';
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'released'
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'in_flight'
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
END;
$fn$;
""",
        f"""
CREATE OR REPLACE FUNCTION public.settle_platform_paid(
    p_reservation_id text,
    p_actual_spend_micro bigint
)
RETURNS TABLE ({row_type})
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $fn$
BEGIN
    IF p_actual_spend_micro IS NULL OR p_actual_spend_micro < 0 THEN
        RAISE EXCEPTION 'platform usage is unknown or negative'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN QUERY
    SELECT r.reservation_id, r.billing_period, r.provider, r.price_version,
           r.max_spend_micro, r.state, r.actual_spend_micro
      FROM public.platform_paid_reservations AS r
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'settled'
       AND r.actual_spend_micro = p_actual_spend_micro;
    IF FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.platform_paid_reservations AS r
       SET state = 'settled', actual_spend_micro = p_actual_spend_micro,
           settled_at = now()
     WHERE r.reservation_id = p_reservation_id
       AND r.state = 'in_flight'
       AND p_actual_spend_micro <= r.max_spend_micro
    RETURNING r.reservation_id, r.billing_period, r.provider, r.price_version,
              r.max_spend_micro, r.state, r.actual_spend_micro;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'platform reservation cannot be settled'
            USING ERRCODE = 'check_violation';
    END IF;
END;
$fn$;
""",
    ]


def _exchange_invitation_function() -> str:
    """0011's hardened exchange function with explicit invitation provenance."""

    return f"""
CREATE OR REPLACE FUNCTION public.exchange_invitation(
    p_token_hash text,
    p_session_id text,
    p_session_expires_at timestamptz
)
RETURNS TABLE (session_id text, tenant_id text, principal_id text, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_tenant_id text;
    v_principal_id text;
BEGIN
    IF p_session_id IS NULL OR char_length(p_session_id) = 0
       OR p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
    THEN
        RAISE EXCEPTION 'invalid invitation session arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    UPDATE public.invitations
       SET consumed_at = now(),
           consumed_by = invitee_principal_id
     WHERE token_hash = p_token_hash
       AND consumed_at IS NULL
       AND public.invitations.expires_at > now()
    RETURNING public.invitations.tenant_id, public.invitations.invitee_principal_id
      INTO v_tenant_id, v_principal_id;

    IF v_principal_id IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO public.user_sessions
        (session_id, tenant_id, principal_id, issued_at, expires_at,
         auth_method, credential_id, security_generation)
    VALUES
        (p_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at,
         'invitation', NULL, 1);

    RETURN QUERY
        SELECT p_session_id, v_tenant_id, v_principal_id, p_session_expires_at;
END;
$fn$;
"""


def _rate_limit_function() -> str:
    """Retain persistent TTL cleanup and add a hard bucket-cap refusal."""
    return """
CREATE OR REPLACE FUNCTION public.register_auth_attempt(
    p_bucket text,
    p_window_seconds integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_window_start timestamptz;
    v_attempts integer;
BEGIN
    IF p_bucket IS NULL OR length(p_bucket) = 0 OR p_window_seconds <= 0 THEN
        RAISE EXCEPTION 'invalid rate limit arguments' USING ERRCODE = 'check_violation';
    END IF;
    v_window_start := to_timestamp(
        floor(extract(epoch from clock_timestamp()) / p_window_seconds) * p_window_seconds
    );
    DELETE FROM public.auth_attempt_counters
     WHERE window_start < now() - interval '24 hours';
    IF (SELECT count(*) FROM public.auth_attempt_counters) >= 10000
       AND NOT EXISTS (SELECT 1 FROM public.auth_attempt_counters WHERE bucket = p_bucket)
    THEN
        RETURN 2147483647;
    END IF;
    INSERT INTO public.auth_attempt_counters AS c (bucket, window_start, attempts, last_at)
    VALUES (p_bucket, v_window_start, 1, clock_timestamp())
    ON CONFLICT (bucket) DO UPDATE
       SET window_start = CASE WHEN c.window_start < EXCLUDED.window_start
                               THEN EXCLUDED.window_start ELSE c.window_start END,
           attempts = CASE WHEN c.window_start < EXCLUDED.window_start
                           THEN 1 ELSE c.attempts + 1 END,
           last_at = EXCLUDED.last_at
    RETURNING c.attempts INTO v_attempts;
    RETURN v_attempts;
END;
$fn$;
"""


def _rate_limit_function_legacy() -> str:
    """Restore the 0006 function when a disposable test DB downgrades."""
    return """
CREATE OR REPLACE FUNCTION public.register_auth_attempt(p_bucket text, p_window_seconds integer)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_window_start timestamptz;
    v_attempts integer;
BEGIN
    IF p_bucket IS NULL OR length(p_bucket) = 0 OR p_window_seconds <= 0 THEN
        RAISE EXCEPTION 'invalid rate limit arguments' USING ERRCODE = 'check_violation';
    END IF;
    v_window_start := to_timestamp(
        floor(extract(epoch from clock_timestamp()) / p_window_seconds) * p_window_seconds
    );
    DELETE FROM public.auth_attempt_counters
     WHERE window_start < now() - interval '24 hours';
    INSERT INTO public.auth_attempt_counters AS c (bucket, window_start, attempts, last_at)
    VALUES (p_bucket, v_window_start, 1, clock_timestamp())
    ON CONFLICT (bucket) DO UPDATE
       SET window_start = CASE WHEN c.window_start < EXCLUDED.window_start
                               THEN EXCLUDED.window_start ELSE c.window_start END,
           attempts = CASE WHEN c.window_start < EXCLUDED.window_start
                           THEN 1 ELSE c.attempts + 1 END,
           last_at = EXCLUDED.last_at
    RETURNING c.attempts INTO v_attempts;
    RETURN v_attempts;
END;
$fn$;
"""


def _exchange_invitation_0011_function() -> str:
    """Restore the schema-qualified, pg_catalog-search-path 0011 shape."""

    return f"""
CREATE OR REPLACE FUNCTION public.exchange_invitation(
    p_token_hash text,
    p_session_id text,
    p_session_expires_at timestamptz
)
RETURNS TABLE (session_id text, tenant_id text, principal_id text, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_tenant_id text;
    v_principal_id text;
BEGIN
    IF p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
    THEN
        RAISE EXCEPTION 'session expiry outside allowed window (0, %s days]',
            {MAX_SESSION_TTL_DAYS}
            USING ERRCODE = 'check_violation';
    END IF;

    UPDATE public.invitations
       SET consumed_at = now(),
           consumed_by = invitee_principal_id
     WHERE token_hash = p_token_hash
       AND consumed_at IS NULL
       AND public.invitations.expires_at > now()
    RETURNING public.invitations.tenant_id, public.invitations.invitee_principal_id
      INTO v_tenant_id, v_principal_id;

    IF v_principal_id IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO public.user_sessions
        (session_id, tenant_id, principal_id, issued_at, expires_at)
    VALUES
        (p_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at);

    RETURN QUERY
        SELECT p_session_id, v_tenant_id, v_principal_id, p_session_expires_at;
END;
$fn$;
"""


def _register_account_function() -> str:
    return f"""
CREATE OR REPLACE FUNCTION public.register_account(
    p_username text,
    p_username_normalized text,
    p_password_hash text,
    p_hash_version integer,
    p_session_expires_at timestamptz
)
RETURNS TABLE (
    credential_id text,
    session_id text,
    tenant_id text,
    principal_id text,
    expires_at timestamptz,
    security_generation bigint,
    default_project_id text,
    default_project_name text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_tenant_id text := 't_' || replace(gen_random_uuid()::text, '-', '');
    v_principal_id text := 'u_' || replace(gen_random_uuid()::text, '-', '');
    v_credential_id text := 'cred_' || replace(gen_random_uuid()::text, '-', '');
    v_session_id text := 'sess_' || replace(gen_random_uuid()::text, '-', '');
    v_project_id text := 'proj_' || replace(gen_random_uuid()::text, '-', '');
    v_event_id text := 'aud_' || replace(gen_random_uuid()::text, '-', '');
    v_security_generation bigint := 1;
    v_constraint_name text;
BEGIN
    IF p_username IS NULL OR char_length(p_username) NOT BETWEEN 1 AND 16
       OR p_username_normalized IS NULL
       OR char_length(p_username_normalized) NOT BETWEEN 1 AND 16
       OR p_password_hash IS NULL
       OR p_password_hash NOT LIKE '$argon2id$%'
       OR p_hash_version IS NULL OR p_hash_version <= 0
       OR p_session_expires_at IS NULL
       OR p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
    THEN
        RAISE EXCEPTION 'invalid account registration arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    INSERT INTO public.tenants (tenant_id, name)
    VALUES (v_tenant_id, p_username);

    INSERT INTO public.principals (principal_id, tenant_id, display_name)
    VALUES (v_principal_id, v_tenant_id, p_username);

    INSERT INTO public.projects (project_id, tenant_id, name)
    VALUES (v_project_id, v_tenant_id, '{DEFAULT_PROJECT_NAME}');

    INSERT INTO public.project_grants (tenant_id, principal_id, project_id)
    VALUES (v_tenant_id, v_principal_id, v_project_id);

    INSERT INTO public.account_credentials (
        credential_id, tenant_id, principal_id, username,
        username_normalized, password_hash, hash_version, security_generation
    ) VALUES (
        v_credential_id, v_tenant_id, v_principal_id, p_username,
        p_username_normalized COLLATE "C", p_password_hash, p_hash_version,
        v_security_generation
    );

    INSERT INTO public.user_sessions (
        session_id, tenant_id, principal_id, issued_at, expires_at,
        auth_method, credential_id, security_generation
    ) VALUES (
        v_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at,
        'password', v_credential_id, v_security_generation
    );

    PERFORM set_config('app.tenant_id', v_tenant_id, true);
    INSERT INTO public.auth_audit_outbox (
        event_id, event_type, payload, risk, tenant_id
    ) VALUES (
        v_event_id,
        'account_registered',
        jsonb_build_object(
            'principal_id', v_principal_id,
            'credential_id', v_credential_id,
            'default_project_id', v_project_id
        ),
        'high',
        v_tenant_id
    );

    RETURN QUERY SELECT
        v_credential_id,
        v_session_id,
        v_tenant_id,
        v_principal_id,
        p_session_expires_at,
        v_security_generation,
        v_project_id,
        '{DEFAULT_PROJECT_NAME}'::text;
EXCEPTION
    WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
        IF v_constraint_name = 'account_credentials_username_normalized_uq' THEN
            RAISE EXCEPTION 'username already registered'
                USING ERRCODE = 'unique_violation',
                      CONSTRAINT = 'account_credentials_username_normalized_uq';
        END IF;
        RAISE;
END;
$fn$;
"""


def _lookup_account_function() -> str:
    return """
CREATE OR REPLACE FUNCTION public.lookup_account_for_login(
    p_username_normalized text
)
RETURNS TABLE (
    credential_id text,
    tenant_id text,
    principal_id text,
    username text,
    password_hash text,
    hash_version integer,
    security_generation bigint,
    disabled_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog
AS $fn$
BEGIN
    IF p_username_normalized IS NULL
       OR char_length(p_username_normalized) NOT BETWEEN 1 AND 16
    THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        c.credential_id,
        c.tenant_id,
        c.principal_id,
        c.username,
        c.password_hash,
        c.hash_version,
        c.security_generation,
        c.disabled_at
      FROM public.account_credentials AS c
     WHERE c.username_normalized = p_username_normalized COLLATE "C";
END;
$fn$;
"""


def _complete_account_login_function() -> str:
    return f"""
CREATE OR REPLACE FUNCTION public.complete_account_login(
    p_credential_id text,
    p_expected_security_generation bigint,
    p_session_expires_at timestamptz,
    p_new_password_hash text DEFAULT NULL,
    p_new_hash_version integer DEFAULT NULL
)
RETURNS TABLE (
    session_id text,
    tenant_id text,
    principal_id text,
    expires_at timestamptz,
    security_generation bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_session_id text := 'sess_' || replace(gen_random_uuid()::text, '-', '');
    v_event_id text := 'aud_' || replace(gen_random_uuid()::text, '-', '');
    v_tenant_id text;
    v_principal_id text;
    v_security_generation bigint;
BEGIN
    IF p_credential_id IS NULL OR char_length(p_credential_id) = 0
       OR p_expected_security_generation IS NULL
       OR p_expected_security_generation <= 0
       OR p_session_expires_at IS NULL
       OR p_session_expires_at <= now()
       OR p_session_expires_at > now() + interval '{MAX_SESSION_TTL_DAYS} days'
       OR ((p_new_password_hash IS NULL) <> (p_new_hash_version IS NULL))
       OR (p_new_password_hash IS NOT NULL AND p_new_password_hash NOT LIKE '$argon2id$%')
       OR (p_new_hash_version IS NOT NULL AND p_new_hash_version <= 0)
    THEN
        RAISE EXCEPTION 'invalid login completion arguments'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT c.tenant_id, c.principal_id, c.security_generation
      INTO v_tenant_id, v_principal_id, v_security_generation
      FROM public.account_credentials AS c
     WHERE c.credential_id = p_credential_id
       AND c.security_generation = p_expected_security_generation
       AND c.disabled_at IS NULL
     FOR UPDATE;

    IF v_tenant_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE public.account_credentials AS c
       SET password_hash = COALESCE(p_new_password_hash, c.password_hash),
           hash_version = COALESCE(p_new_hash_version, c.hash_version),
           last_login_at = now(),
           updated_at = now()
     WHERE c.credential_id = p_credential_id;

    INSERT INTO public.user_sessions (
        session_id, tenant_id, principal_id, issued_at, expires_at,
        auth_method, credential_id, security_generation
    ) VALUES (
        v_session_id, v_tenant_id, v_principal_id, now(), p_session_expires_at,
        'password', p_credential_id, v_security_generation
    );

    PERFORM set_config('app.tenant_id', v_tenant_id, true);
    INSERT INTO public.auth_audit_outbox (
        event_id, event_type, payload, risk, tenant_id
    ) VALUES (
        v_event_id,
        'password_login_succeeded',
        jsonb_build_object(
            'principal_id', v_principal_id,
            'credential_id', p_credential_id
        ),
        'high',
        v_tenant_id
    );

    RETURN QUERY SELECT
        v_session_id,
        v_tenant_id,
        v_principal_id,
        p_session_expires_at,
        v_security_generation;
END;
$fn$;
"""


def _secure_function(signature: str, *, executor_role: str = APP_ROLE) -> list[str]:
    return [
        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
        f"REVOKE ALL ON FUNCTION {signature} FROM {APP_ROLE}",
        f"REVOKE ALL ON FUNCTION {signature} FROM {WORKER_ROLE}",
        f"GRANT EXECUTE ON FUNCTION {signature} TO {executor_role}",
    ]


def upgrade() -> None:
    _assert_roles_exist()

    for statement in _credential_statements():
        op.execute(statement)
    for statement in _session_upgrade_statements():
        op.execute(statement)
    for statement in _platform_budget_functions():
        op.execute(statement)
    for signature in (
        PLATFORM_RESERVE_SIGNATURE,
        PLATFORM_MARK_IN_FLIGHT_SIGNATURE,
        PLATFORM_RELEASE_HELD_SIGNATURE,
        PLATFORM_RELEASE_FAILED_SIGNATURE,
        PLATFORM_SETTLE_SIGNATURE,
    ):
        for statement in _secure_function(signature, executor_role=WORKER_ROLE):
            op.execute(statement)

    op.execute(_exchange_invitation_function())
    op.execute(_rate_limit_function())
    for statement in _secure_function(EXCHANGE_INVITATION_SIGNATURE):
        op.execute(statement)

    op.execute(_register_account_function())
    op.execute(_lookup_account_function())
    op.execute(_complete_account_login_function())
    for signature in (
        REGISTER_ACCOUNT_SIGNATURE,
        LOOKUP_ACCOUNT_SIGNATURE,
        COMPLETE_LOGIN_SIGNATURE,
    ):
        for statement in _secure_function(signature):
            op.execute(statement)


def downgrade() -> None:
    # Test databases only: 0011 cannot represent password-session provenance.
    op.execute("DELETE FROM public.user_sessions WHERE auth_method = 'password'")

    for signature in (
        COMPLETE_LOGIN_SIGNATURE,
        LOOKUP_ACCOUNT_SIGNATURE,
        REGISTER_ACCOUNT_SIGNATURE,
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    op.execute(_exchange_invitation_0011_function())
    op.execute(_rate_limit_function_legacy())
    for statement in _secure_function(EXCHANGE_INVITATION_SIGNATURE):
        op.execute(statement)

    op.execute(
        "ALTER TABLE public.user_sessions"
        " DROP CONSTRAINT IF EXISTS user_sessions_credential_tenant_principal_fk"
    )
    op.execute(
        "ALTER TABLE public.user_sessions"
        " DROP CONSTRAINT IF EXISTS user_sessions_auth_shape_check"
    )
    op.execute(
        "ALTER TABLE public.user_sessions"
        " DROP CONSTRAINT IF EXISTS user_sessions_security_generation_check"
    )
    op.execute("ALTER TABLE public.user_sessions DROP COLUMN IF EXISTS security_generation")
    op.execute("ALTER TABLE public.user_sessions DROP COLUMN IF EXISTS credential_id")
    op.execute("ALTER TABLE public.user_sessions DROP COLUMN IF EXISTS auth_method")
    op.execute("DROP POLICY IF EXISTS account_credentials_isolation ON public.account_credentials")
    op.execute("DROP TABLE IF EXISTS public.account_credentials")
    op.execute("DROP FUNCTION IF EXISTS public.invalidate_credential_sessions()")
    for signature in (
        PLATFORM_SETTLE_SIGNATURE,
        PLATFORM_RELEASE_FAILED_SIGNATURE,
        PLATFORM_RELEASE_HELD_SIGNATURE,
        PLATFORM_MARK_IN_FLIGHT_SIGNATURE,
        PLATFORM_RESERVE_SIGNATURE,
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP TABLE IF EXISTS public.platform_paid_reservations")
    op.execute("DROP TABLE IF EXISTS public.platform_budget_config")
