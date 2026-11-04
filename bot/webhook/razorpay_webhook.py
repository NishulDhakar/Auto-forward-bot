"""
bot/webhook/razorpay_webhook.py
Production-ready Razorpay webhook server.

Architecture:
  - Runs an aiohttp HTTP server in the SAME asyncio event loop as PTB polling.
  - Started in post_init, stopped in post_stop — no extra threads needed.
  - Listens on POST /webhooks/razorpay

Security:
  - HMAC-SHA256 signature verification on every request (constant-time compare).
  - Deduplication via `processed_webhooks` table — safe against Razorpay retries.
  - Returns 200 OK for all non-error states to stop Razorpay retry loops.

Events handled:
  - payment.captured → activate subscription, notify user via Telegram
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Optional

from aiohttp import web
from telegram import Bot

from bot.config import (
    RAZORPAY_WEBHOOK_SECRET,
    WEBHOOK_PORT,
    SUPER_ADMIN_ID,
)
from bot.db import subscriptions as subs_db
from bot.db import users as users_db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Plan registry — maps Razorpay note value → subscription duration
# Uses the same canonical keys and prices as the payment UI
# ─────────────────────────────────────────────────────────────────────────────

from bot.payment_plans import PLANS
PLAN_REGISTRY = PLANS

# ─────────────────────────────────────────────────────────────────────────────
# Module-level state (set during server startup)
# ─────────────────────────────────────────────────────────────────────────────

_runner: Optional[web.AppRunner] = None
_bot_instance: Optional[Bot] = None


# ─────────────────────────────────────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────────────────────────────────────

def _verify_signature(raw_body: bytes, razorpay_signature: str) -> bool:
    """
    Verify the X-Razorpay-Signature header using HMAC-SHA256.
    Uses hmac.compare_digest to prevent timing attacks.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.error("Webhook rejected: RAZORPAY_WEBHOOK_SECRET is missing")
        return False

    if not isinstance(razorpay_signature, str) or len(razorpay_signature) != 64 or not razorpay_signature.isascii():
        return False

    expected_sig = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison prevents timing-based attacks
    return hmac.compare_digest(expected_sig, razorpay_signature)


# ─────────────────────────────────────────────────────────────────────────────
# Payment activation
# ─────────────────────────────────────────────────────────────────────────────

# Telegram notification
# ─────────────────────────────────────────────────────────────────────────────

