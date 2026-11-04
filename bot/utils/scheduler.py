"""
bot/utils/scheduler.py
APScheduler integration — manages scheduled messages and subscription expiry checks.

Design:
- Uses AsyncIOScheduler (runs in the same event loop as PTB)
- On startup, bootstraps all active DB schedules as APScheduler jobs
- Runs a subscription-expiry check every hour
- Each scheduled message job sends content to all of the admin's target channels
"""

from __future__ import annotations

import asyncio
import logging

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot

from bot.db.channels import get_target_channels
from bot.db.schedules import deactivate_schedule, get_all_active_schedules
from bot.db.users import get_expired_admins, revoke_subscription
from bot.utils.delivery import send_with_retry

logger = logging.getLogger(__name__)

# Singleton scheduler instance
_scheduler: AsyncIOScheduler | None = None

# Timezone for all cron jobs (bot operates in UTC)
TZ = pytz.utc


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=TZ)
    return _scheduler


def start_scheduler() -> None:
    """Start the APScheduler. Must be called after the event loop is running."""
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started.")


# ─────────────────────────────────────────────────────────────────────────────
# Scheduled message jobs
# ─────────────────────────────────────────────────────────────────────────────

async def _send_scheduled_message(
    bot: Bot,
    admin_id: int,
    schedule_id: str,
    content: str,
    frequency: str,
) -> None:
    """Job callback: send content to all of admin's target channels."""
    from bot.utils import userbot_manager
    is_working = await userbot_manager.is_admin_working(admin_id)
    if not is_working:
        logger.info("Schedule %s fired but admin %s has stopped the bot (/stop). Skipping.", schedule_id, admin_id)
        return

    targets = await asyncio.to_thread(get_target_channels, admin_id)
    if not targets:
        logger.warning("Schedule %s fired but admin %s has no target channels.", schedule_id, admin_id)
        return

    failed = False
    for target in targets:
        try:
            await send_with_retry(lambda: bot.send_message(chat_id=target["channel_id"], text=content))
        except Exception as exc:
            failed = True
            logger.error(
                "Failed to send scheduled msg %s to channel %s: %s",
                schedule_id, target["channel_id"], exc,
            )

    # For one-time jobs, deactivate after first fire
    if frequency == "once" and not failed:
        await asyncio.to_thread(deactivate_schedule, schedule_id)
        _remove_job_if_exists(f"sched_{schedule_id}")
        logger.info("One-time schedule %s deactivated.", schedule_id)


def schedule_message(
    bot: Bot,
    schedule_id: str,
    admin_id: int,
    content: str,
    post_time: str,   # "HH:MM"
    frequency: str,   # "once" | "daily"
) -> None:
    """
    Register an APScheduler cron job for a scheduled message.
    post_time: "HH:MM" in UTC.
    """
    scheduler = get_scheduler()
    parts = post_time.split(":")
    hour, minute = int(parts[0]), int(parts[1])
    job_id = f"sched_{schedule_id}"

    _remove_job_if_exists(job_id)

    if frequency == "daily":
        trigger = CronTrigger(hour=hour, minute=minute, timezone=TZ)
    else:  # once
        trigger = CronTrigger(hour=hour, minute=minute, timezone=TZ)

    scheduler.add_job(
        _send_scheduled_message,
        trigger=trigger,
        id=job_id,
        kwargs={
            "bot": bot,
            "admin_id": admin_id,
            "schedule_id": schedule_id,
            "content": content,
            "frequency": frequency,
        },
        replace_existing=True,
        misfire_grace_time=60,
    )
    logger.info("Scheduled job %s (admin=%s, time=%s, freq=%s).", job_id, admin_id, post_time, frequency)


def remove_scheduled_job(schedule_id: str) -> None:
    """Remove an APScheduler job by schedule UUID."""
    _remove_job_if_exists(f"sched_{schedule_id}")


def _remove_job_if_exists(job_id: str) -> None:
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass  # Job may not exist yet


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap on startup
# ─────────────────────────────────────────────────────────────────────────────

async def bootstrap_schedules(bot: Bot) -> None:
    """
    Reload all active DB schedules into APScheduler.
    Call this once after the scheduler is started.
    """
    schedules = await asyncio.to_thread(get_all_active_schedules)
    for sched in schedules:
        schedule_message(
            bot=bot,
            schedule_id=sched["id"],
            admin_id=sched["admin_id"],
            content=sched["content"],
            post_time=sched["post_time"],
            frequency=sched["frequency"],
        )
    logger.info("Bootstrapped %d scheduled message(s).", len(schedules))


# ─────────────────────────────────────────────────────────────────────────────
# Subscription expiry checker
# ─────────────────────────────────────────────────────────────────────────────

async def _check_expired_subscriptions(bot: Bot) -> None:
    """
    Runs every hour. Demotes admins whose subscription_end has passed.
    Clears all subscription fields and sends a notification to the user.
    """
    expired = await asyncio.to_thread(get_expired_admins)
    for user in expired:
        user_id = user["user_id"]
        await asyncio.to_thread(revoke_subscription, user_id)
        from bot.utils import userbot_manager
        userbot_manager.invalidate_cache(user_id)
        await userbot_manager.stop_userbot(user_id)
        logger.info("Auto-demoted admin %s (subscription expired).", user_id)
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ *Your premium membership has expired.*\n\n"
                    "Renew to continue using premium features.\n"
                    "Use /pro to view plans and upgrade."
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.warning("Could not notify expired admin %s: %s", user_id, exc)


def start_expiry_checker(bot: Bot) -> None:
    """Register the hourly subscription expiry check job."""
    scheduler = get_scheduler()
    scheduler.add_job(
        _check_expired_subscriptions,
        trigger=CronTrigger(minute=0, timezone=TZ),  # Every hour on the hour
        id="expiry_checker",
        kwargs={"bot": bot},
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("Subscription expiry checker registered (hourly).")


def stop_scheduler() -> None:
    """Stop producing jobs before shutting down Telegram clients."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
