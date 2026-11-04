-- Apply AFTER supabase_schema.sql, migration_payment_system.sql,
-- migration_webhook_support.sql and migration_work_stop.sql.
-- Use a server-side Supabase service_role key for the bot.
BEGIN;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS session_string TEXT;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS referred_by BIGINT;
CREATE INDEX IF NOT EXISTS idx_source_channels_channel_id ON public.source_channels(channel_id);

-- Missing from the original base schema despite being used by /refer.
CREATE TABLE IF NOT EXISTS public.referral_settings (
    id INTEGER PRIMARY KEY, amount NUMERIC(10,2) NOT NULL DEFAULT 22,
    min_withdrawal NUMERIC(10,2) NOT NULL DEFAULT 100
);
INSERT INTO public.referral_settings(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS public.referrals (
    id BIGSERIAL PRIMARY KEY, referrer_id BIGINT NOT NULL,
    referred_id BIGINT NOT NULL UNIQUE, joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_pro BOOLEAN NOT NULL DEFAULT false, earned_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
    pro_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer_id ON public.referrals(referrer_id);

-- A single Postgres transaction owns deduplication, entitlement and the ledger.
-- Any exception rolls everything back, allowing a provider retry to recover.
CREATE OR REPLACE FUNCTION public.activate_razorpay_payment(
    p_payment_id TEXT, p_telegram_id BIGINT, p_plan_key TEXT,
    p_amount_paise BIGINT, p_order_id TEXT DEFAULT ''
) RETURNS TABLE (expires_at TIMESTAMPTZ, activated BOOLEAN)
LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    v_days INTEGER;
    v_expected BIGINT;
    v_inserted INTEGER;
    v_expiry TIMESTAMPTZ;
    v_previous TIMESTAMPTZ;
    v_now TIMESTAMPTZ := now();
BEGIN
    CASE p_plan_key
        WHEN '1_month' THEN v_days := 30; v_expected := 8800;
        WHEN '3_months' THEN v_days := 90; v_expected := 25000;
        WHEN '6_months' THEN v_days := 180; v_expected := 50000;
        ELSE RAISE EXCEPTION 'Unknown payment plan';
    END CASE;
    IF p_payment_id IS NULL OR p_payment_id NOT LIKE 'pay\_%' ESCAPE '\'
       OR p_telegram_id <= 0 OR p_amount_paise <> v_expected THEN
        RAISE EXCEPTION 'Invalid payment identity or amount';
    END IF;
    INSERT INTO processed_webhooks(payment_id, telegram_id, plan_key, amount_inr)
    VALUES(p_payment_id, p_telegram_id, p_plan_key, p_amount_paise / 100.0)
    ON CONFLICT(payment_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    IF v_inserted = 0 THEN
        SELECT s.expires_at INTO v_expiry FROM subscriptions s
        WHERE s.razorpay_payment_id = p_payment_id AND s.telegram_id = p_telegram_id
          AND s.payment_status = 'paid' ORDER BY s.id DESC LIMIT 1;
        IF v_expiry IS NULL THEN
            RAISE EXCEPTION 'Existing payment marker needs manual reconciliation';
        END IF;
        RETURN QUERY SELECT v_expiry, false;
        RETURN;
    END IF;

    INSERT INTO users(user_id) VALUES(p_telegram_id) ON CONFLICT(user_id) DO NOTHING;
    SELECT subscription_end INTO v_previous FROM users WHERE user_id = p_telegram_id FOR UPDATE;
    v_expiry := greatest(v_now, coalesce(v_previous, v_now)) + make_interval(days => v_days);
    UPDATE users SET role = CASE WHEN role = 'superadmin' THEN role ELSE 'admin' END,
        subscription_plan = p_plan_key, subscription_start = v_now,
        subscription_end = v_expiry, payment_method = 'inr',
        payment_amount = p_amount_paise / 100.0, payment_status = 'paid'
    WHERE user_id = p_telegram_id;
    INSERT INTO transactions(admin_id, amount, duration_days, promoted_at, expires_at)
    VALUES(p_telegram_id, p_amount_paise / 100.0, v_days, v_now, v_expiry);
    -- Never activate an arbitrary "latest" pending plan belonging to the user.
    INSERT INTO subscriptions(telegram_id, plan_name, amount, payment_method,
        payment_status, razorpay_order_id, razorpay_payment_id, activated_at, expires_at)
    VALUES(p_telegram_id, p_plan_key, p_amount_paise / 100.0, 'inr',
        'paid', p_order_id, p_payment_id, v_now, v_expiry);
    UPDATE subscriptions SET payment_status = 'superseded'
    WHERE telegram_id = p_telegram_id AND payment_status = 'pending'
      AND razorpay_order_id = p_order_id AND p_order_id <> '';
    UPDATE referrals SET is_pro = true, pro_at = v_now,
        earned_amount = (SELECT amount FROM referral_settings WHERE id = 1)
    WHERE referred_id = p_telegram_id AND referrer_id <> p_telegram_id AND NOT is_pro;
    RETURN QUERY SELECT v_expiry, true;
END;
$$;
REVOKE ALL ON FUNCTION public.activate_razorpay_payment(TEXT, BIGINT, TEXT, BIGINT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.activate_razorpay_payment(TEXT, BIGINT, TEXT, BIGINT, TEXT)
    TO service_role;

-- These tables contain Telegram login sessions and privileged billing data.
-- The bot uses service_role and performs tenant checks in server-side handlers.
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.target_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.filters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scheduled_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.processed_webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bot_content ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referrals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referral_settings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.users, public.source_channels, public.target_channels,
    public.filters, public.scheduled_messages, public.transactions,
    public.subscriptions, public.processed_webhooks, public.bot_content,
    public.referrals, public.referral_settings
    FROM anon, authenticated;
COMMIT;
