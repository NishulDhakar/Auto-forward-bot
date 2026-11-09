"""Small async-safe Groq chat-completions client used by the support agent."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from bot.config import GROQ_API_KEY, GROQ_MODEL

logger = logging.getLogger(__name__)


class AgentUnavailable(RuntimeError):
    """Raised when Groq is not configured or does not return a usable answer."""


def build_system_prompt(plan_context: str) -> str:
    """Build the agent policy with the user's current access level."""
    return (
        "You are the Auto Forward Bot support and setup agent. "
        "Help the user solve their Telegram forwarding problem with short, concrete steps.\n\n"
        f"CURRENT ACCOUNT CONTEXT:\n{plan_context}\n\n"
        "POLICY:\n"
        "- Start by understanding the user's problem. If the request is vague, ask one clear question.\n"
        "- Never claim that an action was completed unless the bot has explicitly confirmed it.\n"
        "- Never ask for a Telegram OTP, Telegram password, bot token, API key, or payment credentials.\n"
        "- The existing bot commands perform setup. For a paid user, guide them through the next command "
        "one step at a time: /authorize, /addsource, /addtarget, /filter, /schedule, /work, and /mystatus. "
        "Explain what input each command expects and what to do when Telegram shows a picker.\n"
        "- A source is where messages are copied from; targets are destinations. The bot must be an admin "
        "in target channels. Filters can replace links/usernames/text or block keywords/APK files.\n"
        "- For a free user, answer general questions and explain the workflow, but clearly say that real-time "
        "forwarding and setup commands require an active paid plan. Encourage /pro without being deceptive or pushy.\n"
        "- If the user asks about billing, direct them to /pro and /plan. Do not invent prices, payment status, "
        "or subscription expiry.\n"
        "- Do not tell a free user that a paid-only command will work. Do not tell a paid user to purchase again "
        "unless their account context says the subscription is inactive.\n"
        "- Use plain text suitable for Telegram. Keep replies below 3500 characters."
    )


def _request(messages: list[dict[str, str]]) -> str:
    if not GROQ_API_KEY:
        raise AgentUnavailable("GROQ_API_KEY is not configured")

    payload = json.dumps({
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 700,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body: Any = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Groq agent request failed: %s", exc)
        raise AgentUnavailable("Groq did not return an answer") from exc

    try:
        answer = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Groq response did not contain assistant content")
        raise AgentUnavailable("Groq returned an invalid answer") from exc

    if not isinstance(answer, str) or not answer.strip():
        raise AgentUnavailable("Groq returned an empty answer")
    return answer.strip()


async def ask_agent(messages: list[dict[str, str]]) -> str:
    """Call Groq without blocking python-telegram-bot's event loop."""
    import asyncio

    return await asyncio.to_thread(_request, messages)
