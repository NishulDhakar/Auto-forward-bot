"""
bot/handlers/payment.py
Handles the full /pro premium subscription flow:
  - Plan selection (1 Month, 3 Months, 6 Months)
  - Payment method selection (INR via Razorpay / USDT crypto)
  - Razorpay payment link generation + payment link ID stored in DB
  - "I've Paid" → auto-checks Razorpay API → if confirmed, activates subscription instantly
  - USDT → mandatory manual review by super admin
"""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, SUPER_ADMIN_ID
from bot.db import subscriptions as subs_db
from bot.db import users as users_db

logger = logging.getLogger(__name__)

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


# ─────────────────────────────────────────────────────────────────────────────
# Plan definitions
# ─────────────────────────────────────────────────────────────────────────────

from bot.payment_plans import PLANS

# USDT wallet addresses
USDT_BEP20 = "0x565f64c9edc74e60ae4ca24c816b25d10dd9bdf6"
USDT_TRC20 = "TMjuAXzEfvLuzxQv9CrtPWy4m6jA2QxvHf"
BINANCE_ID = "212753448"
SUPPORT_USERNAME = "@savvyop"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _razorpay_available() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def _get_razorpay_client():
    """Return a configured Razorpay client or None."""
    if not _razorpay_available():
        return None
    try:
        import razorpay  # type: ignore
        return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    except Exception as exc:
        logger.error("Could not create Razorpay client: %s", exc)
        return None


def _create_razorpay_link(plan_key: str, user_id: int) -> tuple[Optional[str], Optional[str]]:
    """
    Create a Razorpay Payment Link.
    Returns (short_url, payment_link_id) or (None, None) on failure.
    """
    client = _get_razorpay_client()
    if not client:
        return None, None
    try:
        plan = PLANS[plan_key]
        data = {
            "amount": plan["inr"] * 100,  # paise
            "currency": "INR",
            "description": f"Premium Membership – {plan['label']}",
            "notes": {
                "telegram_id": str(user_id),
                "plan": plan_key,
            },
        }
        resp = client.payment_link.create(data)
        return resp.get("short_url"), resp.get("id")
    except Exception as exc:
        logger.error("Razorpay link creation failed: %s", exc)
        return None, None


def _check_razorpay_payment(payment_link_id: str, user_id: int, plan_key: str) -> dict | None:
    """Verify ownership, pricing and captured payment directly with Razorpay."""
    client = _get_razorpay_client()
    if not client:
        return None
    link = client.payment_link.fetch(payment_link_id)
    notes = link.get("notes") or {}
    if (link.get("status") != "paid" or link.get("currency") != "INR"
        or link.get("amount") != PLANS[plan_key]["inr"] * 100
        or str(notes.get("telegram_id")) != str(user_id) or notes.get("plan") != plan_key):
        return None
    for item in link.get("payments", []):
        payment_id = item.get("payment_id")
        if not payment_id:
            continue
        payment = client.payment.fetch(payment_id)
        if (payment.get("status") == "captured" and payment.get("currency") == "INR"
            and payment.get("amount") == PLANS[plan_key]["inr"] * 100):
            return payment
    return None


def _build_plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, plan in PLANS.items():
        emoji = "1️⃣" if key == "1_month" else "3️⃣" if key == "3_months" else "6️⃣"
        rows.append([
            InlineKeyboardButton(
                f"{emoji} {plan['label']} — ₹{plan['inr']} INR  |  USDT {plan['usdt']}",
                callback_data=f"plan_select:{key}",
            )
        ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="plan_cancel")])
    return InlineKeyboardMarkup(rows)


def _build_payment_method_keyboard(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 INR Payment (UPI / Razorpay)", callback_data=f"pay_inr:{plan_key}")],
        [InlineKeyboardButton("₮ USDT Payment (Crypto)", callback_data=f"pay_usdt:{plan_key}")],
        [InlineKeyboardButton("⬅️ Back to Plans", callback_data="plan_back")],
    ])


