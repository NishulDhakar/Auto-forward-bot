"""
bot/handlers/user.py
Handlers for regular User-role commands: /start and /plan.
"""

from __future__ import annotations

import asyncio

import logging
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import TRIAL_DAYS
from bot.db import users as users_db
from bot.db.content import get_content
from bot.db import referrals as ref_db
from bot.utils import userbot_manager

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = BASE_DIR / "images"

# Custom Premium Emojis (HTML)
telegram_emoji = "<tg-emoji emoji-id='6269232713929069503'>💎</tg-emoji>"
check_emoji = "<tg-emoji emoji-id='5215538285438311443'>✅</tg-emoji>"
yellow_emoji = "<tg-emoji emoji-id='5978986632315931621'>🟨</tg-emoji>"
rocket_star = "<tg-emoji emoji-id='5895720492190404869'>🚀</tg-emoji>"
authorize = "<tg-emoji emoji-id='5852518859268951767'>✅</tg-emoji>"
check_green = "<tg-emoji emoji-id='5852871561983299073'>👑</tg-emoji>"
crown_emoji = "<tg-emoji emoji-id='5433758796289685818'>🤖</tg-emoji>"
bot_emoji = "<tg-emoji emoji-id='5314391089514291948'>🟨</tg-emoji>"

# New Custom Premium Emojis
dash_emoji = "<tg-emoji emoji-id='5382261056078881010'>➖</tg-emoji>"
money_emoji = "<tg-emoji emoji-id='6296202896639791835'>💰</tg-emoji>"
shield_emoji = "<tg-emoji emoji-id='6269105110450705259'>🛡️</tg-emoji>"
box_emoji = "<tg-emoji emoji-id='5884479287171485878'>🔲</tg-emoji>"

def _get_admin_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🏠 START")],
        [KeyboardButton("📥 ADD SOURCE"), KeyboardButton("📤 ADD TARGET")],
        [KeyboardButton("❌ REMOVE SOURCE"), KeyboardButton("❌ REMOVE TARGET")],
        [KeyboardButton("🔍 FILTER"), KeyboardButton("📋 MY FILTERS")],
        [KeyboardButton("⏰ SCHEDULE"), KeyboardButton("🗑️ REMOVE SCHEDULE")],
        [KeyboardButton("📊 MY STATUS"), KeyboardButton("💎 PRO")],
        [KeyboardButton("🤝 REFER"), KeyboardButton("💰 WITHDRAW")],
        [KeyboardButton("📡 WORK"), KeyboardButton("🛑 STOP")],
        [KeyboardButton("🔐 AUTHORIZE"), KeyboardButton("💡 HELP")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)

def _get_free_user_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🏠 START")],
        [KeyboardButton("🔐 AUTHORIZE")], [KeyboardButton("💎 PRO")],
        [KeyboardButton("🤝 REFER"), KeyboardButton("💰 WITHDRAW")],
        [KeyboardButton("📋 PLAN"), KeyboardButton("💡 HELP")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)




# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register user, handle referral deep link, show status."""
    tg_user = update.effective_user
    user = (await asyncio.to_thread(users_db.get_or_create_user, tg_user.id, tg_user.username))
    role = user["role"]

    # ── Handle referral deep link: /start ref_<referrer_id> ──────────────
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
                # Only record if this is a new user (no existing referral) and not self-referral
                if referrer_id != tg_user.id and not user.get("referred_by"):
                    (await asyncio.to_thread(ref_db.record_referral, referrer_id, tg_user.id))
                    (await asyncio.to_thread(users_db.save_referred_by, tg_user.id, referrer_id))
            except (ValueError, Exception) as e:
                logger.debug("Referral parse error: %s", e)

    if role == "superadmin":
        await update.message.reply_text(
            f"<b>{check_green} Welcome back, Super Admin! {crown_emoji}</b>\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👥 <b>Admin Management</b>\n"
            f"/addadmin — Promote user to admin\n"
            f"/removeadmin — Demote an admin\n"
            f"/alladmins — List all admins\n"
            f"/allchannels — List all channels\n"
            f"/admin_subscriptions — View subscriptions\n"
            f"/grant_premium — Grant premium access\n"
            f"/revoke_premium — Revoke premium access\n\n"
            f"<b>Payments & Income</b>\n"
            f"/addincome — Log a manual payment\n"
            f"/stats — View system statistics\n\n"
            f"📢 <b>Broadcast & Content</b>\n"
            f"/broadcast — Send message to all users\n"
            f"/update — Edit bot content (welcome msg etc.)\n\n"
            f"🤝 <b>Referral System</b>\n"
            f"/referralstats — Top referrers leaderboard\n"
            f"/editreferral — Adjust a user's referral balance\n"
            f"/refer — Your own referral dashboard\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Type /cancel to abort any active conversation.",
            parse_mode="HTML",
            reply_markup=_get_admin_keyboard(),
        )
        return

    if role == "admin":
        sub_end_str = user.get("subscription_end")
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)
        days_left = max(0, (sub_end - datetime.now(timezone.utc)).days) if sub_end else 0

        # Check if userbot is authorized
        if not (await asyncio.to_thread(userbot_manager.is_userbot_authorized, tg_user.id)):
            await update.message.reply_text(
                f"<b>{check_green} Welcome, Admin! {crown_emoji}</b>\n\n"
                f"📅 Subscription expires: <code>{sub_end.strftime('%Y-%m-%d') if sub_end else 'N/A'}</code>\n"
                f"⏳ Days remaining: <b>{days_left}</b>\n\n"
                f"⚠️ <b>Telegram Account Not Linked</b>\n"
                f"Your account is not linked yet. Please authorize first to start forwarding.\n\n"
                f"👉 /authorize — Link your Telegram account\n\n"
                f"💡 Need help? Send /help",
                parse_mode="HTML",
                reply_markup=_get_admin_keyboard(),
            )
            return

        # Fully authorized admin — show all commands in organized sections
        await update.message.reply_text(
            f"<b>{check_green} Welcome back, Admin! {crown_emoji}</b>\n\n"
            f"📅 Subscription: <code>{sub_end.strftime('%Y-%m-%d') if sub_end else 'N/A'}</code> | <b>{days_left} days left</b>\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📡 <b>Forwarding</b>\n"
            f"/work — Start/Resume forwarding\n"
            f"/stop — Stop/Pause forwarding\n"
            f"/addsource — Set source channel\n"
            f"/removesource — Remove source channel\n"
            f"/addtarget — Add a target channel\n"
            f"/removetarget — Remove a target channel\n\n"
            f"🔍 <b>Filters</b>\n"
            f"/filter — Add text/keyword filter\n"
            f"/myfilters — View & remove filters\n\n"
            f"⏰ <b>Scheduling</b>\n"
            f"/schedule — Schedule a message\n"
            f"/removeschedule — Remove a schedule\n\n"
            f"👤 <b>Account</b>\n"
            f"/mystatus — View subscription status\n"
            f"/plan — Check plan details\n"
            f"/pro — Upgrade / renew plan\n\n"
            f"🤝 <b>Referral & Earnings</b>\n"
            f"/refer — Referral dashboard & link\n"
            f"/withdraw — Cash out your earnings\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 Need help? Send /help",
            parse_mode="HTML",
            reply_markup=_get_admin_keyboard(),
        )
        return

    # Regular user — show greeting, all admin commands, and premium call-to-action
    first_name = tg_user.first_name or "User"

    default_text = (
        f"<b>{crown_emoji} Welcome {first_name} to the Auto Forward Bot!</b>\n\n"
        f"{check_green} <b>Start Here:</b> Send /help to see step-by-step image tutorials on how to use the bot!\n\n"
        f"<b>{rocket_star} Available Commands:</b>\n"
        f"{dash_emoji} /authorize — Link Telegram account {authorize}\n\n"
        f"{dash_emoji} /addsource — Set source channel\n"
        f"{dash_emoji} /addtarget — Add target channels\n"
        f"{dash_emoji} /filter — Add text & link filters\n"
        f"{dash_emoji} /myfilters — View/remove filters\n"
        f"{dash_emoji} /schedule — Schedule messages\n"
        f"{dash_emoji} /removeschedule — Remove a schedule\n"
        f"{dash_emoji} /mystatus — View your status\n"
        f"{dash_emoji} /plan — Check plan status\n"
        f"{dash_emoji} /refer — Refer & Earn {money_emoji}\n\n"
        f"<b>{yellow_emoji} Paid Plan Needed:</b>\n"
        f"To activate real-time channel forwarding, please use /pro to purchase a plan and activate your account!"
    )

    text = (await asyncio.to_thread(get_content, "welcome_msg", default_text)).replace("{first_name}", first_name)

    image_path = IMAGES_DIR / "welcome.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=_get_free_user_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=_get_free_user_keyboard())


# ─────────────────────────────────────────────────────────────────────────────
# /plan
# ─────────────────────────────────────────────────────────────────────────────

async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current plan and trial/subscription status."""
    tg_user = update.effective_user
    user = (await asyncio.to_thread(users_db.get_or_create_user, tg_user.id, tg_user.username))
    role = user["role"]

    if role == "superadmin":
        await update.message.reply_text(f"👑 <b>You are the Super Admin — unlimited access!</b> {telegram_emoji}", parse_mode="HTML")
        return

    if role == "admin":
        sub_end_str = user.get("subscription_end")
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        active = sub_end and sub_end > now
        days_left = max(0, (sub_end - now).days) if sub_end else 0

        status_icon = check_emoji if active else "❌"
        await update.message.reply_text(
            f"{telegram_emoji} <b>Your Plan</b>\n\n"
            f"Role: <b>Admin</b>\n"
            f"Status: {status_icon} <b>{'Active' if active else 'Expired'}</b>\n"
            f"Expires: <code>{sub_end.strftime('%Y-%m-%d %H:%M UTC') if sub_end else 'N/A'}</code>\n"
            f"Days left: <b>{days_left}</b>",
            parse_mode="HTML",
        )
        return

    # Free user
    trial_start = user.get("trial_start")
    days_left = (await asyncio.to_thread(users_db.get_trial_days_remaining, trial_start)) if trial_start else 0
    active = days_left > 0

    status_icon = check_emoji if active else "❌"
    await update.message.reply_text(
        f"{telegram_emoji} <b>Your Plan</b>\n\n"
        f"Role: <b>Free Trial</b>\n"
        f"Status: {status_icon} <b>{'Active' if active else 'Expired'}</b>\n"
        f"Days remaining: <b>{days_left}</b> / {TRIAL_DAYS}\n\n"
        + (
            "Please use /pro to view subscription plans and upgrade." if not active else
            "Your trial is active. Please use /pro to view subscription plans and upgrade to a paid plan."
        ),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Regex("^🏠 START$"), start_command))
    application.add_handler(CommandHandler("plan", plan_command))
    application.add_handler(MessageHandler(filters.Regex("^📋 PLAN$"), plan_command))
    # /pro is now registered in bot/handlers/payment.py


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route unrecognized private messages to the conversational support agent."""
    if not update.message or not update.message.text:
        return
    if update.effective_chat.type != "private":
        return

    from bot.handlers.agent import handle_agent_message
    await handle_agent_message(update, context)

def register_fallback(application) -> None:
    application.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, fallback_handler))