async def _notify_user(telegram_id: int, plan_key: str, amount_inr: float, expires_at: datetime) -> None:
    """Send a confirmation DM to the user and a summary to the super admin."""
    if not _bot_instance:
        logger.warning("Bot not initialised — cannot send Telegram notification.")
        return

    plan_label = PLAN_REGISTRY.get(plan_key, {}).get("label", plan_key)

    # ── User notification ──────────────────────────────────────────────────
    try:
        await _bot_instance.send_message(
            chat_id=telegram_id,
            text=(
                "🎉 *Payment Successful!*\n\n"
                "Your premium membership has been activated.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Plan: *{plan_label}*\n"
                f"💰 Amount: *₹{amount_inr:.0f} INR*\n"
                f"📅 Expires: `{expires_at.strftime('%d/%m/%Y')}`\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "✅ You now have full admin access.\n"
                "Use /start to see all your commands.\n\n"
                "Thank you for your support ❤️"
            ),
            parse_mode="Markdown",
        )
        logger.info("Sent activation confirmation to telegram_id=%s", telegram_id)
    except Exception as exc:
        logger.warning("Could not DM user %s: %s", telegram_id, exc)

    # ── Super admin summary ───────────────────────────────────────────────
    try:
        user = await asyncio.to_thread(users_db.get_user, telegram_id)
        username = f"@{user['username']}" if user and user.get("username") else str(telegram_id)
        await _bot_instance.send_message(
            chat_id=SUPER_ADMIN_ID,
            text=(
                "💰 *Razorpay Payment Confirmed*\n\n"
                f"👤 User: {username} (`{telegram_id}`)\n"
                f"📦 Plan: *{plan_label}*\n"
                f"💵 Amount: *₹{amount_inr:.0f} INR*\n"
                f"📅 Expires: `{expires_at.strftime('%d/%m/%Y')}`\n\n"
                "_Activated automatically via webhook._"
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify super admin: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Request handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_razorpay_webhook(request: web.Request) -> web.Response:
    """
    POST /webhooks/razorpay

    Steps:
      1. Read raw body (required for signature verification)
      2. Verify HMAC-SHA256 signature
      3. Parse JSON payload
      4. Filter to payment.captured only
      5. Extract telegram_id, plan, payment details from notes
      6. Deduplicate using processed_webhooks table
      7. Activate subscription
      8. Notify user via Telegram
    """
    # ── 1. Read raw body ──────────────────────────────────────────────────
    raw_body = await request.read()

    # ── 2. Signature verification ─────────────────────────────────────────
    razorpay_sig = request.headers.get("X-Razorpay-Signature", "")
    if not _verify_signature(raw_body, razorpay_sig):
        logger.warning(
            "Webhook signature mismatch from %s — rejected",
            request.remote,
        )
        return web.Response(status=400, text="Invalid signature")

    # ── 3. Parse JSON ─────────────────────────────────────────────────────
    try:
        payload: dict = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Malformed webhook JSON: %s", exc)
        return web.Response(status=400, text="Invalid JSON")

    if not isinstance(payload, dict):
        return web.Response(status=400, text="Invalid payload")
    event: str = payload.get("event", "")
    logger.info("Razorpay webhook received: event=%s from=%s", event, request.remote)

    # ── 4. Only handle payment.captured ──────────────────────────────────
    if event != "payment.captured":
        logger.debug("Ignoring unhandled event: %s", event)
        return web.Response(status=200, text="Event ignored")

    # ── 5. Extract payment data ───────────────────────────────────────────
    try:
        entity: dict = payload["payload"]["payment"]["entity"]
        payment_id: str = entity["id"]
        order_id: str = entity.get("order_id") or ""
        amount_paise = entity.get("amount")
        if type(amount_paise) is not int:
            raise ValueError("Invalid amount")
        amount_inr: float = amount_paise / 100
        notes: dict = entity.get("notes") or {}

        telegram_id_str: str = str(notes.get("telegram_id", "")).strip()
        plan_key: str = str(notes.get("plan", "")).strip().lower()

    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        logger.error("Failed to parse payment.captured payload: %s", type(exc).__name__)
        # Return 200 so Razorpay stops retrying a malformed payload
        return web.Response(status=200, text="Malformed payload — logged")

    # Validate extracted values
    if not telegram_id_str or not telegram_id_str.isdigit():
        logger.error("Invalid telegram_id in notes: %r — payment_id=%s", telegram_id_str, payment_id)
        return web.Response(status=200, text="Invalid telegram_id in notes")

    if plan_key not in PLAN_REGISTRY:
        logger.error("Unknown plan key %r — payment_id=%s", plan_key, payment_id)
        return web.Response(status=200, text="Unknown plan key")

    telegram_id = int(telegram_id_str)

    logger.info(
        "payment.captured — payment_id=%s telegram_id=%s plan=%s amount=₹%.2f",
        payment_id, telegram_id, plan_key, amount_inr,
    )

    if (not isinstance(payment_id, str) or not payment_id.startswith("pay_")
        or entity.get("currency") != "INR" or entity.get("status") != "captured"
        or amount_paise != PLANS[plan_key]["inr"] * 100 or telegram_id <= 0):
        return web.Response(status=400, text="Payment validation failed")

    try:
        result = await asyncio.to_thread(subs_db.activate_razorpay_payment,
                                        payment_id, telegram_id, plan_key, amount_paise, order_id)
        expires_at = datetime.fromisoformat(result["expires_at"])
    except Exception:
        logger.exception("Payment activation failed for payment %s; provider may retry", payment_id)
        return web.Response(status=500, text="Activation error")
    from bot.utils.userbot_manager import invalidate_cache
    invalidate_cache(telegram_id)
    if not result["activated"]:
        return web.Response(status=200, text="Already processed")

    # ── 8. Notify via Telegram ────────────────────────────────────────────
    await _notify_user(telegram_id, plan_key, amount_inr, expires_at)

    return web.Response(status=200, text="OK")


# ─────────────────────────────────────────────────────────────────────────────
# Server lifecycle — called from bot/main.py post_init / post_stop
# ─────────────────────────────────────────────────────────────────────────────

async def start_webhook_server(bot: Bot) -> None:
    """
    Start the aiohttp webhook server in the running asyncio event loop.
    Called from PTB's post_init hook so no extra thread is needed.
    """
    global _runner, _bot_instance
    _bot_instance = bot

    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning(
            "RAZORPAY_WEBHOOK_SECRET is not set. "
            "Payment webhook is disabled until a verification secret is configured."
        )

    app = web.Application(client_max_size=256 * 1024)
    if RAZORPAY_WEBHOOK_SECRET:
        app.router.add_post("/webhooks/razorpay", handle_razorpay_webhook)
    # Health-check endpoint (useful for load balancers / uptime monitors)
    app.router.add_get("/health", lambda _req: web.Response(text="OK"))

    _runner = web.AppRunner(
        app,
        access_log=logging.getLogger("aiohttp.access"),
    )
    await _runner.setup()

    site = web.TCPSite(_runner, host="0.0.0.0", port=WEBHOOK_PORT)
    await site.start()

    logger.info(
        "Razorpay webhook server listening on 0.0.0.0:%s  →  POST /webhooks/razorpay",
        WEBHOOK_PORT,
    )


async def stop_webhook_server() -> None:
    """Gracefully shut down the webhook server. Called from PTB's post_stop hook."""
    global _runner
    if _runner is not None:
        logger.info("Stopping Razorpay webhook server...")
        await _runner.cleanup()
        _runner = None
        logger.info("Razorpay webhook server stopped.")
