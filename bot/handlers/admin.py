"""
bot/handlers/admin.py
Admin-role commands:
  /authorize   — link Telegram account via OTP (required first step)
  /addsource   — set source channel (private or public)
  /addtarget   — add a target channel (multiple allowed)
  /removesource
  /removetarget
  /filter      — add find→replace text filter (links, usernames, any text)
  /myfilters   — list and remove filters
  /mystatus    — show subscription + auth status
  /schedule    — create a scheduled message
  /removeschedule
"""

from __future__ import annotations

import logging
import asyncio
import re
import warnings
from datetime import datetime, timezone, timedelta
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from bot.db import channels as channels_db
from bot.db import filters as filters_db
from bot.db import schedules as schedules_db
from bot.db import users as users_db
from bot.utils.roles import require_admin
from bot.utils.scheduler import remove_scheduled_job, schedule_message
from bot.utils import userbot_manager
from bot.utils.userbot_manager import invalidate_cache

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Conversation states
# ─────────────────────────────────────────────────────────────────────────────
(
    ADD_SOURCE_WAIT,
    ADD_TARGET_WAIT,
    FILTER_TYPE_WAIT,
    FILTER_FIND_WAIT,
    FILTER_REPLACE_WAIT,
    SCHED_CONTENT_WAIT,
    SCHED_TIME_WAIT,
    SCHED_FREQ_WAIT,
    AUTH_PHONE_WAIT,
    AUTH_CODE_WAIT,
    AUTH_2FA_WAIT,
) = range(11)

_CTX_FIND = "filter_find_text"
_CTX_SCHED_CONTENT = "sched_content"
_CTX_SCHED_TIME = "sched_time"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_schedule_time(time_str: str) -> str | None:
    """Parse time string like '14:30', '12:00 PM', '12:00PM IND' into 'HH:MM' UTC."""
    pattern = re.compile(r"^\s*(\d{1,2}):(\d{2})(?:\s*(am|pm))?(?:\s*(utc|ind|ist))?\s*$", re.IGNORECASE)
    match = pattern.match(time_str)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = (match.group(3) or "").upper()
    tz = (match.group(4) or "").upper()

    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    if hour > 23 or minute > 59:
        return None

    dt = datetime(2000, 1, 1, hour, minute)

    if tz in ("IND", "IST"):
        dt = dt - timedelta(hours=5, minutes=30)

    return dt.strftime("%H:%M")


async def _resolve_channel(bot, identifier: str) -> tuple[int | None, str | None]:
    """
    Resolve a channel identifier (username, t.me link, or numeric ID) to (channel_id, username).
    Returns (None, None) on failure.
    """
    identifier = identifier.strip()

    if re.match(r"^-?\d+$", identifier):
        channel_id = int(identifier)
        try:
            chat = await bot.get_chat(channel_id)
            return chat.id, chat.username
        except Exception:
            return None, None

    username = identifier
    if "t.me/" in identifier:
        username = identifier.split("t.me/")[-1].split("/")[0]
    username = username.lstrip("@")

    try:
        chat = await bot.get_chat(f"@{username}")
        return chat.id, chat.username
    except Exception:
        return None, None


