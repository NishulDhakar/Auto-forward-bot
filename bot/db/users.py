"""
bot/db/users.py
All database operations for the `users` table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.config import SUPER_ADMIN_ID, TRIAL_DAYS
from bot.db.supabase_client import get_client


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_user(user_id: int, username: Optional[str] = None) -> dict:
    """
    Fetch the user record, creating it with a trial if it doesn't exist.
    The Super Admin is automatically assigned the 'superadmin' role.
    """
    db = get_client()
    res = db.table("users").select("*").eq("user_id", user_id).execute()

    if res.data:
        # Update username in case it changed
        if username and res.data[0].get("username") != username:
            db.table("users").update({"username": username}).eq("user_id", user_id).execute()
            res.data[0]["username"] = username
        return res.data[0]

    # Determine initial role
    role = "superadmin" if user_id == SUPER_ADMIN_ID else "user"

    new_user = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "trial_start": _now().isoformat(),
        "subscription_end": None,
    }
    db.table("users").insert(new_user).execute()
    return new_user


def get_user(user_id: int) -> Optional[dict]:
    """Fetch a single user record by ID."""
    db = get_client()
    res = db.table("users").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


def set_role(user_id: int, role: str) -> None:
    """Update the role of a user. role ∈ {'superadmin', 'admin', 'user'}"""
    db = get_client()
    db.table("users").update({"role": role}).eq("user_id", user_id).execute()


def set_subscription_end(user_id: int, end_dt: datetime) -> None:
    """Set the subscription expiry datetime (UTC-aware) for a user."""
    db = get_client()
    db.table("users").update({"subscription_end": end_dt.isoformat()}).eq("user_id", user_id).execute()


def get_all_admins() -> list[dict]:
    """Return all users with role='admin', ordered by subscription_end."""
    db = get_client()
    res = db.table("users").select("*").eq("role", "admin").order("subscription_end").execute()
    return res.data or []


def get_expired_admins() -> list[dict]:
    """Return admins whose subscription_end has passed (or is null but role is admin)."""
    db = get_client()
    now_iso = _now().isoformat()
    # Get admins where subscription_end < now
    res = (
        db.table("users")
        .select("*")
        .eq("role", "admin")
        .lt("subscription_end", now_iso)
        .execute()
    )
    return res.data or []


def get_trial_days_remaining(trial_start_str: str) -> int:
    """Return how many trial days are left (0 if expired)."""
    trial_start = datetime.fromisoformat(trial_start_str)
    if trial_start.tzinfo is None:
        trial_start = trial_start.replace(tzinfo=timezone.utc)
    expiry = trial_start + timedelta(days=TRIAL_DAYS)
    remaining = (expiry - _now()).days
    return max(remaining, 0)


def is_trial_active(trial_start_str: str) -> bool:
    """Return True if the 3-day trial hasn't expired yet."""
    return get_trial_days_remaining(trial_start_str) > 0


def get_all_users_count() -> int:
    """Return total number of users in the DB."""
    db = get_client()
    res = db.table("users").select("user_id", count="exact").execute()
    return res.count or 0

def get_all_users() -> list[dict]:
    """Return all users (user_id only) for broadcasting."""
    db = get_client()
    res = db.table("users").select("user_id").execute()
    return res.data or []


def save_referred_by(user_id: int, referrer_id: int) -> None:
    """Record which user referred this user (one-time, on first join)."""
    try:
        db = get_client()
        db.table("users").update({"referred_by": referrer_id}).eq("user_id", user_id).execute()
    except Exception:
        pass


def set_subscription(
    user_id: int,
    plan: str,
    start_dt: datetime,
    end_dt: datetime,
    payment_method: str,
    amount: float,
    payment_status: str = "paid",
) -> None:
    """
    Atomically promote user to admin and record full subscription details.
    plan: '1_month' | '3_months' | '6_months' | 'manual'
    payment_method: 'inr' | 'usdt' | 'manual'
    """
    db = get_client()
    db.table("users").update({
        "role": "admin",
        "subscription_plan": plan,
        "subscription_start": start_dt.isoformat(),
        "subscription_end": end_dt.isoformat(),
        "payment_method": payment_method,
        "payment_amount": amount,
        "payment_status": payment_status,
    }).eq("user_id", user_id).execute()


def revoke_subscription(user_id: int) -> None:
    """
    Demote user to 'user' role and clear all subscription/payment fields.
    """
    db = get_client()
    db.table("users").update({
        "role": "user",
        "subscription_plan": None,
        "subscription_start": None,
        "subscription_end": None,
        "payment_method": None,
        "payment_amount": None,
        "payment_status": None,
    }).eq("user_id", user_id).execute()


def get_active_subscriptions() -> list[dict]:
    """Return all admins with an active (non-expired) subscription."""
    db = get_client()
    now_iso = _now().isoformat()
    res = (
        db.table("users")
        .select("*")
        .eq("role", "admin")
        .gt("subscription_end", now_iso)
        .order("subscription_end")
        .execute()
    )
    return res.data or []


def get_expiring_soon_admins(days: int = 3) -> list[dict]:
    """Return admins whose subscription expires within `days` days."""
    from datetime import timedelta
    db = get_client()
    now_iso = _now().isoformat()
    soon_iso = (_now() + timedelta(days=days)).isoformat()
    res = (
        db.table("users")
        .select("*")
        .eq("role", "admin")
        .gt("subscription_end", now_iso)
        .lte("subscription_end", soon_iso)
        .order("subscription_end")
        .execute()
    )
    return res.data or []
