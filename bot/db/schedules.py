"""
bot/db/schedules.py
Database operations for the `scheduled_messages` table.
"""

from __future__ import annotations


from bot.db.supabase_client import get_client


def add_schedule(
    admin_id: int,
    content: str,
    post_time: str,   # "HH:MM"
    frequency: str,   # "once" | "daily"
) -> dict:
    """Insert a new scheduled message and return the created record."""
    db = get_client()
    res = db.table("scheduled_messages").insert({
        "admin_id": admin_id,
        "content": content,
        "post_time": post_time,
        "frequency": frequency,
        "is_active": True,
    }).execute()
    return res.data[0] if res.data else {}


def get_schedules(admin_id: int, active_only: bool = True) -> list[dict]:
    """Return scheduled messages for an admin."""
    db = get_client()
    query = db.table("scheduled_messages").select("*").eq("admin_id", admin_id)
    if active_only:
        query = query.eq("is_active", True)
    res = query.order("post_time").execute()
    return res.data or []


def deactivate_schedule(schedule_id: str, admin_id: int | None = None) -> bool:
    """Mark a scheduled message as inactive. Returns True if updated."""
    db = get_client()
    query = db.table("scheduled_messages").select("id").eq("id", schedule_id)
    if admin_id is not None:
        query = query.eq("admin_id", admin_id)
    res = query.execute()
    if not res.data:
        return False
    query = db.table("scheduled_messages").update({"is_active": False}).eq("id", schedule_id)
    if admin_id is not None:
        query = query.eq("admin_id", admin_id)
    query.execute()
    return True


def get_all_active_schedules() -> list[dict]:
    """Return all active schedules across all admins (used on bot startup to reload APScheduler jobs)."""
    db = get_client()
    res = db.table("scheduled_messages").select("*").eq("is_active", True).execute()
    return res.data or []
