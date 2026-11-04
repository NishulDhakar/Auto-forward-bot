"""
bot/db/referrals.py
Database operations for the Refer & Earn system.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.db.supabase_client import get_client

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Referral settings
# ─────────────────────────────────────────────────────────────────────────────

def get_referral_payout_amount() -> float:
    """Return the configured ₹ amount credited per Pro referral."""
    try:
        db = get_client()
        res = db.table("referral_settings").select("amount").eq("id", 1).single().execute()
        return float(res.data.get("amount", 22.0))
    except Exception:
        return 22.0


def get_min_withdrawal() -> float:
    """Return the minimum withdrawal amount."""
    try:
        db = get_client()
        res = db.table("referral_settings").select("min_withdrawal").eq("id", 1).single().execute()
        return float(res.data.get("min_withdrawal", 100.0))
    except Exception:
        return 100.0


def set_referral_payout_amount(amount: float) -> None:
    """Update the per-referral payout amount."""
    db = get_client()
    db.table("referral_settings").upsert({"id": 1, "amount": amount}).execute()


# ─────────────────────────────────────────────────────────────────────────────
# Recording referrals
# ─────────────────────────────────────────────────────────────────────────────

def record_referral(referrer_id: int, referred_id: int) -> bool:
    """
    Save a new referral relationship. Returns True if saved, False if already exists.
    The referred_id has a UNIQUE constraint so duplicate joins are safely ignored.
    """
    if referrer_id == referred_id or referrer_id <= 0 or referred_id <= 0:
        return False
    try:
        db = get_client()
        db.table("referrals").insert({
            "referrer_id": referrer_id,
            "referred_id": referred_id,
            "joined_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        logger.info("Referral recorded: referrer=%s referred=%s", referrer_id, referred_id)
        return True
    except Exception as e:
        # Unique constraint violation = user already referred, ignore silently
        logger.debug("record_referral skipped (already exists or error): %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Crediting earnings when referred user buys Pro
# ─────────────────────────────────────────────────────────────────────────────

def credit_referral(referred_id: int) -> float | None:
    """
    Called when `referred_id` buys a Pro plan.
    Finds their referrer, credits the payout amount, and returns the amount credited.
    Returns None if no referral record exists for this user.
    """
    try:
        db = get_client()
        # Find the referral record for this user
        res = (
            db.table("referrals")
            .select("id, referrer_id, is_pro")
            .eq("referred_id", referred_id)
            .single()
            .execute()
        )
        if not res.data:
            return None

        record = res.data
        if record.get("is_pro"):
            # Already credited — don't double-credit
            logger.debug("Referral already credited for referred_id=%s", referred_id)
            return None

        amount = get_referral_payout_amount()
        referrer_id = record["referrer_id"]
        record_id = record["id"]

        # Mark the referral as converted to Pro and set earned amount
        db.table("referrals").update({
            "is_pro": True,
            "earned_amount": amount,
            "pro_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", record_id).execute()

        logger.info(
            "Referral credited: referrer=%s earned ₹%.2f (referred=%s)",
            referrer_id, amount, referred_id,
        )
        return amount

    except Exception as e:
        logger.error("credit_referral error for referred_id=%s: %s", referred_id, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Stats queries
# ─────────────────────────────────────────────────────────────────────────────

def get_referral_stats(user_id: int) -> dict:
    """
    Return referral stats for a given user:
    {
        total_referrals: int,
        pro_referrals: int,
        total_earned: float,
    }
    """
    try:
        db = get_client()
        res = (
            db.table("referrals")
            .select("is_pro, earned_amount")
            .eq("referrer_id", user_id)
            .execute()
        )
        rows = res.data or []
        total = len(rows)
        pro = sum(1 for r in rows if r.get("is_pro"))
        earned = sum(float(r.get("earned_amount", 0)) for r in rows)
        return {
            "total_referrals": total,
            "pro_referrals": pro,
            "total_earned": earned,
        }
    except Exception as e:
        logger.error("get_referral_stats error for user=%s: %s", user_id, e)
        return {"total_referrals": 0, "pro_referrals": 0, "total_earned": 0.0}


def get_top_referrers(limit: int = 10) -> list[dict]:
    """Return top referrers by Pro conversions (for super admin stats)."""
    try:
        db = get_client()
        res = (
            db.table("referrals")
            .select("referrer_id, earned_amount")
            .eq("is_pro", True)
            .execute()
        )
        rows = res.data or []
        # Aggregate by referrer
        agg: dict[int, dict] = {}
        for r in rows:
            rid = r["referrer_id"]
            if rid not in agg:
                agg[rid] = {"referrer_id": rid, "pro_count": 0, "total_earned": 0.0}
            agg[rid]["pro_count"] += 1
            agg[rid]["total_earned"] += float(r.get("earned_amount", 0))
        sorted_agg = sorted(agg.values(), key=lambda x: x["pro_count"], reverse=True)
        return sorted_agg[:limit]
    except Exception as e:
        logger.error("get_top_referrers error: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Super admin: manual balance adjustment
# ─────────────────────────────────────────────────────────────────────────────

def adjust_user_earnings(referrer_id: int, new_amount: float) -> bool:
    """
    Super admin tool: set the total earned amount for a specific referrer.
    Useful for marking payouts (set to 0 after paying them).
    Updates all un-credited referral rows for that user proportionally, or
    inserts a synthetic adjustment record.
    This is a simple approach: insert a special adjustment row.
    """
    try:
        db = get_client()
        # Find all pro referrals for this user and sum current earnings
        res = (
            db.table("referrals")
            .select("id, earned_amount")
            .eq("referrer_id", referrer_id)
            .eq("is_pro", True)
            .execute()
        )
        rows = res.data or []
        current_total = sum(float(r.get("earned_amount", 0)) for r in rows)
        diff = new_amount - current_total

        if not rows and new_amount > 0:
            # Insert synthetic adjustment record if no organic rows exist
            db.table("referrals").insert({
                "referrer_id": referrer_id,
                "referred_id": 0,   # 0 = synthetic adjustment
                "is_pro": True,
                "earned_amount": new_amount,
                "pro_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        elif rows:
            # Adjust the first row by the difference
            first_id = rows[0]["id"]
            first_amount = float(rows[0].get("earned_amount", 0))
            db.table("referrals").update({
                "earned_amount": max(0, first_amount + diff),
            }).eq("id", first_id).execute()

        logger.info(
            "Admin adjusted referral balance for user=%s: %.2f → %.2f",
            referrer_id, current_total, new_amount,
        )
        return True
    except Exception as e:
        logger.error("adjust_user_earnings error for user=%s: %s", referrer_id, e)
        return False
