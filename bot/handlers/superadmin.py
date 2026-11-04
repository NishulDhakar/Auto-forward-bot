"""
bot/handlers/superadmin.py
Super Admin only ConversationHandlers and commands:
  /stats         — System statistics
  /alladmins     — List all admins
  /allchannels   — List all channels
  /addadmin      — Promote user to admin
  /removeadmin   — Demote admin immediately
  /addincome     — Log a manual payment
  /broadcast     — Broadcast message to all users
"""

from __future__ import annotations

import logging
import warnings
import asyncio
from datetime import datetime, timedelta, timezone

from telegram.error import RetryAfter
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.db import channels as channels_db
from bot.db import transactions as tx_db
from bot.db import users as users_db
from bot.db.transactions import log_transaction
from bot.db.users import (
    get_active_subscriptions,
    get_expiring_soon_admins,
    get_expired_admins,
    set_subscription,
    revoke_subscription,
)
from bot.db.content import set_content
from bot.db import referrals as ref_db
from bot.utils.roles import require_superadmin

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Conversation states
# ─────────────────────────────────────────────────────────────────────────────
(
    ADDADMIN_USERID_WAIT,
    ADDADMIN_DURATION_WAIT,
    REMOVEADMIN_WAIT,
    ADDINCOME_ADMINID_WAIT,
    ADDINCOME_AMOUNT_WAIT,
    UPDATE_TEXT_WAIT,
    BROADCAST_WAIT,
    EDITREFERRAL_USERID_WAIT,
    EDITREFERRAL_AMOUNT_WAIT,
) = range(9)

# Context keys
_CTX_TARGET_USER = "target_user_id"
_CTX_INCOME_ADMIN = "income_admin_id"
_CTX_UPDATE_KEY = "update_content_key"


# ─────────────────────────────────────────────────────────────────────────────
# /stats
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    all_admins = (await asyncio.to_thread(users_db.get_all_admins))
    now = datetime.now(timezone.utc)

    active_admins = 0
    for a in all_admins:
        sub_end_str = a.get("subscription_end")
        if sub_end_str:
            sub_end = datetime.fromisoformat(sub_end_str)
            if sub_end.tzinfo is None:
                sub_end = sub_end.replace(tzinfo=timezone.utc)
            if sub_end > now:
                active_admins += 1

    total_channels = (await asyncio.to_thread(channels_db.get_all_channels_count))
    total_income = (await asyncio.to_thread(tx_db.get_total_income))
    total_tx = (await asyncio.to_thread(tx_db.get_total_transactions))

    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👑 Total admins: *{len(all_admins)}*\n"
        f"✅ Active admins: *{active_admins}*\n"
        f"📡 Total channels: *{total_channels}*\n"
        f"💰 Total income: *${total_income:,.2f}*\n"
        f"🧾 Total transactions: *{total_tx}*",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /alladmins
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def alladmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admins = (await asyncio.to_thread(users_db.get_all_admins))

    if not admins:
        await update.message.reply_text("📭 No admins found.")
        return

    now = datetime.now(timezone.utc)
    lines = ["👥 <b>All Admins</b>\n"]

    for i, admin in enumerate(admins, start=1):
        raw_username = f"@{admin['username']}" if admin.get("username") else "—"
        username = raw_username.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        sub_end_str = admin.get("subscription_end")
        if sub_end_str:
            sub_end = datetime.fromisoformat(sub_end_str)
            if sub_end.tzinfo is None:
                sub_end = sub_end.replace(tzinfo=timezone.utc)
            days_left = max(0, (sub_end - now).days)
            status = "✅" if sub_end > now else "❌"
            expiry_str = sub_end.strftime("%Y-%m-%d")
        else:
            status = "❌"
            expiry_str = "N/A"
            days_left = 0

        lines.append(
            f"{i}. {username} (<code>{admin['user_id']}</code>)\n"
            f"   {status} Expires: <code>{expiry_str}</code> ({days_left}d left)"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# /allchannels
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def allchannels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sources = (await asyncio.to_thread(channels_db.get_all_source_channels))
    targets = (await asyncio.to_thread(channels_db.get_all_target_channels))

    if not sources and not targets:
        await update.message.reply_text("📭 No channels configured yet.")
        return

    lines = ["📡 <b>All Channels</b>\n"]

    if sources:
        lines.append("<b>Source Channels:</b>")
        for s in sources:
            raw_display = f"@{s['channel_username']}" if s.get("channel_username") else str(s["channel_id"])
            display = raw_display.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"• {display} — admin <code>{s['added_by']}</code>")
    else:
        lines.append("<b>Source Channels:</b> None")

    lines.append("")

    if targets:
        lines.append("<b>Target Channels:</b>")
        for t in targets:
            raw_display = f"@{t['channel_username']}" if t.get("channel_username") else str(t["channel_id"])
            display = raw_display.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"• {display} — admin <code>{t['admin_id']}</code>")
    else:
        lines.append("<b>Target Channels:</b> None")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# /addadmin conversation
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def addadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "➕ *Add Admin*\n\n"
        "Step 1/2 — Send me the *Telegram User ID* of the user to promote.\n"
        "(They must have started the bot at least once.)\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return ADDADMIN_USERID_WAIT


