"""
bot/db/filters.py
Database operations for the `filters` table.
"""

from __future__ import annotations


from bot.db.supabase_client import get_client


def add_filter(admin_id: int, find_text: str, replace_text: str) -> dict:
    """Insert a new text filter rule for an admin and return the created record."""
    db = get_client()
    res = db.table("filters").insert({
        "admin_id": admin_id,
        "find_text": find_text,
        "replace_text": replace_text,
    }).execute()
    return res.data[0] if res.data else {}


def get_filters(admin_id: int) -> list[dict]:
    """Return all filter rules for an admin."""
    db = get_client()
    res = db.table("filters").select("*").eq("admin_id", admin_id).execute()
    return res.data or []


def remove_filter(filter_id: str, admin_id: int) -> bool:
    """Delete a filter by its UUID. Returns True if deleted."""
    db = get_client()
    res = db.table("filters").select("id").eq("id", filter_id).eq("admin_id", admin_id).execute()
    if not res.data:
        return False
    db.table("filters").delete().eq("id", filter_id).eq("admin_id", admin_id).execute()
    return True


def remove_all_filters(admin_id: int) -> None:
    """Remove all filters for an admin (e.g., on account demotion)."""
    db = get_client()
    db.table("filters").delete().eq("admin_id", admin_id).execute()
