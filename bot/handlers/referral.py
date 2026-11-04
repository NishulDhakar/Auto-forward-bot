"""
bot/handlers/referral.py
Handles the /refer and /withdraw commands — available to ALL users.
Shows referral stats, earnings, referral link, and withdrawal info.
"""

from __future__ import annotations

import asyncio

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.db import referrals as ref_db

logger = logging.getLogger(__name__)

# Custom Premium Emojis (HTML)
telegram_emoji = "<tg-emoji emoji-id='5798505243180273024'>💎</tg-emoji>"
check_emoji = "<tg-emoji emoji-id='5215538285438311443'>✅</tg-emoji>"
yellow_emoji = "<tg-emoji emoji-id='5978986632315931621'>🟨</tg-emoji>"
rocket_star = "<tg-emoji emoji-id='5895720492190404869'>🚀</tg-emoji>"
authorize = "<tg-emoji emoji-id='5852518859268951767'>✅</tg-emoji>"
check_green = "<tg-emoji emoji-id='5852871561983299073'>👑</tg-emoji>"
crown_emoji = "<tg-emoji emoji-id='5433758796289685818'>🤖</tg-emoji>"
bot_emoji = "<tg-emoji emoji-id='5314391089514291948'>🟨</tg-emoji>"

BOT_USERNAME = "Auto_Forwarder_Official_Bot"
OWNER_USERNAME = "Savvyop"


# ─────────────────────────────────────────────────────────────────────────────
# /refer
# ─────────────────────────────────────────────────────────────────────────────

async def refer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral dashboard for any user."""
    tg_user = update.effective_user
    user_id = tg_user.id

    stats = (await asyncio.to_thread(ref_db.get_referral_stats, user_id))
    payout = (await asyncio.to_thread(ref_db.get_referral_payout_amount))
    min_withdrawal = (await asyncio.to_thread(ref_db.get_min_withdrawal))

    total = stats["total_referrals"]
    pro = stats["pro_referrals"]
    earned = stats["total_earned"]

    bot_username = context.bot.username or (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    # Withdrawal status
    if earned >= min_withdrawal:
        withdrawal_status = (
            f"{check_emoji} <b>Withdrawal Available!</b>\n"
            f"You have <b>₹{earned:.2f}</b> ready to withdraw.\n"
            f"👉 Contact @{OWNER_USERNAME} with your UPI ID to get paid!"
        )
    else:
        remaining = min_withdrawal - earned
        withdrawal_status = (
            f"⏳ <b>₹{remaining:.0f} more</b> to reach the minimum withdrawal of ₹{min_withdrawal:.0f}\n"
            f"Use /withdraw to learn how to cash out."
        )

    text = (
        f"🤝 <b>Refer & Earn</b>\n\n"
        f"Earn <b>₹{payout:.0f}</b> for every friend who buys a Pro plan!\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 <b>Your Stats</b>\n"
        f"👥 Total Referrals: <b>{total}</b>\n"
        f"⭐ Pro Conversions: <b>{pro}</b>\n"
        f"💰 Total Earned: <b>₹{earned:.2f}</b>\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🔗 <b>Your Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"📤 Share this link. When a friend buys Pro, you earn ₹{payout:.0f}!\n\n"
        f"💳 <b>Withdrawal:</b>\n"
        f"{withdrawal_status}\n\n"
        f"📌 Minimum withdrawal: <b>₹{min_withdrawal:.0f}</b> via UPI"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📤 Share My Referral Link",
            url=f"https://t.me/share/url?url={ref_link}&text=Join%20Auto%20Forwarder%20Bot%20and%20get%20real-time%20channel%20forwarding!"
        )],
        [InlineKeyboardButton("💸 How to Withdraw?", callback_data="refer_withdraw_info")],
    ])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


# ─────────────────────────────────────────────────────────────────────────────
# /withdraw
# ─────────────────────────────────────────────────────────────────────────────

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show withdrawal instructions."""
    tg_user = update.effective_user
    user_id = tg_user.id

    stats = (await asyncio.to_thread(ref_db.get_referral_stats, user_id))
    min_withdrawal = (await asyncio.to_thread(ref_db.get_min_withdrawal))
    earned = stats["total_earned"]

    if earned >= min_withdrawal:
        status_line = f"{check_emoji} <b>You are eligible to withdraw ₹{earned:.2f}!</b>"
    else:
        remaining = min_withdrawal - earned
        status_line = f"⏳ You need <b>₹{remaining:.0f} more</b> to reach the minimum (₹{min_withdrawal:.0f})"

    text = (
        f"💸 <b>Withdraw Your Earnings</b>\n\n"
        f"{status_line}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 <b>How to Withdraw:</b>\n\n"
        f"{bot_emoji} 1️⃣ Make sure your balance is ₹{min_withdrawal:.0f} or more\n"
        f"{bot_emoji} 2️⃣ Contact <b>@{OWNER_USERNAME}</b> on Telegram\n"
        f"{bot_emoji} 3️⃣ Send your <b>UPI ID</b> and <b>User ID</b> (<code>{user_id}</code>)\n"
        f"{bot_emoji} 4️⃣ Payment will be sent within <b>24 hours</b> {check_emoji}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Your current balance: <b>₹{earned:.2f}</b>\n"
        f"📌 Minimum withdrawal: <b>₹{min_withdrawal:.0f}</b>\n\n"
        f"📣 Keep referring to earn more! Use /refer to get your link."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Contact @{OWNER_USERNAME}", url=f"https://t.me/{OWNER_USERNAME}")],
        [InlineKeyboardButton("🔗 My Referral Link", callback_data="refer_show_link")],
    ])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


# ─────────────────────────────────────────────────────────────────────────────
# Callback for inline buttons
# ─────────────────────────────────────────────────────────────────────────────

async def refer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "refer_withdraw_info":
        await withdraw_command(update, context)
    elif query.data == "refer_show_link":
        user_id = query.from_user.id
        bot_username = context.bot.username or (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        await query.message.reply_text(
            f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>",
            parse_mode="HTML",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    from telegram.ext import CallbackQueryHandler
    application.add_handler(CommandHandler("refer", refer_command))
    application.add_handler(MessageHandler(filters.Regex("^🤝 REFER$"), refer_command))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(MessageHandler(filters.Regex("^💰 WITHDRAW$"), withdraw_command))
    application.add_handler(CallbackQueryHandler(refer_callback, pattern=r"^refer_"))
