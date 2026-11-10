"""Offline checks for plan-aware agent behavior."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.update(
    BOT_TOKEN="123456:offline-test-token",
    SUPER_ADMIN_ID="1",
    SUPABASE_URL="https://example.supabase.co",
    SUPABASE_KEY="offline-key",
    TELEGRAM_API_ID="12345",
    TELEGRAM_API_HASH="offline-hash",
    GROQ_API_KEY="",
)

from bot.handlers.agent import _account_context, _detect_setup_command, _fallback_answer, _is_paid
from bot.utils.groq_agent import build_system_prompt


class AgentPlanTests(unittest.TestCase):
    def test_active_paid_user_gets_paid_context(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=10)
        user = {"role": "admin", "subscription_end": expiry.isoformat(), "subscription_plan": "1_month"}

        self.assertTrue(_is_paid(22, user))
        context = _account_context(22, user)
        self.assertIn("paid and active", context)
        self.assertIn("1_month", context)
        self.assertIn("/addsource", build_system_prompt(context))

    def test_expired_user_gets_free_context_and_upgrade_prompt(self):
        expiry = datetime.now(timezone.utc) - timedelta(days=1)
        user = {"role": "admin", "subscription_end": expiry.isoformat(), "trial_start": None}

        self.assertFalse(_is_paid(22, user))
        self.assertIn("Access mode: free", _account_context(22, user))
        self.assertIn("/pro", _fallback_answer(False))

    def test_super_admin_is_always_paid(self):
        user = {"role": "user", "subscription_end": None}
        self.assertTrue(_is_paid(1, user))
        self.assertIn("unlimited", _account_context(1, user))


class AgentHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_message_is_sent_with_current_plan_to_groq(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from bot.handlers.agent import handle_agent_message

        message = SimpleNamespace(text="How do I add a source?", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(type="private", id=22),
            effective_user=SimpleNamespace(id=22, username="free_user"),
        )
        context = SimpleNamespace(
            user_data={},
            bot=SimpleNamespace(send_chat_action=AsyncMock()),
        )
        free_user = {"role": "user", "trial_start": None, "subscription_end": None}

        with patch("bot.handlers.agent.users_db.get_or_create_user", return_value=free_user), \
             patch("bot.handlers.agent.ask_agent", new=AsyncMock(return_value="Use /pro first.")) as ask:
            await handle_agent_message(update, context)

        self.assertEqual(message.reply_text.await_args.args[0], "Use /pro first.")
        prompt = ask.await_args.args[0][0]["content"]
        self.assertIn("Access mode: free", prompt)

    async def test_paid_setup_phrase_runs_target_conversation(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from telegram import Chat, Message, Update, User
        from bot.handlers.agent import handle_agent_message

        bot = SimpleNamespace(send_chat_action=AsyncMock())
        message = Message(
            message_id=10,
            date=datetime.now(timezone.utc),
            chat=Chat(22, "private"),
            from_user=User(22, "paid_user", False),
            text="Help me to add my channel",
        )
        message.set_bot(bot)
        update = Update(10, message=message)
        update.set_bot(bot)
        context = SimpleNamespace(
            user_data={},
            bot=bot,
            application=SimpleNamespace(process_update=AsyncMock()),
        )
        paid_user = {
            "role": "admin",
            "subscription_end": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
            "subscription_plan": "1_month",
        }

        with patch("bot.handlers.agent.users_db.get_or_create_user", return_value=paid_user):
            await handle_agent_message(update, context)

        context.application.process_update.assert_awaited_once()
        synthetic = context.application.process_update.await_args.args[0]
        self.assertEqual(synthetic.effective_message.text, "/addtarget")


if __name__ == "__main__":
    unittest.main()
