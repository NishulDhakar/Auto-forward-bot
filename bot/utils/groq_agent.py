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
        "You are the Auto Forward Bot support agent. Give only the next useful action in plain Telegram text.\n"
        f"CURRENT ACCOUNT CONTEXT:\n{plan_context}\n\n"
        "RULES:\n"
        "- Reply in 1-3 short lines. No introductions, summaries, menus, or repeated questions.\n"
        "- If the request is unclear, ask one specific question only.\n"
        "- Never claim setup is complete unless a bot action confirmed it.\n"
        "- Never ask for OTPs, passwords, API keys, bot tokens, or payment credentials.\n"
        "- Paid setup uses /authorize, /addsource, /addtarget, /filter, /schedule, /work, and /mystatus.\n"
        "- Free users can get explanations, but forwarding setup requires an active paid plan. Say: Use /pro.\n"
        "- Do not invent prices, payment status, expiry dates, or capabilities.\n"
        "- Source means copy-from channel. Target means destination channel. The bot must be admin in targets."
    )


def _request(messages: list[dict[str, str]]) -> str:
    if not GROQ_API_KEY:
        raise AgentUnavailable("GROQ_API_KEY is not configured")

    payload = json.dumps({
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 250,
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