async def _cleanup_auth(context: ContextTypes.DEFAULT_TYPE, admin_id: int) -> None:
    """Disconnect and clean up any pending auth client."""
    await userbot_manager.clear_pending_auth(admin_id)
    client = context.user_data.pop("auth_client", None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    context.user_data.pop("auth_phone", None)
    context.user_data.pop("auth_code_hash", None)


async def _clear_other_conversations(update: Update, context: ContextTypes.DEFAULT_TYPE, current_name: str) -> None:
    """Safely clear active conversation states for this user from all other handlers."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    if not chat_id or not user_id:
        return

    # If clearing "authorize", make sure to clean up any pending client
    if current_name != "authorize":
        await _cleanup_auth(context, user_id)

    # Clear user_data to start with a fresh slate for the new command
    if hasattr(context, "user_data") and isinstance(context.user_data, dict):
        context.user_data.clear()

    # Loop through all handler groups and their registered handlers
    if hasattr(context, "application") and hasattr(context.application, "handlers"):
        for group in context.application.handlers.values():
            for handler in group:
                if isinstance(handler, ConversationHandler):
                    if handler.name != current_name:
                        handler._conversations.pop((chat_id, user_id), None)
                        handler._conversations.pop((user_id,), None)
                        handler._conversations.pop((chat_id,), None)


# ─────────────────────────────────────────────────────────────────────────────
# /authorize — OTP login
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def authorize_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _clear_other_conversations(update, context, "authorize")
    admin_id = update.effective_user.id

    if update.callback_query:
        await update.callback_query.answer()

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        await update.effective_message.reply_text(
            "❌ *Bot Not Configured*\n\n"
            "`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are missing from the server `.env` file.\n"
            "Please ask the Super Admin to add them.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if (await asyncio.to_thread(userbot_manager.is_userbot_authorized, admin_id)):
        await update.effective_message.reply_text(
            "✅ *Already Authorized!*\n\n"
            "Your Telegram account is already linked and actively monitoring your source channel.\n\n"
            "Use /addsource to set a source channel or /addtarget to add target channels.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "🔐 *Link Your Telegram Account*\n\n"
        "To monitor private and public channels, I need to link your personal Telegram account.\n\n"
        "📱 *Step 1 of 2:* Send your phone number in international format:\n"
        "`+91xxxxxxxxxx` or `+1xxxxxxxxxx`\n\n"
        "Type /cancel to abort.\n\n"
        "💡 *Need a tutorial?* Send /help",
        parse_mode="Markdown",
    )
    return AUTH_PHONE_WAIT



@require_admin
async def auth_phone_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    admin_id = update.message.from_user.id

    if not re.match(r"^\+\d{7,15}$", phone):
        await update.message.reply_text(
            "❌ Invalid format. Please send your phone number with country code:\n"
            "`+91xxxxxxxxxx`\n\nTry again or /cancel.",
            parse_mode="Markdown",
        )
        return AUTH_PHONE_WAIT

    connecting_msg = await update.message.reply_text("⏳ Connecting to Telegram...")

    try:
        await userbot_manager.clear_pending_auth(admin_id)
        (await asyncio.to_thread(userbot_manager.remove_session, admin_id))

        # connect and pre-switch DC
        from telethon.errors import FloodWaitError
        client = await userbot_manager.prepare_auth_client(phone)

        sent = await client.send_code_request(phone)

        # Store exactly the same client that sent the request
        userbot_manager.set_pending_auth(admin_id, client, phone, sent.phone_code_hash)

        await connecting_msg.edit_text(
            "📨 *Verification Code Sent!*\n\n"
            "Enter the code Telegram sent to your app or SMS.\n\n"
            "🚨 *CRITICAL:* Do NOT send the code as plain digits! Telegram will detect it as a phishing attempt and expire the code immediately.\n\n"
            "✅ Send it with hyphens between each number, e.g., `1-2-3-4-5`.\n\n"
            "Type /cancel to abort.\n\n"
        "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
        return AUTH_CODE_WAIT

    except FloodWaitError as e:
        await connecting_msg.edit_text(
            f"⏳ *Telegram Rate Limit*\n\n"
            f"Too many login attempts. Please wait *{e.seconds} seconds* and try /authorize again.",
            parse_mode="Markdown",
        )
        await userbot_manager.clear_pending_auth(admin_id)
        return ConversationHandler.END

    except Exception as exc:
        logger.error("Failed to send code for admin %s: %s", admin_id, exc)
        await connecting_msg.edit_text(
            f"❌ *Failed to send verification code*\n\n`{exc}`\n\n"
            "Please check your phone number and try /authorize again.",
            parse_mode="Markdown",
        )
        await userbot_manager.clear_pending_auth(admin_id)
        return ConversationHandler.END


@require_admin
async def auth_code_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    code = re.sub(r"\D", "", raw)
    admin_id = update.message.from_user.id
    try:
        await update.message.delete()
    except Exception:
        logger.warning("Could not remove sensitive auth message for admin %s", admin_id)

    pending = userbot_manager.get_pending_auth(admin_id)
    if not pending:
        await update.message.reply_text("❌ Session expired. Please run /authorize again.")
        return ConversationHandler.END

    client = pending["client"]
    phone = pending["phone"]
    code_hash = pending["code_hash"]

    if not code or len(code) < 4:
        await update.message.reply_text(
            "❌ That doesn't look like a valid code. Please send the numeric code from Telegram.\n\n"
            "Try again or /cancel.",
        )
        return AUTH_CODE_WAIT

    try:
        from telethon.errors import (
            PhoneCodeInvalidError, PhoneCodeExpiredError, SessionPasswordNeededError
        )

        logger.info("Attempting sign_in for admin %s", admin_id)
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)

        # Success
        session_string = client.session.save()
        (await asyncio.to_thread(userbot_manager.save_session_string, admin_id, session_string))
        await userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            "✅ *Authorization Successful!*\n\n"
            "Your Telegram account is now linked.\n\n"
            "👉 Please click /start to see all features and commands!",
            parse_mode="Markdown"
        )
        await userbot_manager.start_userbot(admin_id, context.bot)
        return ConversationHandler.END

    except PhoneCodeInvalidError:
        logger.warning("Wrong code entered by admin %s", admin_id)
        await update.message.reply_text(
            "❌ *Wrong Code*\n\n"
            "That code is incorrect. Please check and try again, or /cancel.",
            parse_mode="Markdown",
        )
        return AUTH_CODE_WAIT

    except PhoneCodeExpiredError:
        logger.warning("Code expired for admin %s", admin_id)
        await userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            "⏰ *Code Expired*\n\n"
            "The verification code has expired (Telegram's server rejected it).\n"
            "Run /authorize again to get a fresh code.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 *Two-Step Verification Required*\n\n"
            "Your account has a 2FA password enabled.\n"
            "Please send your *2FA password* now:\n\n"
            "Type /cancel to abort.\n\n"
        "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
        return AUTH_2FA_WAIT

    except Exception as e:
        logger.error("sign_in failed: %s | type: %s", e, type(e).__name__)
        await userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            f"❌ *Login Failed*\n\n`{type(e).__name__}: {e}`\n\nRun /authorize again to try with a fresh code.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END


@require_admin
async def auth_2fa_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    admin_id = update.message.from_user.id
    try:
        await update.message.delete()
    except Exception:
        logger.warning("Could not remove sensitive auth message for admin %s", admin_id)

    pending = userbot_manager.get_pending_auth(admin_id)
    if not pending:
        await update.message.reply_text("❌ Session expired. Please run /authorize again.")
        return ConversationHandler.END

    client = pending["client"]

    try:
        await client.sign_in(password=password)

        session_string = client.session.save()
        (await asyncio.to_thread(userbot_manager.save_session_string, admin_id, session_string))
        await userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            "🎉 *Authorization Successful!* (2FA)\n\n"
            "Your Telegram account is now linked.\n\n"
            "📡 /addsource — Set a source channel\n"
            "🎯 /addtarget — Add target channels\n"
            "🔧 /filter — Set text filters",
            parse_mode="Markdown",
        )
        await userbot_manager.start_userbot(admin_id, context.bot)
        return ConversationHandler.END

    except Exception as exc:
        logger.error("2FA sign-in failed for admin %s: %s", admin_id, exc)
        await update.message.reply_text(
            f"❌ *Wrong 2FA Password*\n\n`{exc}`\n\nTry again or /cancel.",
            parse_mode="Markdown",
        )
        return AUTH_2FA_WAIT


async def auth_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    await userbot_manager.clear_pending_auth(admin_id)
    await update.message.reply_text("❌ Authorization cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Guard helper — use in every command that needs auth
# ─────────────────────────────────────────────────────────────────────────────

async def _check_authorized(update: Update, admin_id: int) -> bool:
    """Reply with prompt and return False if userbot not authorized."""
    if not (await asyncio.to_thread(userbot_manager.is_userbot_authorized, admin_id)):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Login Now", callback_data="auth_login")]
        ])
        await update.effective_message.reply_text(
            "<b>Best Auto Forwarding Bot</b>\n"
            "❌ You Are Not Logged In.\n\n"
            "Please Login to Use Bot 👇",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return False
    return True


_DIALOG_PAGE_SIZE = 10


async def _load_dialog_page(picker: dict, page: int) -> None:
    """Load one page plus a lookahead chat, keeping selection indexes stable."""
    required = (page + 1) * _DIALOG_PAGE_SIZE + 1
    while not picker["exhausted"] and len(picker["dialogs"]) < required:
        try:
            dialog = await anext(picker["iterator"])
        except StopAsyncIteration:
            picker["exhausted"] = True
            break
        if dialog.is_user and getattr(dialog.entity, "bot", False) and dialog.entity.id == picker["bot_id"]:
            continue
        picker["dialogs"].append({
            "id": dialog.id,
            "name": dialog.name or "Unnamed Chat",
            "username": getattr(dialog.entity, "username", None),
        })


def _render_dialog_page(picker: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    dialogs = picker["dialogs"]
    prefix = picker["prefix"]
    start = page * _DIALOG_PAGE_SIZE
    stop = min(start + _DIALOG_PAGE_SIZE, len(dialogs))
    keyboard = []
    if dialogs:
        destination = "Source" if prefix == "srcsel" else "Target"
        lines = [f"✉️ <b>Select Your {destination}</b>", "──────────────────────────"]
        for index in range(start, stop):
            # Chat titles are arbitrary text and may contain markup or newlines.
            name = escape(" ".join(dialogs[index]["name"].split())[:150])
            lines.append(f"{index + 1}. {name}")
        lines.append(f"\nShowing chats {start + 1}–{stop}. Tap a number below.")
        for row_start in range(start, stop, 5):
            keyboard.append([
                InlineKeyboardButton(str(index + 1), callback_data=f"{prefix}:{index}")
                for index in range(row_start, min(row_start + 5, stop))
            ])
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton("⬅️ Previous 10", callback_data=f"dialogpage:{prefix}:{page - 1}"))
        if stop < len(dialogs):
            navigation.append(InlineKeyboardButton("Show 10 more ➡️", callback_data=f"dialogpage:{prefix}:{page + 1}"))
        if navigation:
            keyboard.append(navigation)
        message_text = "\n".join(lines)
    else:
        message_text = "📭 No channels, groups, or chats found in your account."
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])
    return message_text, InlineKeyboardMarkup(keyboard)


async def _get_dialogs_keyboard_and_text(admin_id: int, bot, prefix: str, user_data: dict) -> tuple[str, InlineKeyboardMarkup, list[dict]] | None:
    client = await userbot_manager.start_userbot(admin_id, bot)
    if not client:
        return None

    try:
        picker = {
            "iterator": client.iter_dialogs().__aiter__(),
            "dialogs": [], "prefix": prefix, "page": 0,
            "exhausted": False, "bot_id": bot.id,
        }
        await _load_dialog_page(picker, 0)
    except Exception as e:
        logger.error("Error fetching dialogs for %s: %s", admin_id, e)
        return None

    user_data["dialog_picker"] = picker
    message_text, keyboard = _render_dialog_page(picker, 0)
    return message_text, keyboard, picker["dialogs"]


@require_admin
async def dialog_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    picker = context.user_data.get("dialog_picker")
    if not picker:
        await query.answer("This list expired. Open Add Source or Add Target again.", show_alert=True)
        return ConversationHandler.END
    state = ADD_SOURCE_WAIT if picker["prefix"] == "srcsel" else ADD_TARGET_WAIT
    try:
        _, prefix, page_text = query.data.split(":")
        page = int(page_text)
    except (ValueError, AttributeError):
        await query.answer("Invalid page.", show_alert=True)
        return state
    current_page = picker["page"]
    if (prefix != picker["prefix"] or page < 0 or abs(page - current_page) > 1
            or (page > current_page and len(picker["dialogs"]) <= page * _DIALOG_PAGE_SIZE)):
        await query.answer("That page is unavailable.", show_alert=True)
        return state
    await query.answer()
    if page == current_page:
        return state
    try:
        await _load_dialog_page(picker, page)
    except Exception:
        logger.exception("Could not load another chat page for admin %s", query.from_user.id)
        await query.message.reply_text("Couldn't load more chats. Please try the button again.")
        return state
    text, keyboard = _render_dialog_page(picker, page)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    picker["page"] = page
    return state


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ Operation cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /addsource
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def addsource_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _clear_other_conversations(update, context, "addsource")
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    loading_msg = await update.message.reply_text("🔄 Fetching your channels/groups/chats...")

    res = await _get_dialogs_keyboard_and_text(admin_id, context.bot, "srcsel", context.user_data)
    if res:
        msg_text, reply_markup, temp_dialogs = res
        context.user_data["temp_dialogs"] = temp_dialogs
        await loading_msg.delete()
        await update.message.reply_text(
            msg_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    else:
        await loading_msg.delete()
        existing = (await asyncio.to_thread(channels_db.get_source_channel, admin_id))
        existing_note = ""
        if existing:
            display = f"@{existing['channel_username']}" if existing.get("channel_username") else str(existing["channel_id"])
            existing_note = f"\n\n📌 Current source: `{display}` _(will be replaced)_"

        await update.message.reply_text(
            "📡 *Set Source Channel*\n\n"
            "Send the source channel as:\n"
            "• `@username` or `https://t.me/username`\n"
            "• Numeric ID: `-1001234567890`\n"
            "• Or *forward any message* from the channel\n\n"
            "✅ Works for both *public* and *private* channels "
            "(your linked account must be a member of private ones)."
            f"{existing_note}\n\n"
            "Type /cancel to abort.\n\n"
            "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
    return ADD_SOURCE_WAIT


@require_admin
async def addsource_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    admin_id = msg.from_user.id

    # Try forwarded message first
    channel_id, username = None, None
    if msg.forward_origin:
        origin = msg.forward_origin
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if fwd_chat:
            channel_id = fwd_chat.id
            username = fwd_chat.username

    if channel_id is None:
        text = msg.text or ""
        channel_id, username = await _resolve_channel(context.bot, text)

    if channel_id is None:
        # Try via userbot (for private channels the bot can't see)
        client = userbot_manager._clients.get(admin_id)
        if client and msg.text:
            try:
                entity = await client.get_entity(msg.text.strip())
                channel_id = entity.id
                username = getattr(entity, "username", None)
                # Normalize to negative format
                if channel_id > 0:
                    channel_id = int(f"-100{channel_id}")
            except Exception:
                pass

    if channel_id is None:
        await msg.reply_text(
            "❌ Couldn't find that channel.\n\n"
            "Make sure:\n"
            "• The channel username/link is correct\n"
            "• For private channels: your linked account is a member\n\n"
            "Try again or /cancel.",
        )
        return ADD_SOURCE_WAIT

    (await asyncio.to_thread(channels_db.set_source_channel, admin_id, channel_id, username))
    await userbot_manager.restart_userbot_listener(admin_id, context.bot)

    display = f"@{username}" if username else str(channel_id)
    await msg.reply_text(
        f"✅ *Source channel set!*\n\n"
        f"📡 Now monitoring: `{display}`\n"
        f"ID: `{channel_id}`\n\n"
        f"The bot will forward new messages from this channel to your target channels.\n\n"
        f"➡️ *Next Step:* Use /addtarget to add your destination channels.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@require_admin
async def addsource_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    try:
        idx_str = query.data.split(":")[1]
        idx = int(idx_str)
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection. Please start again with /addsource.")
        return ConversationHandler.END

    temp_dialogs = context.user_data.get("temp_dialogs")
    if not temp_dialogs or idx < 0 or idx >= len(temp_dialogs):
        await query.edit_message_text("❌ Session expired or invalid selection. Please try again with /addsource.")
        return ConversationHandler.END

    selected = temp_dialogs[idx]
    channel_id = selected["id"]
    username = selected["username"]

    (await asyncio.to_thread(channels_db.set_source_channel, admin_id, channel_id, username))
    await userbot_manager.restart_userbot_listener(admin_id, context.bot)

    display = f"@{username}" if username else str(channel_id)
    await query.edit_message_text(
        f"✅ *Source channel set!*\n\n"
        f"📡 Now monitoring: `{display}`\n"
        f"ID: `{channel_id}`\n\n"
        f"The bot will forward new messages from this channel to your target channels.\n\n"
        f"➡️ *Next Step:* Use /addtarget to add your destination channels.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /addtarget
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def addtarget_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _clear_other_conversations(update, context, "addtarget")
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    loading_msg = await update.message.reply_text("🔄 Fetching your channels/groups/chats...")

    res = await _get_dialogs_keyboard_and_text(admin_id, context.bot, "tgtsel", context.user_data)
    if res:
        msg_text, reply_markup, temp_dialogs = res
        context.user_data["temp_dialogs"] = temp_dialogs
        await loading_msg.delete()
        await update.message.reply_text(
            msg_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    else:
        await loading_msg.delete()
        targets = (await asyncio.to_thread(channels_db.get_target_channels, admin_id))
        count = len(targets)
        count_note = f"\n\nYou currently have *{count}* target channel(s)." if count else ""

        await update.message.reply_text(
            "🎯 *Add Target Channel*\n\n"
            "Send the target channel as:\n"
            "• `@username` or `https://t.me/username`\n"
            "• Numeric ID: `-1001234567890`\n"
            "• Or *forward any message* from the channel\n\n"
            "⚠️ The bot must be an *admin* of the target channel to post messages."
            f"{count_note}\n\n"
            "You can add multiple targets — just run /addtarget again.\n\n"
            "Type /cancel to abort.\n\n"
            "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
    return ADD_TARGET_WAIT


@require_admin
async def addtarget_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    admin_id = msg.from_user.id

    channel_id, username = None, None
    if msg.forward_origin:
        origin = msg.forward_origin
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if fwd_chat:
            channel_id = fwd_chat.id
            username = fwd_chat.username

    if channel_id is None:
        text = msg.text or ""
        channel_id, username = await _resolve_channel(context.bot, text)

    if channel_id is None:
        await msg.reply_text(
            "❌ Couldn't find that channel.\n\n"
            "Make sure the bot is an *admin* of the target channel, then try again or /cancel.",
        )
        return ADD_TARGET_WAIT

    added = (await asyncio.to_thread(channels_db.add_target_channel, admin_id, channel_id, username))
    invalidate_cache(admin_id)
    display = f"@{username}" if username else str(channel_id)

    if not added:
        await msg.reply_text(
            f"ℹ️ `{display}` is already in your target list.",
            parse_mode="Markdown",
        )
    else:
        targets = (await asyncio.to_thread(channels_db.get_target_channels, admin_id))
        await msg.reply_text(
            f"✅ *Target channel added!*\n\n"
            f"🎯 `{display}` (ID: `{channel_id}`)\n\n"
            f"Total targets: *{len(targets)}*\n\n"
            f"➡️ *Next Steps:*\n"
            f"• Run /addtarget again to add more.\n"
            f"• Use /filter to set up keyword replacements.\n"
            f"• Use /mystatus to verify your setup.",
            parse_mode="Markdown",
        )
    return ConversationHandler.END


@require_admin
async def addtarget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    try:
        idx_str = query.data.split(":")[1]
        idx = int(idx_str)
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection. Please start again with /addtarget.")
        return ConversationHandler.END

    temp_dialogs = context.user_data.get("temp_dialogs")
    if not temp_dialogs or idx < 0 or idx >= len(temp_dialogs):
        await query.edit_message_text("❌ Session expired or invalid selection. Please try again with /addtarget.")
        return ConversationHandler.END

    selected = temp_dialogs[idx]
    channel_id = selected["id"]
    username = selected["username"]

    added = (await asyncio.to_thread(channels_db.add_target_channel, admin_id, channel_id, username))
    invalidate_cache(admin_id)
    display = f"@{username}" if username else str(channel_id)

    if not added:
        await query.edit_message_text(
            f"ℹ️ `{display}` is already in your target list.",
            parse_mode="Markdown",
        )
    else:
        targets = (await asyncio.to_thread(channels_db.get_target_channels, admin_id))
        await query.edit_message_text(
            f"✅ *Target channel added!*\n\n"
            f"🎯 `{display}` (ID: `{channel_id}`)\n\n"
            f"Total targets: *{len(targets)}*\n\n"
            f"➡️ *Next Steps:*\n"
            f"• Run /addtarget again to add more.\n"
            f"• Use /filter to set up keyword replacements.\n"
            f"• Use /mystatus to verify your setup.",
            parse_mode="Markdown",
        )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /removesource
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def removesource_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    source = (await asyncio.to_thread(channels_db.get_source_channel, admin_id))
    if not source:
        await update.message.reply_text("📭 You don't have a source channel set.")
        return

    display = f"@{source['channel_username']}" if source.get("channel_username") else str(source["channel_id"])
    (await asyncio.to_thread(channels_db.remove_source_channel, admin_id))
    await userbot_manager.stop_userbot(admin_id)
    await update.message.reply_text(
        f"✅ Source channel `{display}` removed.\n"
        f"The background listener has been stopped.",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /removetarget
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def removetarget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    targets = (await asyncio.to_thread(channels_db.get_target_channels, admin_id))
    if not targets:
        await update.message.reply_text("📭 You have no target channels set. Use /addtarget to add one.")
        return

    text = "🎯 *Your Target Channels*\n\nTap a button to remove it:\n\n"
    keyboard = []
    for i, t in enumerate(targets, 1):
        display = f"@{t['channel_username']}" if t.get("channel_username") else str(t["channel_id"])
        text += f"{i}. `{display}`\n"
        keyboard.append([
            InlineKeyboardButton(f"🗑 Remove {display}", callback_data=f"rmtarget:{t['channel_id']}")
        ])

    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@require_admin
async def removetarget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    channel_id = int(query.data.split(":", 1)[1])

    deleted = (await asyncio.to_thread(channels_db.remove_target_channel, admin_id, channel_id))
    invalidate_cache(admin_id)
    if deleted:
        targets = (await asyncio.to_thread(channels_db.get_target_channels, admin_id))
        await query.edit_message_text(
            f"✅ Target channel removed.\n"
            f"Remaining targets: *{len(targets)}*",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text("❌ Channel not found (already removed?).")


# ─────────────────────────────────────────────────────────────────────────────
# /filter — add find→replace rule
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def filter_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _clear_other_conversations(update, context, "filter")
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🔗 Replace Any Link", callback_data="ftype:all_links")],
        [InlineKeyboardButton("👤 Replace Any Username", callback_data="ftype:all_usernames")],
        [InlineKeyboardButton("🎯 Replace Specific Text/Link", callback_data="ftype:specific")],
        [InlineKeyboardButton("🚫 Block Message by Keyword", callback_data="ftype:block")],
        [InlineKeyboardButton("📦 Block APK Files", callback_data="ftype:block_apk")]
    ]

    await update.message.reply_text(
        "🔧 *Add Text Filter*\n\n"
        "Filters automatically replace text, links, or usernames in messages before forwarding.\n\n"
        "What would you like to do?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return FILTER_TYPE_WAIT

@require_admin
async def filter_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ftype = query.data.split(":")[1]

    if ftype == "all_links":
        context.user_data[_CTX_FIND] = "<ALL_LINKS>"
        await query.edit_message_text(
            "🔗 *Replace Any Link*\n\n"
            "Send the *default link* you want to replace ALL links with.\n"
            "_(Send a single space or leave blank to delete occurrences.)_\n\n"
            "Type /cancel to abort.\n\n"
        "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
        return FILTER_REPLACE_WAIT

    elif ftype == "all_usernames":
        context.user_data[_CTX_FIND] = "<ALL_USERNAMES>"
        await query.edit_message_text(
            "👤 *Replace Any Username*\n\n"
            "Send the *default username* you want to replace ALL usernames with.\n"
            "_(Send a single space or leave blank to delete occurrences.)_\n\n"
            "Type /cancel to abort.\n\n"
        "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
        return FILTER_REPLACE_WAIT

    elif ftype == "block":
        context.user_data[_CTX_FIND] = "<BLOCK>"
        await query.edit_message_text(
            "🚫 *Block Message by Keyword*\n\n"
            "Send the *keyword*. If a message contains this keyword, it will NOT be forwarded.\n"
            "_(Case-insensitive match)_ \n\n"
            "Type /cancel to abort.\n\n"
        "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
        return FILTER_REPLACE_WAIT

    elif ftype == "block_apk":
        (await asyncio.to_thread(filters_db.add_filter, update.effective_user.id, "<BLOCK_APK>", "true"))
        invalidate_cache(update.effective_user.id)
        await query.edit_message_text(
            "✅ *Filter saved!*\n\n"
            "APK files (`.apk`) will now be blocked and not forwarded.\n"
            "Use /myfilters to view or remove this rule.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    else:
        await query.edit_message_text(
            "🎯 *Replace Specific Text/Link/Username*\n\n"
            "📝 *Step 1:* Send the exact text/link/username you want to *find*.\n\n"
            "Type /cancel to abort.\n\n"
        "💡 *Need a tutorial?* Send /help",
            parse_mode="Markdown",
        )
        return FILTER_FIND_WAIT


@require_admin
async def filter_find_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    find_text = update.message.text.strip()
    if not find_text:
        await update.message.reply_text("❌ Find text cannot be empty. Try again or /cancel.")
        return FILTER_FIND_WAIT

    context.user_data[_CTX_FIND] = find_text
    await update.message.reply_text(
        f"✅ Find text: `{find_text}`\n\n"
        f"📝 *Step 2:* Now send the *replacement text*.\n"
        f"_(Send a single space or leave blank to delete occurrences.)_",
        parse_mode="Markdown",
    )
    return FILTER_REPLACE_WAIT


@require_admin
async def filter_replace_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    replace_text = update.message.text or ""
    find_text = context.user_data.pop(_CTX_FIND, None)
    admin_id = update.message.from_user.id

    if not find_text:
        await update.message.reply_text("❌ Session expired. Please start /filter again.")
        return ConversationHandler.END

    (await asyncio.to_thread(filters_db.add_filter, admin_id, find_text, replace_text.strip()))
    invalidate_cache(admin_id)

    label = f"`{replace_text.strip()}`" if replace_text.strip() else "_(deleted)_"
    display_find = "Any Link" if find_text == "<ALL_LINKS>" else "Any Username" if find_text == "<ALL_USERNAMES>" else "Block Keyword" if find_text == "<BLOCK>" else f"`{find_text}`"

    current = (await asyncio.to_thread(filters_db.get_filters, admin_id))
    await update.message.reply_text(
        f"✅ *Filter saved!*\n\n"
        f"Find: {display_find}\n"
        f"Replace with: {label}\n\n"
        f"Total filters: *{len(current)}* — use /myfilters to view all.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /myfilters
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def myfilters_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    rule_list = (await asyncio.to_thread(filters_db.get_filters, admin_id))
    if not rule_list:
        await update.message.reply_text(
            "📭 No filters set yet.\n\nUse /filter to add a text replacement rule."
        )
        return

    text = "🔧 *Your Text Filters*\n\n"
    keyboard = []
    for i, f in enumerate(rule_list, 1):
        replace = f["replace_text"] or "_(delete)_"
        display_find = "Any Link" if f['find_text'] == "<ALL_LINKS>" else "Any Username" if f['find_text'] == "<ALL_USERNAMES>" else "Block Keyword" if f['find_text'] == "<BLOCK>" else "Block APK Files" if f['find_text'] == "<BLOCK_APK>" else f"`{f['find_text']}`"
        text += f"{i}. {display_find} → `{replace}`\n"
        keyboard.append([
            InlineKeyboardButton(f"🗑 Remove #{i}", callback_data=f"rmfilter:{f['id']}")
        ])

    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@require_admin
async def myfilters_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    filter_id = query.data.split(":", 1)[1]
    admin_id = query.from_user.id
    deleted = (await asyncio.to_thread(filters_db.remove_filter, filter_id, admin_id))
    invalidate_cache(admin_id)
    if deleted:
        await query.edit_message_text("✅ Filter removed.")
    else:
        await query.edit_message_text("❌ Filter not found (already removed?).")


# ─────────────────────────────────────────────────────────────────────────────
# /mystatus
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def mystatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = (await asyncio.to_thread(users_db.get_user, user_id))

    if not user:
        await update.message.reply_text("❌ User not found. Try /start first.")
        return

    sub_end_str = user.get("subscription_end")
    if not sub_end_str:
        await update.message.reply_text("❌ No subscription found. Contact the Super Admin.")
        return

    sub_end = datetime.fromisoformat(sub_end_str)
    if sub_end.tzinfo is None:
        sub_end = sub_end.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    days_left = max(0, (sub_end - now).days)
    is_active = sub_end > now

    source = (await asyncio.to_thread(channels_db.get_source_channel, user_id))
    targets = (await asyncio.to_thread(channels_db.get_target_channels, user_id))
    rule_list = (await asyncio.to_thread(filters_db.get_filters, user_id))
    ub_active = (await asyncio.to_thread(userbot_manager.is_userbot_authorized, user_id))

    source_str = (
        f"@{source['channel_username']}" if source and source.get("channel_username")
        else str(source["channel_id"]) if source
        else "❌ Not set"
    )
    ub_str = "✅ Session saved" if ub_active else "❌ Not linked — run /authorize"

    await update.message.reply_text(
        f"📊 *Your Status*\n\n"
        f"👤 Role: *Admin*\n"
        f"📅 Subscription: {'✅ Active' if is_active else '❌ Expired'}\n"
        f"📆 Expires: `{sub_end.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"⏳ Days left: *{days_left}*\n\n"
        f"🔐 Account: {ub_str}\n"
        f"📡 Source: `{source_str}`\n"
        f"🎯 Targets: *{len(targets)}* channel(s)\n"
        f"🔧 Filters: *{len(rule_list)}* rule(s)\n"
        f"📡 Listener: {'Connected' if userbot_manager.is_userbot_running(user_id) else 'Offline'}\n"
        f"📥 Queued posts: {userbot_manager.delivery_status(user_id)['queued']}\n"
        f"⚠️ Recent delivery failures: {userbot_manager.delivery_status(user_id)['recent_failures']}",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /schedule
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _clear_other_conversations(update, context, "schedule")
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    await update.message.reply_text(
        "⏰ *Schedule a Message*\n\n"
        "Step 1/3 — Send the *message content* to schedule.",
        parse_mode="Markdown",
    )
    return SCHED_CONTENT_WAIT


@require_admin
async def sched_content_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    content = update.message.text
    if not content:
        await update.message.reply_text("❌ Message content cannot be empty. Try again.")
        return SCHED_CONTENT_WAIT

    context.user_data[_CTX_SCHED_CONTENT] = content
    await update.message.reply_text(
        "✅ Content saved!\n\n"
        "Step 2/3 — Send the *time* to post.\n"
        "Examples:\n"
        "• `14:30` (UTC)\n"
        "• `12:00 PM` (UTC)\n"
        "• `12:00 PM IND` (Indian Time)\n"
        "• `14:30 IST`",
        parse_mode="Markdown",
    )
    return SCHED_TIME_WAIT


@require_admin
async def sched_time_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = (update.message.text or "").strip()
    parsed_utc = parse_schedule_time(time_str)
    if not parsed_utc:
        await update.message.reply_text(
            "❌ Invalid format. Use HH:MM or HH:MM PM [IND], e.g. `14:30` or `12:00 PM IND`\nTry again.",
            parse_mode="Markdown",
        )
        return SCHED_TIME_WAIT

    context.user_data[_CTX_SCHED_TIME] = parsed_utc
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔂 Daily", callback_data="schedfreq:daily"),
            InlineKeyboardButton("1️⃣ One-time", callback_data="schedfreq:once"),
        ]
    ])
    await update.message.reply_text(
        f"✅ Time set: *{parsed_utc} UTC*\n\nStep 3/3 — How often?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return SCHED_FREQ_WAIT


@require_admin
async def sched_freq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    frequency = query.data.split(":", 1)[1]
    admin_id = query.from_user.id

    content = context.user_data.pop(_CTX_SCHED_CONTENT, None)
    post_time = context.user_data.pop(_CTX_SCHED_TIME, None)

    if not content or not post_time:
        await query.edit_message_text("❌ Session expired. Please start /schedule again.")
        return ConversationHandler.END

    record = (await asyncio.to_thread(schedules_db.add_schedule, admin_id, content, post_time, frequency))
    schedule_id = record.get("id")
    if schedule_id:
        schedule_message(
            bot=context.bot,
            schedule_id=schedule_id,
            admin_id=admin_id,
            content=content,
            post_time=post_time,
            frequency=frequency,
        )

    freq_label = "Daily 🔂" if frequency == "daily" else "One-time 1️⃣"
    await query.edit_message_text(
        f"✅ *Scheduled!*\n\n"
        f"Time: `{post_time} UTC`\n"
        f"Frequency: {freq_label}\n\n"
        f"Preview: _{content[:100]}{'...' if len(content) > 100 else ''}_",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /removeschedule
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def removeschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    scheds = (await asyncio.to_thread(schedules_db.get_schedules, admin_id, active_only=True))
    if not scheds:
        await update.message.reply_text("📭 You have no active scheduled messages.")
        return

    text = "⏰ *Your Scheduled Messages*\n\n"
    keyboard = []
    for i, s in enumerate(scheds, 1):
        freq = "🔂 Daily" if s["frequency"] == "daily" else "1️⃣ Once"
        text += f"{i}. `{s['post_time']}` UTC — {freq}\n   _{s['content'][:50]}..._\n\n"
        keyboard.append([
            InlineKeyboardButton(f"🗑 Remove #{i}", callback_data=f"rmsched:{s['id']}")
        ])

    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@require_admin
async def removeschedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    schedule_id = query.data.split(":", 1)[1]
    deactivated = (await asyncio.to_thread(schedules_db.deactivate_schedule, schedule_id, update.effective_user.id))
    if deactivated:
        remove_scheduled_job(schedule_id)
        await query.edit_message_text("✅ Scheduled message removed.")
    else:
        await query.edit_message_text("❌ Schedule not found (already removed?).")


# ─────────────────────────────────────────────────────────────────────────────
# /work and /stop commands
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def work_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    from bot.db.supabase_client import get_client
    db = get_client()
    try:
        (await asyncio.to_thread(lambda: db.table("users").update({"is_working": True}).eq("user_id", admin_id).execute()))
        invalidate_cache(admin_id)
        await userbot_manager.start_userbot(admin_id, context.bot)
        await update.message.reply_text("▶️ *Bot Started Working*\n\nForwarding and scheduled messages are now active.", parse_mode="Markdown")
    except Exception as e:
        logger.error("Failed to update working status to True: %s", e)
        await update.message.reply_text(
            "❌ *Failed to start bot*\n\n"
            "Please verify that the database has been migrated with the new `is_working` column.",
            parse_mode="Markdown"
        )


@require_admin
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    from bot.db.supabase_client import get_client
    db = get_client()
    try:
        (await asyncio.to_thread(lambda: db.table("users").update({"is_working": False}).eq("user_id", admin_id).execute()))
        invalidate_cache(admin_id)
        await update.message.reply_text("⏸️ *Bot Stopped Working*\n\nForwarding and scheduled messages are now paused.", parse_mode="Markdown")
    except Exception as e:
        logger.error("Failed to update working status to False: %s", e)
        await update.message.reply_text(
            "❌ *Failed to stop bot*\n\n"
            "Please verify that the database has been migrated with the new `is_working` column.",
            parse_mode="Markdown"
        )


# ─────────────────────────────────────────────────────────────────────────────
# /cancel (shared fallback)
# ─────────────────────────────────────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    cancel_handler = CommandHandler("cancel", cancel_command)

    # /authorize — OTP login
    application.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("authorize", authorize_start),
            CallbackQueryHandler(authorize_start, pattern=r"^auth_login$"),
            MessageHandler(filters.Regex("^🔐 AUTHORIZE$"), authorize_start),
        ],
        states={
            AUTH_PHONE_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_phone_receive),
            ],
            AUTH_CODE_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_code_receive),
            ],
            AUTH_2FA_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_2fa_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", auth_cancel)],
        name="authorize",
        per_user=True,
        per_chat=True,
    ))

    # /addsource
    application.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("addsource", addsource_start),
            MessageHandler(filters.Regex("^📥 ADD SOURCE$"), addsource_start),
        ],
        states={
            ADD_SOURCE_WAIT: [
                CallbackQueryHandler(cancel_callback, pattern=r"^cancel_action$"),
                CallbackQueryHandler(dialog_page_callback, pattern=r"^dialogpage:srcsel:\d+$"),
                CallbackQueryHandler(addsource_callback, pattern=r"^srcsel:"),
                MessageHandler(filters.ALL & ~filters.COMMAND, addsource_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="addsource",
    ))

    # /addtarget
    application.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("addtarget", addtarget_start),
            MessageHandler(filters.Regex("^📤 ADD TARGET$"), addtarget_start),
        ],
        states={
            ADD_TARGET_WAIT: [
                CallbackQueryHandler(cancel_callback, pattern=r"^cancel_action$"),
                CallbackQueryHandler(dialog_page_callback, pattern=r"^dialogpage:tgtsel:\d+$"),
                CallbackQueryHandler(addtarget_callback, pattern=r"^tgtsel:"),
                MessageHandler(filters.ALL & ~filters.COMMAND, addtarget_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="addtarget",
    ))

    # /filter
    with warnings.catch_warnings():
        import telegram.warnings
        warnings.simplefilter("ignore", telegram.warnings.PTBUserWarning)
        application.add_handler(ConversationHandler(
            entry_points=[
                CommandHandler("filter", filter_start),
                MessageHandler(filters.Regex("^🔍 FILTER$"), filter_start),
            ],
            states={
                FILTER_TYPE_WAIT: [
                    CallbackQueryHandler(filter_type_callback, pattern=r"^ftype:"),
                ],
                FILTER_FIND_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, filter_find_receive),
                ],
                FILTER_REPLACE_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, filter_replace_receive),
                ],
            },
            fallbacks=[cancel_handler],
            name="filter",
        ))

    # /schedule
    with warnings.catch_warnings():
        import telegram.warnings
        warnings.simplefilter("ignore", telegram.warnings.PTBUserWarning)
        application.add_handler(ConversationHandler(
            entry_points=[
                CommandHandler("schedule", schedule_start),
                MessageHandler(filters.Regex("^⏰ SCHEDULE$"), schedule_start),
            ],
            states={
                SCHED_CONTENT_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sched_content_receive),
                ],
                SCHED_TIME_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sched_time_receive),
                ],
                SCHED_FREQ_WAIT: [
                    CallbackQueryHandler(sched_freq_callback, pattern=r"^schedfreq:"),
                ],
            },
            fallbacks=[cancel_handler],
            name="schedule",
            per_message=False,
        ))

    # Standalone commands
    application.add_handler(CommandHandler("removesource", removesource_command))
    application.add_handler(MessageHandler(filters.Regex("^❌ REMOVE SOURCE$"), removesource_command))
    application.add_handler(CommandHandler("removetarget", removetarget_command))
    application.add_handler(MessageHandler(filters.Regex("^❌ REMOVE TARGET$"), removetarget_command))
    application.add_handler(CommandHandler("myfilters", myfilters_command))
    application.add_handler(MessageHandler(filters.Regex("^📋 MY FILTERS$"), myfilters_command))
    application.add_handler(CommandHandler("removeschedule", removeschedule_command))
    application.add_handler(MessageHandler(filters.Regex("^🗑️ REMOVE SCHEDULE$"), removeschedule_command))
    application.add_handler(CommandHandler("mystatus", mystatus_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 MY STATUS$"), mystatus_command))
    application.add_handler(CommandHandler("work", work_command))
    application.add_handler(MessageHandler(filters.Regex("^📡 WORK$"), work_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(MessageHandler(filters.Regex("^🛑 STOP$"), stop_command))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(myfilters_callback, pattern=r"^rmfilter:"))
    application.add_handler(CallbackQueryHandler(removeschedule_callback, pattern=r"^rmsched:"))
    application.add_handler(CallbackQueryHandler(removetarget_callback, pattern=r"^rmtarget:"))
