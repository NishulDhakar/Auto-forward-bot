"""Conversational support/setup agent for free and paid bot users."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import SUPER_ADMIN_ID, TRIAL_DAYS
from bot.db import users as users_db
from bot.utils.groq_agent import AgentUnavailable, ask_agent, build_system_prompt
from bot.utils.roles import is_admin_active

logger = logging.getLogger(__name__)
_HISTORY_KEY = "support_agent_history"
_MAX_HISTORY_MESSAGES = 12
_MAX_REPLY_CHARS = 4000


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _account_context(user_id: int, user: dict) -> str:
    """Return only the plan facts the model needs; never include secrets."""
    if user_id == SUPER_ADMIN_ID or user.get("role") == "superadmin":
        return (
            "Access mode: paid/unlimited (super admin).\n"
            "Paid setup commands are available. Subscription purchase prompts are unnecessary."
        )

    if is_admin_active(user):
        expiry = _parse_datetime(user.get("subscription_end"))
        expiry_text = expiry.strftime("%Y-%m-%d %H:%M UTC") if expiry else "not available"
        plan = user.get("subscription_plan") or "paid plan"
        return (
            f"Access mode: paid and active ({plan}).\n"
            f"Subscription expires: {expiry_text}.\n"
            "The user may use the full setup workflow and needs practical step-by-step help."
        )

    trial_start = user.get("trial_start")
    trial_days = users_db.get_trial_days_remaining(trial_start) if trial_start else 0
    return (
        "Access mode: free (not an active paid subscriber).\n"
        f"Free trial days remaining: {trial_days} of {TRIAL_DAYS}.\n"
        "General guidance is available; real-time forwarding and admin setup require /pro."
    )


def _fallback_answer(paid: bool) -> str:
    if paid:
        return (
            "I can help you set this up. What are you trying to do: forward messages, add a source, "
            "add a target, create a filter, or schedule a message? I’ll guide you one step at a time."
        )
    return (
        "Tell me what problem you are trying to solve. I can explain the forwarding workflow and help you "
        "choose the right feature. Real-time forwarding and setup require an active paid plan; use /pro "
        "to view the available plans."
    )


def _is_paid(user_id: int, user: dict) -> bool:
    return user_id == SUPER_ADMIN_ID or user.get("role") == "superadmin" or is_admin_active(user)


async def _get_user(update: Update) -> dict | None:
    tg_user = update.effective_user
    if tg_user is None:
        return None
    return await asyncio.to_thread(
        users_db.get_or_create_user,
        tg_user.id,
        tg_user.username,
    )


async def start_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a fresh conversation and explicitly ask for the user's problem."""
    context.user_data.pop(_HISTORY_KEY, None)
    await update.effective_message.reply_text(
        "I’m ready to help with your Auto Forward Bot setup.\n\n"
        "What problem are you trying to solve? Tell me what you want to forward or configure."
    )


async def handle_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer a private text message using the current account plan as context."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or not message.text or chat is None or chat.type != "private":
        return

    user = await _get_user(update)
    if user is None:
        return
    user_id = update.effective_user.id
    paid = _is_paid(user_id, user)
    history = context.user_data.setdefault(_HISTORY_KEY, [])
    history.append({"role": "user", "content": message.text.strip()})
    history[:] = history[-_MAX_HISTORY_MESSAGES:]

    messages = [
        {"role": "system", "content": build_system_prompt(_account_context(user_id, user))},
        *history,
    ]
    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        answer = await ask_agent(messages)
    except AgentUnavailable:
        answer = _fallback_answer(paid)

    answer = answer[:_MAX_REPLY_CHARS]
    history.append({"role": "assistant", "content": answer})
    history[:] = history[-_MAX_HISTORY_MESSAGES:]
    await message.reply_text(answer)


def register(application) -> None:
    application.add_handler(CommandHandler("ask", start_agent))
    application.add_handler(CommandHandler("agent", start_agent))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_agent_message))
