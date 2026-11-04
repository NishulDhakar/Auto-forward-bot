"""
bot/utils/filters.py
Text filter application logic — applies admin-defined find/replace rules
to message text or media captions before forwarding.
"""

from __future__ import annotations

import re
from bot.db.filters import get_filters


def apply_filters(admin_id: int, text: str | None) -> str | None:
    """
    Apply all text replacement filters for `admin_id` to `text`.
    Returns the modified text, or None if original was None.
    """
    if text is None:
        return None

    rules = get_filters(admin_id)

    # Process blocking rules first
    for rule in rules:
        if rule["find_text"] == "<BLOCK>":
            if rule["replace_text"].lower() in text.lower():
                return None  # Signal to block this message completely

    # Process regex wildcard rules
    for rule in rules:
        find = rule["find_text"]
        replace = rule["replace_text"]
        if find == "<ALL_LINKS>":
            # Match standard URLs, www, and t.me links
            text = re.sub(r'https?://\S+|www\.\S+|t\.me/\S+', lambda _: replace, text)
        elif find == "<ALL_USERNAMES>":
            # Match Telegram usernames starting with @
            text = re.sub(r'@[a-zA-Z0-9_]+', lambda _: replace, text)

    # Process standard exact match rules
    for rule in rules:
        find = rule["find_text"]
        if find not in ("<ALL_LINKS>", "<ALL_USERNAMES>", "<BLOCK>", "<BLOCK_APK>"):
            text = text.replace(find, rule["replace_text"])

    return text

def should_block_apk(admin_id: int) -> bool:
    """Check if the admin has enabled the APK block filter."""
    rules = get_filters(admin_id)
    return any(r["find_text"] == "<BLOCK_APK>" for r in rules)
