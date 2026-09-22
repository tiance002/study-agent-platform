"""Allow paid dispatch without a platform-wide monthly monetary cap.

``NULL`` in ``platform_budget_config.monthly_cap_micro`` means that the
platform still records reservations and actual provider usage, but does not
reject a new dispatch because of a monthly amount.  Request token limits,
provider deadlines and the durable no-hidden-retry state machine remain
active.
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


PLATFORM_RESERVE_SIGNATURE = "public.reserve_platform_paid(text, date, text, text, bigint)"


def _reserve_function() -> str:
    row_type = (
        "reservation_id text, billing_period date, provider text, "
        "price_version text, max_spend_micro bigint, state text, "
        "actual_spend_micro bigint"
    )
    return f"""
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

    IF v_cap IS NOT NULL THEN
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
"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.platform_budget_config "
        "ALTER COLUMN monthly_cap_micro DROP NOT NULL"
    )
    op.execute(_reserve_function())


def downgrade() -> None:
    op.execute(
        """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.platform_budget_config
         WHERE monthly_cap_micro IS NULL
    ) THEN
        RAISE EXCEPTION 'cannot downgrade while platform monthly cap is unlimited';
    END IF;
END
$$
"""
    )
    op.execute(
        "ALTER TABLE public.platform_budget_config "
        "ALTER COLUMN monthly_cap_micro SET NOT NULL"
    )
    # The 0012 function is restored by the normal downgrade chain.
