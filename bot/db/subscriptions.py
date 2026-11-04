"""
bot/db/subscriptions.py
Database operations for the `subscriptions` table (payment audit trail).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bot.db.supabase_client import get_client


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def create_subscription_record(
    telegram_id: int,
    plan_name: str,
    amount: float,
    payment_method: str,
    razorpay_order_id: Optional[str] = None,
) -> dict:
    """
    Insert a new subscription record with status='pending'.
    Returns the created row.
    """
    db = get_client()
    row = {
        "telegram_id": telegram_id,
        "plan_name": plan_name,
        "amount": amount,
        "payment_method": payment_method,
        "payment_status": "pending",
        "created_at": _now().isoformat(),
    }
    if razorpay_order_id:
        row["razorpay_order_id"] = razorpay_order_id
    res = db.table("subscriptions").insert(row).execute()
    return res.data[0] if res.data else {}


def activate_subscription(
    subscription_id: int,
    expires_at: datetime,
) -> None:
    """Mark a subscription as paid/activated."""
    db = get_client()
    db.table("subscriptions").update({
        "payment_status": "paid",
        "activated_at": _now().isoformat(),
        "expires_at": expires_at.isoformat(),
    }).eq("id", subscription_id).execute()


def get_subscriptions_report() -> dict:
    """
    Returns three lists for the /admin_subscriptions report:
      - active: subscriptions where expires_at > now and payment_status = 'paid'
      - expiring_soon: active but expires_at within 3 days
      - expired: payment_status = 'paid' but expires_at <= now
    """
    db = get_client()
    now_iso = _now().isoformat()

    # All paid subscriptions
    res = (
        db.table("subscriptions")
        .select("*")
        .eq("payment_status", "paid")
        .order("expires_at", desc=False)
        .execute()
    )
    rows = res.data or []

    from datetime import timedelta
    soon_threshold = (_now() + timedelta(days=3)).isoformat()

    active = []
    expiring_soon = []
    expired = []

    for row in rows:
        exp_str = row.get("expires_at")
        if not exp_str:
            expired.append(row)
            continue
        if exp_str <= now_iso:
            expired.append(row)
        elif exp_str <= soon_threshold:
            expiring_soon.append(row)
        else:
            active.append(row)

    return {
        "active": active,
        "expiring_soon": expiring_soon,
        "expired": expired,
    }


def get_pending_subscription_by_user(telegram_id: int) -> Optional[dict]:
    """Fetch the most recent subscription record for a user (any status)."""
    db = get_client()
    res = (
        db.table("subscriptions")
        .select("*")
        .eq("telegram_id", telegram_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def update_razorpay_link_id(subscription_id: int, payment_link_id: str) -> None:
    """Attach a Razorpay payment link ID to an existing subscription record."""
    db = get_client()
    db.table("subscriptions").update({
        "razorpay_order_id": payment_link_id,
    }).eq("id", subscription_id).execute()


# ─────────────────────────────────────────────────────────────────────────────
# Webhook deduplication (processed_webhooks table)
# ─────────────────────────────────────────────────────────────────────────────

def is_webhook_processed(payment_id: str) -> bool:
    """
    Return True if this Razorpay payment_id has already been processed.
    Used to prevent double-activation on Razorpay retries.
    """
    db = get_client()
    res = (
        db.table("processed_webhooks")
        .select("id")
        .eq("payment_id", payment_id)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def mark_webhook_processed(
    payment_id: str,
    telegram_id: int,
    plan_key: str,
    amount_inr: float,
) -> None:
    """
    Insert a row into processed_webhooks before activation.
    Idempotent — if the row already exists (race condition), the DB unique
    constraint will raise and the caller can treat it as 'already processed'.
    """
    db = get_client()
    db.table("processed_webhooks").insert({
        "payment_id": payment_id,
        "telegram_id": telegram_id,
        "plan_key": plan_key,
        "amount_inr": amount_inr,
        "processed_at": _now().isoformat(),
    }).execute()


def create_webhook_subscription(
    telegram_id: int,
    plan_name: str,
    amount: float,
    payment_id: str,
    order_id: str,
    expires_at: datetime,
) -> dict:
    """
    Insert a fully-confirmed subscription record sourced directly from a webhook.
    Used when the user has no pending record (e.g. paid without going through /pro).
    """
    db = get_client()
    row = {
        "telegram_id": telegram_id,
        "plan_name": plan_name,
        "amount": amount,
        "payment_method": "inr",
        "payment_status": "paid",
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "created_at": _now().isoformat(),
        "activated_at": _now().isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    res = db.table("subscriptions").insert(row).execute()
    return res.data[0] if res.data else {}




def activate_razorpay_payment(payment_id: str, telegram_id: int, plan_key: str,
                              amount_paise: int, order_id: str = "") -> dict:
    """Commit billing and deduplication together. Requires migration_reliability.sql."""
    response = get_client().rpc("activate_razorpay_payment", {
        "p_payment_id": payment_id, "p_telegram_id": telegram_id,
        "p_plan_key": plan_key, "p_amount_paise": amount_paise, "p_order_id": order_id,
    }).execute()
    if not response.data:
        raise RuntimeError("Payment activation returned no result")
    return response.data[0]