_PLANS_TEXT = (
    f"<b>{telegram_emoji} Premium Membership</b>\n\n"
    f"Unlock real-time channel forwarding, text filters,\n"
    f"scheduled messages and much more!\n\n"
    f"📋 <b>Choose your plan:</b>\n\n"
    f"1️⃣ <b>1 Month</b> — ₹88 INR\n"
    f"<i>(≈ ₹2.9/day • USDT 2)</i>\n\n"
    f"3️⃣ <b>3 Months</b> — ₹250 INR\n"
    f"<i>(≈ ₹2.7/day • USDT 5)</i>\n\n"
    f"6️⃣ <b>6 Months</b> — ₹500 INR\n"
    f"<i>(≈ ₹2.7/day • USDT 10)</i>"
)


# ─────────────────────────────────────────────────────────────────────────────
# /pro command
# ─────────────────────────────────────────────────────────────────────────────

async def pro_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the premium membership plan selection UI."""
    tg_user = update.effective_user
    user = (await asyncio.to_thread(users_db.get_or_create_user, tg_user.id, tg_user.username))

    if user["role"] in ("admin", "superadmin"):
        sub_end_str = user.get("subscription_end")
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)
        days_left = max(0, (sub_end - _now()).days) if sub_end else 0
        plan_name = (user.get("subscription_plan") or "—").replace("_", " ").title()

        await update.message.reply_text(
            f"<b>{check_emoji} You already have an active premium membership!</b>\n\n"
            f"📦 Plan: <b>{plan_name}</b>\n"
            f"📅 Expires: <code>{sub_end.strftime('%d/%m/%Y') if sub_end else 'N/A'}</code>\n"
            f"⏳ Days remaining: <b>{days_left}</b>\n\n"
            f"💬 To renew or for any issues, contact {SUPPORT_USERNAME}",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        _PLANS_TEXT,
        parse_mode="HTML",
        reply_markup=_build_plans_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: plan selected / back / cancel
# ─────────────────────────────────────────────────────────────────────────────

async def plan_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    plan_key = query.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    context.user_data["selected_plan"] = plan_key

    await query.edit_message_text(
        f"<b>{telegram_emoji} Premium Membership</b>\n\n"
        f"📦 Selected plan: <b>{plan['label']}</b>\n"
        f"💰 INR: <b>₹{plan['inr']}</b> ({plan['per_day_inr']})\n"
        f"₮ USDT: <b>{plan['usdt']} USDT</b>\n\n"
        f"Choose payment method:",
        parse_mode="HTML",
        reply_markup=_build_payment_method_keyboard(plan_key),
    )


async def plan_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("selected_plan", None)
    await query.edit_message_text(
        _PLANS_TEXT,
        parse_mode="HTML",
        reply_markup=_build_plans_keyboard(),
    )


async def plan_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("selected_plan", None)
    await query.edit_message_text(
        f"{check_emoji} No problem! Use /pro anytime to view premium plans and upgrade.\n\n"
        f"💬 Have questions? Contact {SUPPORT_USERNAME}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: INR payment — generate Razorpay link, store link ID in DB
# ─────────────────────────────────────────────────────────────────────────────

async def pay_inr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("⏳ Generating payment link...")

    plan_key = query.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    user_id = query.from_user.id

    payment_url, payment_link_id = (await asyncio.to_thread(_create_razorpay_link, plan_key, user_id))

    # Store pending record — with razorpay link ID if available
    (await asyncio.to_thread(subs_db.create_subscription_record,
        telegram_id=user_id,
        plan_name=plan_key,
        amount=plan["inr"],
        payment_method="inr",
        razorpay_order_id=payment_link_id,  # None if no Razorpay keys
    ))

    if payment_url and payment_link_id:
        # Razorpay available — show payment link + auto-verify button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Pay Now — ₹{plan['inr']}", url=payment_url)],
            [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid_check:{plan_key}:{payment_link_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"plan_select:{plan_key}")],
        ])
        await query.edit_message_text(
            f"{money_emoji} <b>INR Payment — {plan['label']}</b>\n\n"
            f"Amount: <b>₹{plan['inr']}</b>\n\n"
            f"{dash_emoji} Tap <b>Pay Now</b> to complete your payment securely via Razorpay\n"
            f"{dash_emoji} Come back here and tap <b>I've Paid</b>\n\n"
            f"{check_emoji} Your subscription will be activated <b>automatically</b> once payment is confirmed.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 Having trouble? Contact {SUPPORT_USERNAME}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        # No Razorpay keys — fallback to manual UPI contact flow
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Contact {SUPPORT_USERNAME} for UPI", url="https://t.me/savvyop")],
            [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid_check:{plan_key}:none")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"plan_select:{plan_key}")],
        ])
        await query.edit_message_text(
            f"{money_emoji} <b>INR Payment — {plan['label']}</b>\n\n"
            f"Amount: <b>₹{plan['inr']}</b>\n\n"
            f"🇮🇳 <b>How to pay via UPI:</b>\n"
            f"Contact {SUPPORT_USERNAME} to receive the UPI QR / ID.\n\n"
            f"After completing your payment:\n"
            f"{dash_emoji} Send a screenshot to {SUPPORT_USERNAME}\n"
            f"{dash_emoji} Tap <b>I've Paid</b> below\n\n"
            f"<i>{shield_emoji} Activation within a few minutes after verification.</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: USDT payment
# ─────────────────────────────────────────────────────────────────────────────

async def pay_usdt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    plan_key = query.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    user_id = query.from_user.id
    username = query.from_user.username or str(user_id)

    # Store pending record for USDT
    (await asyncio.to_thread(subs_db.create_subscription_record,
        telegram_id=user_id,
        plan_name=plan_key,
        amount=plan["usdt"],
        payment_method="usdt",
    ))

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Contact {SUPPORT_USERNAME}", url="https://t.me/savvyop")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"plan_select:{plan_key}")],
    ])

    await query.edit_message_text(
        f"{money_emoji} <b>USDT Payment — {plan['label']}</b>\n\n"
        f"Amount: <b>{plan['usdt']} USDT</b>\n\n"
        f"Send payment to one of the following addresses:\n\n"
        f"🔷 <b>BEP20 (BSC Network):</b>\n"
        f"<code>{USDT_BEP20}</code>\n\n"
        f"🔴 <b>TRC20 (TRON Network):</b>\n"
        f"<code>{USDT_TRC20}</code>\n\n"
        f"🟡 <b>Binance Pay ID:</b>\n"
        f"<code>{BINANCE_ID}</code>\n\n"
        f"<i>Tap any address above to copy it</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📩 <b>After payment, contact {SUPPORT_USERNAME} with:</b>\n"
        f"{dash_emoji} Transaction Hash / TxID\n"
        f"{dash_emoji} Plan: <b>{plan['label']}</b>\n"
        f"{dash_emoji} Your username: @{username}\n\n"
        f"<i>{shield_emoji} Your membership will be activated after verification</i> ⏱",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: "I've Paid" — auto-verify via Razorpay API, then activate
# ─────────────────────────────────────────────────────────────────────────────

async def paid_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Smart payment verification:
    - If Razorpay link ID present → check Razorpay API directly
    - If confirmed paid → activate user immediately (no admin needed)
    - If not paid → prompt to pay / contact support
    - If no Razorpay keys (UPI fallback) → store pending, notify admin
    """
    query = update.callback_query
    await query.answer("🔍 Checking your payment...")

    parts = query.data.split(":", 2)
    plan_key = parts[1]
    payment_link_id = parts[2] if len(parts) > 2 else "none"

    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    user_id = query.from_user.id
    username = query.from_user.username or str(user_id)

    # ── Case 1: Razorpay available with a real link ID ─────────────────────
    if payment_link_id and payment_link_id != "none" and _razorpay_available():
        payment = await asyncio.to_thread(_check_razorpay_payment, payment_link_id, user_id, plan_key)

        if payment:
            # The button and webhook share the same payment-ID transaction.
            result = await asyncio.to_thread(subs_db.activate_razorpay_payment,
                payment["id"], user_id, plan_key, payment["amount"], payment_link_id)
            expires_at = datetime.fromisoformat(result["expires_at"])
            from bot.utils.userbot_manager import invalidate_cache
            invalidate_cache(user_id)

            await query.edit_message_text(
                f"🎉 <b>Payment Successful!</b>\n\n"
                f"Your premium membership has been activated.\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Plan: <b>{plan['label']}</b>\n"
                f"📅 Expires: <code>{expires_at.strftime('%d/%m/%Y')}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{check_emoji} Use /start to see all your admin commands.\n\n"
                f"Thank you for your support {telegram_emoji}",
                parse_mode="HTML",
            )
            logger.info("Auto-activated user %s on plan %s via Razorpay.", user_id, plan_key)
            return

        else:
            # ❌ Not paid yet
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Pay Now — ₹{plan['inr']}", callback_data=f"pay_inr:{plan_key}")],
                [InlineKeyboardButton(f"💬 Help — Contact {SUPPORT_USERNAME}", url="https://t.me/savvyop")],
            ])
            await query.edit_message_text(
                f"❌ <b>Payment Not Confirmed</b>\n\n"
                f"We could not find a completed payment for your account.\n\n"
                f"<b>Possible reasons:</b>\n"
                f"{dash_emoji} Payment is still processing (wait 1–2 min and try again)\n"
                f"{dash_emoji} Payment was not completed\n"
                f"{dash_emoji} You paid from a different session\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"If you've already paid and still see this message,\n"
                f"please contact {SUPPORT_USERNAME} with your payment screenshot.\n"
                f"We'll activate your account manually. 🙏",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

    # ── Case 2: No Razorpay / UPI manual flow ──────────────────────────────
    # Check DB for an existing paid subscription record
    pending = (await asyncio.to_thread(subs_db.get_pending_subscription_by_user, user_id))

    if pending and pending.get("payment_status") == "paid":
        # Already activated by admin previously — just confirm
        user = (await asyncio.to_thread(users_db.get_user, user_id))
        sub_end_str = user.get("subscription_end") if user else None
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)

        await query.edit_message_text(
            f"<b>{check_emoji} You are already subscribed!</b>\n\n"
            f"📦 Plan: <b>{plan['label']}</b>\n"
            f"📅 Expires: <code>{sub_end.strftime('%d/%m/%Y') if sub_end else 'N/A'}</code>\n\n"
            f"Use /start to see all your commands.",
            parse_mode="HTML",
        )
        return

    # No record confirmed — notify admin and tell user to wait
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Contact {SUPPORT_USERNAME}", url="https://t.me/savvyop")],
    ])
    await query.edit_message_text(
        f"📨 <b>Payment Notification Sent!</b>\n\n"
        f"📦 Plan: <b>{plan['label']}</b>\n"
        f"💰 Amount: <b>₹{plan['inr']} INR</b>\n\n"
        f"The admin has been notified and will activate your account shortly.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <b>Please wait</b> — activation usually takes a few minutes.\n\n"
        f"💬 Having trouble? Contact {SUPPORT_USERNAME}\n"
        f"<i>Send your payment screenshot for faster verification.</i>\n\n"
        f"Thank you for your support {telegram_emoji}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    # Notify super admin with one-tap grant command
    try:
        await context.bot.send_message(
            chat_id=SUPER_ADMIN_ID,
            text=(
                f"🔔 <b>New INR Payment — Manual Verification Needed</b>\n\n"
                f"👤 User: @{username} (<code>{user_id}</code>)\n"
                f"📦 Plan: <b>{plan['label']}</b>\n"
                f"💰 Amount: <b>₹{plan['inr']}</b>\n\n"
                f"After verifying payment, run:\n"
                f"<code>/grant_premium {user_id} {plan['days']}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Could not notify super admin: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    application.add_handler(CommandHandler("pro", pro_command))
    application.add_handler(MessageHandler(filters.Regex("^💎 PRO$"), pro_command))
    application.add_handler(CallbackQueryHandler(plan_select_callback, pattern=r"^plan_select:"))
    application.add_handler(CallbackQueryHandler(plan_back_callback, pattern=r"^plan_back$"))
    application.add_handler(CallbackQueryHandler(plan_cancel_callback, pattern=r"^plan_cancel$"))
    application.add_handler(CallbackQueryHandler(pay_inr_callback, pattern=r"^pay_inr:"))
    application.add_handler(CallbackQueryHandler(pay_usdt_callback, pattern=r"^pay_usdt:"))
    application.add_handler(CallbackQueryHandler(paid_check_callback, pattern=r"^paid_check:"))
