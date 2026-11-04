"""
bot/utils/roles.py
Decorator-based role guards and helper predicates for Telegram handlers.

Usage:
    @require_superadmin
    async def my_handler(update, context): ...

    @require_admin
    async def my_admin_handler(update, context): ...
"""

from __future__ import annotations

import asyncio

import functools
import logging
from datetime import datetime, timezone
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import SUPER_ADMIN_ID
from bot.db import users as users_db

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_user_id(update: Update) -> int | None:
    if update.effective_user:
        return update.effective_user.id
    return None


def _parse_dt(dt_str: str | None) -> datetime | None:
    if dt_str is None:
        return None
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─────────────────────────────────────────────────────────────────────────────
# Role predicates
# ─────────────────────────────────────────────────────────────────────────────

def is_superadmin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID


def is_admin_active(user: dict) -> bool:
    """
    Returns True if the user has role='admin' and subscription_end is in the future.
    Super Admin always passes.
    """
    if user["role"] == "superadmin":
        return True
    if user["role"] != "admin":
        return False
    sub_end = _parse_dt(user.get("subscription_end"))
    if sub_end is None:
        return False
    return sub_end > datetime.now(timezone.utc)


def is_trial_valid(user: dict) -> bool:
    trial_start = user.get("trial_start")
    if not trial_start:
        return False
    return users_db.is_trial_active(trial_start)


# ─────────────────────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────────────────────

def require_superadmin(func: Callable) -> Callable:
    """Only the Super Admin can invoke this handler."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = _get_user_id(update)
        if user_id != SUPER_ADMIN_ID:
            if update.message:
                await update.message.reply_text("⛔ This command is restricted to the Super Admin.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Super Admin only.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)

    return wrapper


def require_admin(func: Callable) -> Callable:
    """
    The user must be an *active* admin (or superadmin) to invoke this handler.
    On failure the user is told their subscription has expired or they lack permissions.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = _get_user_id(update)
        if user_id is None:
            return
        if update.effective_chat and update.effective_chat.type != "private":
            if update.effective_message:
                await update.effective_message.reply_text("Please use this command in a private chat with the bot.")
            return ConversationHandler.END

        if user_id == SUPER_ADMIN_ID:
            return await func(update, context, *args, **kwargs)

        user = (await asyncio.to_thread(users_db.get_user, user_id))
        if user is None:
            if update.message:
                await update.message.reply_text("❌ You are not registered. Send /start first.")
            return ConversationHandler.END

        if not is_admin_active(user):
            msg = (
                "⭐ *Premium Plan Required*\n\n"
                "This feature is restricted to active subscribers.\n\n"
                "Please use /pro to view subscription plans and purchase premium to unlock this feature!"
            )
            if update.message:
                await update.message.reply_text(msg, parse_mode="Markdown")
            elif update.callback_query:
                await update.callback_query.answer("Upgrade to Premium required.", show_alert=True)
            return ConversationHandler.END

        return await func(update, context, *args, **kwargs)

    return wrapper