@require_superadmin
async def addadmin_userid_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric Telegram User ID. Try again.")
        return ADDADMIN_USERID_WAIT

    target_id = int(text)
    user = (await asyncio.to_thread(users_db.get_or_create_user, target_id))

    context.user_data[_CTX_TARGET_USER] = target_id
    username = f"@{user['username']}" if user.get("username") else f"ID {target_id}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("30 Days", callback_data="addadmin_dur:30"),
            InlineKeyboardButton("60 Days", callback_data="addadmin_dur:60"),
        ]
    ])
    await update.message.reply_text(
        f"✅ User found: *{username}*\n\n"
        "Step 2/2 — Select subscription duration:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ADDADMIN_DURATION_WAIT


@require_superadmin
async def addadmin_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    duration_days = int(query.data.split(":", 1)[1])
    target_id = context.user_data.pop(_CTX_TARGET_USER, None)

    if target_id is None:
        await query.edit_message_text("❌ Session expired. Please run /addadmin again.")
        return ConversationHandler.END

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=duration_days)

    # Determine plan label from days
    if duration_days <= 30:
        plan = "1_month"
    elif duration_days <= 90:
        plan = "3_months"
    elif duration_days <= 180:
        plan = "6_months"
    else:
        plan = "manual"

    # Promote using the unified subscription setter
    (await asyncio.to_thread(set_subscription,
        user_id=target_id,
        plan=plan,
        start_dt=now,
        end_dt=expires_at,
        payment_method="manual",
        amount=0.0,
        payment_status="paid",
    ))
    (await asyncio.to_thread(log_transaction, target_id, 0.0, duration_days, now, expires_at))

    # Notify the promoted user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎉 *Congratulations! You have been promoted to Admin!*\n\n"
                f"📅 Subscription: *{duration_days} days*\n"
                f"⏳ Expires: `{expires_at.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                f"Use /start to see your admin commands."
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify user %s: %s", target_id, exc)

    user = (await asyncio.to_thread(users_db.get_user, target_id))
    username = f"@{user['username']}" if user and user.get("username") else f"ID {target_id}"

    await query.edit_message_text(
        f"✅ *{username}* has been promoted to Admin!\n\n"
        f"Duration: *{duration_days} days*\n"
        f"Expires: `{expires_at.strftime('%Y-%m-%d %H:%M UTC')}`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /removeadmin conversation
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def removeadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "➖ *Remove Admin*\n\n"
        "Send me the *Telegram User ID* of the admin to demote.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return REMOVEADMIN_WAIT


@require_superadmin
async def removeadmin_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric User ID. Try again.")
        return REMOVEADMIN_WAIT

    target_id = int(text)
    user = (await asyncio.to_thread(users_db.get_user, target_id))

    if user is None:
        await update.message.reply_text(f"❌ User `{target_id}` not found.", parse_mode="Markdown")
        return REMOVEADMIN_WAIT

    if user["role"] != "admin":
        await update.message.reply_text(f"ℹ️ User `{target_id}` is not an admin.", parse_mode="Markdown")
        return ConversationHandler.END

    (await asyncio.to_thread(users_db.revoke_subscription, target_id))
    from bot.utils import userbot_manager
    userbot_manager.invalidate_cache(target_id)
    await userbot_manager.stop_userbot(target_id)

    # Notify the demoted user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "⚠️ *Your admin access has been revoked by the Super Admin.*\n\n"
                "Contact the Super Admin if you believe this is a mistake."
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify demoted admin %s: %s", target_id, exc)

    username = f"@{user['username']}" if user.get("username") else f"ID {target_id}"
    await update.message.reply_text(
        f"✅ *{username}* has been demoted to User.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /addincome conversation
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def addincome_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "💰 *Log Payment*\n\n"
        "Step 1/2 — Send me the *Admin's Telegram User ID*.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return ADDINCOME_ADMINID_WAIT


@require_superadmin
async def addincome_adminid_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric User ID. Try again.")
        return ADDINCOME_ADMINID_WAIT

    admin_id = int(text)
    user = (await asyncio.to_thread(users_db.get_user, admin_id))
    if user is None:
        await update.message.reply_text(f"❌ User `{admin_id}` not found.", parse_mode="Markdown")
        return ADDINCOME_ADMINID_WAIT

    context.user_data[_CTX_INCOME_ADMIN] = admin_id
    username = f"@{user['username']}" if user.get("username") else f"ID {admin_id}"
    await update.message.reply_text(
        f"✅ Admin: *{username}*\n\n"
        "Step 2/2 — Send me the *payment amount* (numeric, e.g. `50` or `29.99`).",
        parse_mode="Markdown",
    )
    return ADDINCOME_AMOUNT_WAIT


@require_superadmin
async def addincome_amount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().replace(",", ".")
    admin_id = context.user_data.pop(_CTX_INCOME_ADMIN, None)

    if admin_id is None:
        await update.message.reply_text("❌ Session expired. Please run /addincome again.")
        return ConversationHandler.END

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Send a positive number like `50` or `29.99`.")
        return ADDINCOME_AMOUNT_WAIT

    user = (await asyncio.to_thread(users_db.get_user, admin_id))
    sub_end_str = user.get("subscription_end") if user else None
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(sub_end_str) if sub_end_str else now

    (await asyncio.to_thread(log_transaction, admin_id, amount, 0, now, expires_at))

    username = f"@{user['username']}" if user and user.get("username") else f"ID {admin_id}"
    await update.message.reply_text(
        f"✅ *Payment logged!*\n\n"
        f"Admin: *{username}*\n"
        f"Amount: *${amount:,.2f}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /update conversation (Dynamic CMS)
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def update_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Welcome Message", callback_data="update_key:welcome_msg")],
        [InlineKeyboardButton("Help - Link Account", callback_data="update_key:howtoauth")],
        [InlineKeyboardButton("Help - Set Forwarding", callback_data="update_key:howtoaddforwarding")],
        [InlineKeyboardButton("Help - Set Filters", callback_data="update_key:howtosetfilter")],
        [InlineKeyboardButton("Help - Schedule Messages", callback_data="update_key:howtoschedule")],
        [InlineKeyboardButton("Help - Premium Info", callback_data="update_key:howtopro")],
    ])
    await update.message.reply_text(
        "📝 *Update Bot Content*\n\n"
        "Select the message you want to update from the buttons below.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return UPDATE_TEXT_WAIT

@require_superadmin
async def update_key_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    key = query.data.split(":", 1)[1]
    context.user_data[_CTX_UPDATE_KEY] = key

    friendly_names = {
        "welcome_msg": "Welcome Message",
        "howtoauth": "Help - Link Account",
        "howtoaddforwarding": "Help - Set Forwarding",
        "howtosetfilter": "Help - Set Filters",
        "howtoschedule": "Help - Schedule Messages",
        "howtopro": "Help - Premium Info"
    }

    await query.edit_message_text(
        f"📝 <b>Updating: {friendly_names.get(key, key)}</b>\n\n"
        f"Please send me the new text for this section now.\n"
        f"You can use HTML formatting tags (like <b>bold</b>) or &lt;tg-emoji&gt; tags.\n\n"
        f"Send /cancel to abort.",
        parse_mode="HTML"
    )
    return UPDATE_TEXT_WAIT

@require_superadmin
async def update_text_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_text = update.message.text
    if not new_text:
        await update.message.reply_text("❌ Please send text only.")
        return UPDATE_TEXT_WAIT

    key = context.user_data.pop(_CTX_UPDATE_KEY, None)
    if not key:
        await update.message.reply_text("❌ Session expired. Please run /update again.")
        return ConversationHandler.END

    success = (await asyncio.to_thread(set_content, key, new_text))
    if success:
        await update.message.reply_text("✅ *Content updated successfully!*", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Failed to update content in database.", parse_mode="Markdown")

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /cancel (shared)
# ─────────────────────────────────────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /admin_subscriptions
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def admin_subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active, expiring soon, and expired subscription report."""
    now = datetime.now(timezone.utc)

    active = (await asyncio.to_thread(get_active_subscriptions))
    expiring = (await asyncio.to_thread(get_expiring_soon_admins, days=3))
    expired = (await asyncio.to_thread(get_expired_admins))

    expiring_ids = {u["user_id"] for u in expiring}
    true_active = [u for u in active if u["user_id"] not in expiring_ids]

    def _fmt_user(u: dict) -> str:
        uname = f"@{u['username']}" if u.get("username") else f"ID {u['user_id']}"
        sub_end_str = u.get("subscription_end")
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)
        days_left = max(0, (sub_end - now).days) if sub_end else 0
        plan = (u.get("subscription_plan") or "—").replace("_", " ").title()
        method = (u.get("payment_method") or "—").upper()
        exp_str = sub_end.strftime("%d/%m/%Y") if sub_end else "N/A"
        return f"`{u['user_id']}` {uname}\n   Plan: {plan} | {method} | Expires: {exp_str} ({days_left}d)"

    lines = ["📊 *Subscription Report*\n"]

    lines.append(f"✅ *Active* — {len(true_active)} user(s)")
    if true_active:
        for u in true_active:
            lines.append(_fmt_user(u))
    else:
        lines.append("  _None_")

    lines.append(f"\n⚠️ *Expiring Soon* (≤3 days) — {len(expiring)} user(s)")
    if expiring:
        for u in expiring:
            lines.append(_fmt_user(u))
    else:
        lines.append("  _None_")

    lines.append(f"\n❌ *Expired* — {len(expired)} user(s)")
    if expired:
        for u in expired[:10]:  # cap at 10 to avoid message length limits
            lines.append(_fmt_user(u))
        if len(expired) > 10:
            lines.append(f"  _...and {len(expired) - 10} more_")
    else:
        lines.append("  _None_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# /grant_premium USER_ID DAYS
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def grant_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /grant_premium USER_ID DAYS
    Manually grant premium access to a user for the given number of days.
    """
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/grant_premium USER_ID DAYS`\n"
            "Example: `/grant_premium 123456789 30`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(args[0])
        days = int(args[1])
        if days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid arguments. USER_ID and DAYS must be positive integers.\n"
            "Example: `/grant_premium 123456789 30`",
            parse_mode="Markdown",
        )
        return

    user = (await asyncio.to_thread(users_db.get_user, target_id))
    if user is None:
        await update.message.reply_text(
            f"❌ User `{target_id}` not found. They must send /start first.",
            parse_mode="Markdown",
        )
        return

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=days)

    # Determine plan label from days
    if days <= 30:
        plan = "1_month"
    elif days <= 90:
        plan = "3_months"
    elif days <= 180:
        plan = "6_months"
    else:
        plan = "manual"

    (await asyncio.to_thread(set_subscription,
        user_id=target_id,
        plan=plan,
        start_dt=now,
        end_dt=expires_at,
        payment_method="manual",
        amount=0.0,
        payment_status="paid",
    ))
    (await asyncio.to_thread(log_transaction, target_id, 0.0, days, now, expires_at))

    username = f"@{user['username']}" if user.get("username") else f"ID {target_id}"

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"✅ *Payment Successful*\n\n"
                f"Your premium membership has been activated.\n\n"
                f"📦 Plan: *{plan.replace('_', ' ').title()}* ({days} days)\n"
                f"📅 Expires: `{expires_at.strftime('%d/%m/%Y')}`\n\n"
                f"Thank you for your support \u2764\ufe0f"
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify user %s: %s", target_id, exc)

    await update.message.reply_text(
        f"✅ *Premium granted!*\n\n"
        f"User: *{username}* (`{target_id}`)\n"
        f"Plan: *{plan.replace('_', ' ').title()}*\n"
        f"Duration: *{days} days*\n"
        f"Expires: `{expires_at.strftime('%d/%m/%Y')}`",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /revoke_premium USER_ID
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def revoke_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /revoke_premium USER_ID
    Immediately remove premium access from a user.
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Usage: `/revoke_premium USER_ID`\n"
            "Example: `/revoke_premium 123456789`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid USER_ID. Must be a numeric Telegram user ID.")
        return

    user = (await asyncio.to_thread(users_db.get_user, target_id))
    if user is None:
        await update.message.reply_text(f"❌ User `{target_id}` not found.", parse_mode="Markdown")
        return

    if user["role"] not in ("admin", "superadmin"):
        await update.message.reply_text(
            f"ℹ️ User `{target_id}` does not have an active premium subscription.",
            parse_mode="Markdown",
        )
        return

    (await asyncio.to_thread(revoke_subscription, target_id))
    from bot.utils import userbot_manager
    userbot_manager.invalidate_cache(target_id)
    await userbot_manager.stop_userbot(target_id)

    username = f"@{user['username']}" if user.get("username") else f"ID {target_id}"

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "⚠️ *Your premium membership has expired.*\n\n"
                "Renew to continue using premium features.\n"
                "Use /pro to view plans and upgrade."
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify revoked user %s: %s", target_id, exc)

    await update.message.reply_text(
        f"✅ Premium access revoked for *{username}* (`{target_id}`).",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /broadcast
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    total = (await asyncio.to_thread(users_db.get_all_users_count))
    await update.message.reply_text(
        f"📢 *Broadcast Message*\n\n"
        f"Total recipients: *{total} users*\n\n"
        f"Send the message you want to broadcast.\n"
        f"Supports: text, photo, video, document, GIF.\n\n"
        f"Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return BROADCAST_WAIT


@require_superadmin
async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    all_users = (await asyncio.to_thread(users_db.get_all_users))
    total = len(all_users)

    sent = 0
    failed = 0
    blocked = 0

    # Send progress message
    progress_msg = await msg.reply_text(
        f"📤 Broadcasting to {total} users...\n"
        f"✅ Sent: 0 | ❌ Failed: 0 | 🚫 Blocked: 0"
    )

    for i, user in enumerate(all_users):
        uid = user["user_id"]
        # Add a tiny delay between messages to respect Telegram limits
        await asyncio.sleep(0.05)

        try:
            if msg.photo:
                await context.bot.send_photo(
                    chat_id=uid,
                    photo=msg.photo[-1].file_id,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                )
            elif msg.video:
                await context.bot.send_video(
                    chat_id=uid,
                    video=msg.video.file_id,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                )
            elif msg.animation:
                await context.bot.send_animation(
                    chat_id=uid,
                    animation=msg.animation.file_id,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                )
            elif msg.document:
                await context.bot.send_document(
                    chat_id=uid,
                    document=msg.document.file_id,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                )
            else:
                await context.bot.send_message(
                    chat_id=uid,
                    text=msg.text,
                    entities=msg.entities,
                )
            sent += 1
        except RetryAfter as re_err:
            logger.warning("Rate limit hit during broadcast. Sleeping for %s seconds", re_err.retry_after)
            await asyncio.sleep(re_err.retry_after + 1)
            # Retry sending
            try:
                if msg.photo:
                    await context.bot.send_photo(
                        chat_id=uid,
                        photo=msg.photo[-1].file_id,
                        caption=msg.caption,
                        caption_entities=msg.caption_entities,
                    )
                elif msg.video:
                    await context.bot.send_video(
                        chat_id=uid,
                        video=msg.video.file_id,
                        caption=msg.caption,
                        caption_entities=msg.caption_entities,
                    )
                elif msg.animation:
                    await context.bot.send_animation(
                        chat_id=uid,
                        animation=msg.animation.file_id,
                        caption=msg.caption,
                        caption_entities=msg.caption_entities,
                    )
                elif msg.document:
                    await context.bot.send_document(
                        chat_id=uid,
                        document=msg.document.file_id,
                        caption=msg.caption,
                        caption_entities=msg.caption_entities,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=msg.text,
                        entities=msg.entities,
                    )
                sent += 1
            except Exception as e:
                err = str(e).lower()
                if "blocked" in err or "forbidden" in err or "deactivated" in err:
                    blocked += 1
                else:
                    failed += 1
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "forbidden" in err or "deactivated" in err:
                blocked += 1
            else:
                failed += 1

        # Update progress every 20 users
        if (i + 1) % 20 == 0 or (i + 1) == total:
            try:
                await progress_msg.edit_text(
                    f"📤 Broadcasting to {total} users...\n"
                    f"Progress: {i + 1}/{total}\n"
                    f"✅ Sent: {sent} | ❌ Failed: {failed} | 🚫 Blocked: {blocked}"
                )
            except Exception:
                pass

    # Final summary
    await progress_msg.edit_text(
        f"✅ <b>Broadcast Complete!</b>\n\n"
        f"📊 <b>Results:</b>\n"
        f"• Total: <b>{total}</b>\n"
        f"• ✅ Sent: <b>{sent}</b>\n"
        f"• 🚫 Blocked/left: <b>{blocked}</b>\n"
        f"• ❌ Failed: <b>{failed}</b>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /referralstats — super admin view of top referrers
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def referralstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top referrers leaderboard."""
    top = (await asyncio.to_thread(ref_db.get_top_referrers, limit=10))
    payout = (await asyncio.to_thread(ref_db.get_referral_payout_amount))

    if not top:
        await update.message.reply_text("📭 No referral conversions yet.")
        return

    lines = [f"🏆 *Top Referrers* (₹{payout:.0f}/conversion)\n"]
    for i, row in enumerate(top, 1):
        uid = row["referrer_id"]
        pro = row["pro_count"]
        earned = row["total_earned"]
        lines.append(f"{i}. `{uid}` — {pro} conversions — ₹{earned:.2f} earned")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# /editreferral — super admin: adjust a user's referral balance
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def editreferral_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "✏️ *Edit Referral Balance*\n\n"
        "Send the *User ID* of the referrer you want to edit.\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return EDITREFERRAL_USERID_WAIT


@require_superadmin
async def editreferral_userid_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        user_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID. Send a numeric Telegram user ID.")
        return EDITREFERRAL_USERID_WAIT

    stats = (await asyncio.to_thread(ref_db.get_referral_stats, user_id))
    context.user_data["ref_edit_uid"] = user_id

    await update.message.reply_text(
        f"📊 Current stats for `{user_id}`:\n"
        f"• Total referrals: *{stats['total_referrals']}*\n"
        f"• Pro conversions: *{stats['pro_referrals']}*\n"
        f"• Total earned: *₹{stats['total_earned']:.2f}*\n\n"
        f"Send the *new total earned amount* (₹) to set.\n"
        f"_(e.g. send `0` to reset after payout, or `44` to set to ₹44)_\n\n"
        f"Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return EDITREFERRAL_AMOUNT_WAIT


@require_superadmin
async def editreferral_amount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        new_amount = float(text)
        if new_amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Send a positive number like `44` or `0`.")
        return EDITREFERRAL_AMOUNT_WAIT

    user_id = context.user_data.get("ref_edit_uid")
    success = (await asyncio.to_thread(ref_db.adjust_user_earnings, user_id, new_amount))

    if success:
        await update.message.reply_text(
            f"✅ Referral balance for `{user_id}` updated to *₹{new_amount:.2f}*.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("❌ Failed to update balance. Check logs.")

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    cancel_handler = CommandHandler("cancel", cancel_command)

    # Standalone commands
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("alladmins", alladmins_command))
    application.add_handler(CommandHandler("allchannels", allchannels_command))

    # Subscription management commands
    application.add_handler(CommandHandler("admin_subscriptions", admin_subscriptions_command))
    application.add_handler(CommandHandler("grant_premium", grant_premium_command))
    application.add_handler(CommandHandler("revoke_premium", revoke_premium_command))

    # /addadmin conversation (has CallbackQueryHandler in state — per_message=False is correct)
    with warnings.catch_warnings():
        import telegram.warnings
        warnings.simplefilter("ignore", telegram.warnings.PTBUserWarning)
        application.add_handler(ConversationHandler(
            entry_points=[CommandHandler("addadmin", addadmin_start)],
            states={
                ADDADMIN_USERID_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, addadmin_userid_receive),
                ],
                ADDADMIN_DURATION_WAIT: [
                    CallbackQueryHandler(addadmin_duration_callback, pattern=r"^addadmin_dur:"),
                ],
            },
            fallbacks=[cancel_handler],
            name="addadmin",
            per_message=False,
        ))

    # /removeadmin conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("removeadmin", removeadmin_start)],
        states={
            REMOVEADMIN_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, removeadmin_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="removeadmin",
    ))

    # /addincome conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addincome", addincome_start)],
        states={
            ADDINCOME_ADMINID_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addincome_adminid_receive),
            ],
            ADDINCOME_AMOUNT_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addincome_amount_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="addincome",
    ))

    # /update conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("update", update_start)],
        states={
            UPDATE_TEXT_WAIT: [
                CallbackQueryHandler(update_key_callback, pattern=r"^update_key:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, update_text_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="update_content",
    ))

    # /broadcast conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_WAIT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION)
                    & ~filters.COMMAND,
                    broadcast_send,
                ),
            ],
        },
        fallbacks=[cancel_handler],
        name="broadcast",
    ))

    # /referralstats standalone
    application.add_handler(CommandHandler("referralstats", referralstats_command))

    # /editreferral conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("editreferral", editreferral_start)],
        states={
            EDITREFERRAL_USERID_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editreferral_userid_receive),
            ],
            EDITREFERRAL_AMOUNT_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editreferral_amount_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="editreferral",
    ))
