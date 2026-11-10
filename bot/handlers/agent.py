"""Conversational support/setup agent for free and paid bot users."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from telegram import Message, MessageEntity, Update
from telegram.constants import ChatAction
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import SUPER_ADMIN_ID, TRIAL_DAYS
from bot.db import users as users_db
from bot.utils.groq_agent import AgentUnavailable, ask_agent, build_system_prompt
from bot.utils.roles import is_admin_active

logger = logging.getLogger(__name__)
_HISTORY_KEY = "support_agent_history"
_MAX_HISTORY_MESSAGES = 12
_MAX_REPLY_CHARS = 700


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
        return "What do you need: source, target, filter, schedule, or forwarding?"
    return "That setup needs Premium. Use /pro to upgrade."


def _is_paid(user_id: int, user: dict) -> bool:
    return user_id == SUPER_ADMIN_ID or user.get("role") == "superadmin" or is_admin_active(user)


def _detect_setup_command(text: str) -> str | None:
    """Map common requests to the existing interactive bot commands."""
    value = re.sub(r"\s+", " ", text.lower()).strip()
    if re.search(r"\b(upgrade|premium|paid plan|buy|purchase)\b", value):
        return "/pro"
    if re.search(r"\b(plan|trial|subscription|expiry|expire)\b", value):
        return "/plan"
    if re.search(r"\b(my filters|show filters|list filters)\b", value):
        return "/myfilters"
    if re.search(r"\b(remove|delete)\b.*\b(schedule|scheduled)\b", value):
        return "/removeschedule"
    if re.search(r"\b(remove|delete)\b.*\b(target|destination)\b", value):
        return "/removetarget"
    if re.search(r"\b(remove|delete)\b.*\b(source|channel)\b", value):
        return "/removesource"
    if re.search(r"\b(stop|pause) forwarding\b", value):
        return "/stop"
    if re.search(r"\b(start|resume) forwarding\b", value):
        return "/work"
    if re.search(r"\b(status|connection|connected|logged in)\b", value):
        return "/mystatus"
    if re.search(r"\b(authorize|authorise|login|log in|link my account)\b", value):
        return "/authorize"
    if re.search(r"\b(source|from channel|copy from|monitor)\b", value):
        return "/addsource"
    if (
        re.search(r"\b(target|destination|receive messages|forward to)\b", value)
        or "help me to add my channel" in value
        or "this is my channel" in value
        or ("add my channel" in value and "source" not in value)
    ):
        return "/addtarget"
    if re.search(r"\b(filter|replace links|block keyword|block apk)\b", value):
        return "/filter"
    if re.search(r"\b(schedule|scheduled|daily message)\b", value):
        return "/schedule"
    if value in {"help", "help me", "commands", "menu"}:
        return "/help"
    return None


def _compact_reply(answer: str) -> str:
    """Keep model output useful in a Telegram chat instead of essay-like."""
    lines = [line.strip() for line in answer.strip().splitlines() if line.strip()]
    compact = "\n".join(lines[:3])
    if len(compact) > _MAX_REPLY_CHARS:
        compact = compact[:_MAX_REPLY_CHARS].rsplit(" ", 1)[0].rstrip() + "…"
    return compact or "Tell me what you need help with."


async def _run_internal_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
) -> None:
    """Feed a detected command through the real ConversationHandler."""
    message = update.effective_message
    synthetic_message = Message(
        message_id=message.message_id,
        date=message.date,
        chat=message.chat,
        from_user=message.from_user,
        text=command,
        entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(command))],
    )
    synthetic_message.set_bot(context.bot)
    synthetic_update = Update(update.update_id, message=synthetic_message)
    synthetic_update.set_bot(context.bot)
    await context.application.process_update(synthetic_update)




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
    await update.effective_message.reply_text("What do you need help with?")


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
    command = _detect_setup_command(message.text)
    if command == "/mystatus" and not paid:
        command = "/plan"
    paid_only = {
        "/authorize", "/addsource", "/addtarget", "/filter", "/myfilters",
        "/schedule", "/removesource", "/removetarget", "/removeschedule",
        "/mystatus", "/work", "/stop",
    }
    if command:
        context.user_data.pop(_HISTORY_KEY, None)
        if command in paid_only and not paid:
            await message.reply_text("That feature needs Premium. Use /pro to upgrade.")
        else:
            await _run_internal_command(update, context, command)
        return
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

    answer = _compact_reply(answer)
    history.append({"role": "assistant", "content": answer})
    history[:] = history[-_MAX_HISTORY_MESSAGES:]
    await message.reply_text(answer)


def register(application) -> None:
    application.add_handler(CommandHandler("ask", start_agent))
    application.add_handler(CommandHandler("agent", start_agent))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_agent_message))
