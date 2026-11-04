"""Telethon sessions, cached account configuration, and ordered forwarding."""

from __future__ import annotations

import logging
import os
import re
import time
import asyncio
from datetime import datetime, timezone
from copy import deepcopy

from bot.utils.delivery import OrderedDispatcher, send_with_retry

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from bot.db import channels as channels_db
from bot.db import users as users_db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Session storage
# ─────────────────────────────────────────────────────────────────────────────
_clients: dict[int, TelegramClient] = {}
_listeners: dict[int, list] = {}
_tasks:   dict[int, list] = {}          # admin_id -> list of background asyncio tasks
_session_dir = os.path.expanduser("~/.tg_bot_sessions")



# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache — DB is NEVER called in the message-handling hot path.
# Sync Supabase calls are offloaded to a thread via run_in_executor
# so they never block the Telethon event loop.
# ─────────────────────────────────────────────────────────────────────────────
_target_cache: dict[int, dict] = {}   # admin_id -> {"data": list, "ts": float}
_filter_cache: dict[int, dict] = {}   # admin_id -> {"data": list, "ts": float}
_working_cache: dict[int, dict] = {}  # admin_id -> {"data": bool, "ts": float}
_CACHE_TTL = 30  # seconds

async def _get_cached_targets(admin_id: int) -> list[dict]:
    """Return target channels from cache, refreshing in a thread if stale."""
    cached = _target_cache.get(admin_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    # Offload the synchronous Supabase call to a thread
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, channels_db.get_target_channels, admin_id)
    _target_cache[admin_id] = {"data": data, "ts": time.time()}
    return data


async def _get_cached_filters(admin_id: int) -> list[dict]:
    """Return filter rules from cache, refreshing in a thread if stale."""
    cached = _filter_cache.get(admin_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    from bot.db.filters import get_filters
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, get_filters, admin_id)
    _filter_cache[admin_id] = {"data": data, "ts": time.time()}
    return data


async def _get_cached_working_status(admin_id: int) -> bool:
    """Return working status of the admin from cache, refreshing if stale."""
    cached = _working_cache.get(admin_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL and time.time() < cached.get("expires", float("inf")):
        return cached["data"]

    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, users_db.get_user, admin_id)
    from bot.config import SUPER_ADMIN_ID
    from bot.utils.roles import is_admin_active
    active = admin_id == SUPER_ADMIN_ID or (user is not None and is_admin_active(user))
    is_working = active and (user.get("is_working") is not False if user else True)
    expires = float("inf")
    if user and user.get("subscription_end") and admin_id != SUPER_ADMIN_ID:
        expiry = datetime.fromisoformat(user["subscription_end"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        expires = expiry.timestamp()
    _working_cache[admin_id] = {"data": bool(is_working), "ts": time.time(), "expires": expires}
    return _working_cache[admin_id]["data"]


async def is_admin_working(admin_id: int) -> bool:
    """Public helper to check if admin's bot status is active/working."""
    return await _get_cached_working_status(admin_id)


def invalidate_cache(admin_id: int) -> None:
    """Call when an admin changes targets, filters, or source channel."""
    _target_cache.pop(admin_id, None)
    _filter_cache.pop(admin_id, None)
    _working_cache.pop(admin_id, None)


def _adjust_entities_for_replacement(
    text: str,
    entities: list,
    pattern_or_find: str | re.Pattern,
    replace_str: str,
    is_regex: bool = False,
) -> tuple[str, list]:
    """
    Replaces pattern_or_find with replace_str in text and adjusts entity offsets and lengths.
    Handles multiple matches of pattern_or_find.
    """
    matches = []
    if is_regex:
        if isinstance(pattern_or_find, str):
            regex = re.compile(pattern_or_find)
        else:
            regex = pattern_or_find
        for match in regex.finditer(text):
            matches.append((match.start(), match.end()))
    else:
        find_len = len(pattern_or_find)
        if find_len > 0:
            start = 0
            while True:
                pos = text.find(pattern_or_find, start)
                if pos == -1:
                    break
                matches.append((pos, pos + find_len))
                start = pos + find_len

    if not matches:
        return text, entities

    current_entities = deepcopy(entities)
    # Telegram entities count UTF-16 code units, Python regex uses code points.
    def utf16_len(value):
        return len(value.encode("utf-16-le")) // 2
    matches_utf16 = [(utf16_len(text[:start]), utf16_len(text[:end])) for start, end in matches]

    matches.sort(key=lambda x: x[0])

    adjusted_entities = []
    for entity in current_entities:
        e_start = entity.offset
        e_end = entity.offset + entity.length

        new_e_start = e_start
        new_e_end = e_end

        for start, end in matches_utf16:
            diff = utf16_len(replace_str) - (end - start)

            if end <= e_start:
                new_e_start += diff
            elif start <= e_start < end:
                new_e_start += (start - e_start)

            if end <= e_end:
                new_e_end += diff
            elif start <= e_end < end:
                new_e_end += (start + utf16_len(replace_str) - e_end)

        new_len = new_e_end - new_e_start
        if new_len > 0:
            entity.offset = new_e_start
            entity.length = new_len
            adjusted_entities.append(entity)

    new_text_parts = []
    last_end = 0
    for start, end in matches:
        new_text_parts.append(text[last_end:start])
        new_text_parts.append(replace_str)
        last_end = end
    new_text_parts.append(text[last_end:])
    new_text = "".join(new_text_parts)

    return new_text, adjusted_entities


def _apply_cached_filters_with_entities(
    rules: list[dict],
    text: str | None,
    entities: list | None,
) -> tuple[str | None, list]:
    """Apply filter rules on text and adjust entity offsets simultaneously."""
    if text is None:
        return None, []

    entities = deepcopy(entities or [])

    # Block rules first
    for rule in rules:
        if rule["find_text"] == "<BLOCK>":
            if rule["replace_text"].lower() in text.lower():
                return None, []  # Signal to block this message

    # Regex wildcard rules
    for rule in rules:
        find = rule["find_text"]
        replace = rule["replace_text"]
        if find == "<ALL_LINKS>":
            from telethon.types import MessageEntityTextUrl
            for entity in list(entities):
                if isinstance(entity, MessageEntityTextUrl):
                    if replace:
                        entity.url = replace
                    else:
                        entities.remove(entity)
            text, entities = _adjust_entities_for_replacement(
                text, entities, r'https?://\S+|www\.\S+|t\.me/\S+', replace, is_regex=True
            )
        elif find == "<ALL_USERNAMES>":
            text, entities = _adjust_entities_for_replacement(
                text, entities, r'@[a-zA-Z0-9_]+', replace, is_regex=True
            )

    # Exact match rules
    for rule in rules:
        find = rule["find_text"]
        if find not in ("<ALL_LINKS>", "<ALL_USERNAMES>", "<BLOCK>", "<BLOCK_APK>"):
            text, entities = _adjust_entities_for_replacement(
                text, entities, find, rule["replace_text"], is_regex=False
            )

    return text, entities


# ─────────────────────────────────────────────────────────────────────────────
# Pending auth — module-level dict so the TelegramClient object is never
# copied or re-serialised between PTB handler calls.
# ─────────────────────────────────────────────────────────────────────────────
_pending_auth: dict[int, dict] = {}


def set_pending_auth(admin_id: int, client: TelegramClient, phone: str, code_hash: str) -> None:
    _pending_auth[admin_id] = {"client": client, "phone": phone, "code_hash": code_hash}


def get_pending_auth(admin_id: int) -> dict | None:
    return _pending_auth.get(admin_id)


async def clear_pending_auth(admin_id: int) -> None:
    pending = _pending_auth.pop(admin_id, None)
    if pending and pending.get("client"):
        try:
            await pending["client"].disconnect()
        except:
            pass


def _make_auth_client() -> TelegramClient:
    return TelegramClient(
        StringSession(),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        device_model="Samsung Galaxy S23",
        system_version="Android 14",
        app_version="10.3.2",
        lang_code="en",
        system_lang_code="en-IN",
        flood_sleep_threshold=60,
    )


async def prepare_auth_client(phone: str) -> TelegramClient:
    client = _make_auth_client()
    await client.connect()
    await asyncio.sleep(1)  # let connection stabilize
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Session persistence
# ─────────────────────────────────────────────────────────────────────────────

def save_session_string(admin_id: int, session_string: str) -> None:
    os.makedirs(_session_dir, mode=0o700, exist_ok=True)
    os.chmod(_session_dir, 0o700)
    path = os.path.join(_session_dir, f"admin_{admin_id}.session_str")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(session_string)
    logger.info("Session string saved to local file for admin %s", admin_id)

    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        db.table("users").update({"session_string": session_string}).eq("user_id", admin_id).execute()
        logger.info("Session string also saved to DB for admin %s", admin_id)
    except Exception as e:
        logger.error("Session database backup failed for admin %s: %s", admin_id, type(e).__name__)


def load_session_string(admin_id: int) -> str | None:
    path = os.path.join(_session_dir, f"admin_{admin_id}.session_str")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()

    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        res = db.table("users").select("session_string").eq("user_id", admin_id).execute()
        data = getattr(res, "data", [])
        if isinstance(data, list) and len(data) > 0:
            row = data[0]
            if isinstance(row, dict) and row.get("session_string"):
                return str(row["session_string"])
    except Exception:
        pass
    return None


def is_userbot_authorized(admin_id: int) -> bool:
    return bool(load_session_string(admin_id))


def remove_session(admin_id: int) -> None:
    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        db.table("users").update({"session_string": None}).eq("user_id", admin_id).execute()
    except Exception:
        pass
    for path in [
        os.path.join(_session_dir, f"admin_{admin_id}.session_str"),
        os.path.join(_session_dir, f"admin_{admin_id}.session"),
        os.path.join(_session_dir, f"admin_{admin_id}"),
    ]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Resolved entity cache — avoids slow get_input_entity on every send
from typing import Any
_resolved_entities: dict[int, dict[int, Any]] = {}  # admin_id -> {channel_id -> entity}


async def _resolve_target(client: TelegramClient, admin_id: int, channel_id: int) -> bool:
    """Pre-resolve a target entity and cache it. Returns True on success."""
    _resolved_entities.setdefault(admin_id, {})
    if channel_id in _resolved_entities[admin_id]:
        return True
    try:
        entity = await client.get_entity(channel_id)
        _resolved_entities[admin_id][channel_id] = entity
        return True
    except Exception as e:
        logger.warning("Failed to pre-resolve target %s for admin %s: %s", channel_id, admin_id, e)
        return False


class TargetResolutionError(RuntimeError):
    """Target could not be resolved before any send was attempted."""


async def _get_target_entity(client: TelegramClient, admin_id: int, target_id: int) -> Any:
    """Retrieve target entity from cache, or resolve it dynamically on demand."""
    _resolved_entities.setdefault(admin_id, {})
    entity = _resolved_entities[admin_id].get(target_id)
    if entity is not None:
        return entity
    try:
        entity = await client.get_entity(target_id)
        _resolved_entities[admin_id][target_id] = entity
        return entity
    except Exception as e:
        logger.warning("Failed to dynamically resolve target entity %s for admin %s: %s", target_id, admin_id, e)
        raise TargetResolutionError(f"Cannot resolve target {target_id}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Userbot lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def start_userbot(admin_id: int, bot) -> TelegramClient | None:
    if admin_id in _clients:
        return _clients[admin_id]

    if not await is_admin_working(admin_id):
        return None
    session_string = await asyncio.to_thread(load_session_string, admin_id)
    if not session_string:
        return None

    client = None
    try:
        logger.info("Starting background userbot for admin %s...", admin_id)
        client = TelegramClient(
            StringSession(session_string),
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
            device_model="Samsung Galaxy S23",
            system_version="Android 14",
            app_version="10.3.2",
            lang_code="en",
            system_lang_code="en-IN",
            flood_sleep_threshold=60,
            catch_up=False,  # CRITICAL: prevents batch replay of missed messages
        )
        await client.connect()

        if not await client.is_user_authorized():
            logger.warning("Session unauthorized for admin %s. Removing.", admin_id)
            await client.disconnect()
            await asyncio.to_thread(remove_session, admin_id)
            return None

        source = await asyncio.to_thread(channels_db.get_source_channel, admin_id)
        if source:
            source_id = source["channel_id"]
            source_username = source.get("channel_username")

            # If it's a public channel and we have the username, ensure we are joined
            # because Telegram does not push NewMessage events to non-members of public channels!
            if source_username:
                try:
                    from telethon.tl.functions.channels import JoinChannelRequest
                    await client(JoinChannelRequest(source_username.replace("@", "")))
                    logger.info("Auto-joined public source channel %s for admin %s", source_username, admin_id)
                except Exception as e:
                    logger.debug("Auto-join skipped/failed for %s: %s", source_username, e)

            # Validate source channel access
            try:
                await client.get_input_entity(source_id)
            except Exception:
                # StringSession stores credentials but no persistent entity cache.
                # Load dialog access hashes for private channels after a restart.
                await client.get_dialogs()
                await client.get_input_entity(source_id)
                logger.info("Loaded private source access hash for admin %s", admin_id)
            # Pre-warm caches so the first message doesn't hit DB
            loop = asyncio.get_running_loop()
            targets = await loop.run_in_executor(None, channels_db.get_target_channels, admin_id)
            _target_cache[admin_id] = {"data": targets, "ts": time.time()}

            from bot.db.filters import get_filters
            rules = await loop.run_in_executor(None, get_filters, admin_id)
            _filter_cache[admin_id] = {"data": rules, "ts": time.time()}

            # Pre-resolve target entities so send_message doesn't need to resolve each time
            for t in targets:
                tid = t["channel_id"]
                ok = await _resolve_target(client, admin_id, tid)
                if ok:
                    logger.info("  ✓ Pre-resolved target %s for admin %s", tid, admin_id)
                else:
                    logger.warning("  ✗ Cannot pre-resolve target %s for admin %s (will use Bot API fallback)", tid, admin_id)

            # Establish a baseline once; the poller recovers subsequent gaps.
            latest = await client.get_messages(source_id, limit=1)
            _poll_cursors[admin_id] = latest[0].id if latest else 0
            # Register event handler with chats= filter for efficient dispatch
            _register_forwarding_listener(client, admin_id, source_id, bot)
            logger.info("Userbot listening on source %s for admin %s", source_id, admin_id)

            # Start connection health monitor and fallback poller — track for clean shutdown
            t1 = asyncio.create_task(_connection_monitor(client, admin_id, bot))
            t2 = asyncio.create_task(_channel_poller(client, bot, admin_id, source_id))
            _tasks.setdefault(admin_id, []).extend([t1, t2])

        _clients[admin_id] = client
        return client

    except Exception as exc:
        if client is not None:
            await client.disconnect()
        error_str = str(exc)
        # Detect permanently invalidated session (used from 2 IPs simultaneously)
        if "authorization key" in error_str.lower() or "AuthKeyDuplicated" in error_str:
            logger.error(
                "DEAD SESSION for admin %s — session was used from two IPs. "
                "Auto-removing and alerting admin to re-authorize.", admin_id
            )
            # Wipe the dead session so the bot doesn't keep retrying it
            await asyncio.to_thread(remove_session, admin_id)
            # Alert the admin via Telegram so they know to re-link
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "🔴 *Session Expired — Action Required*\n\n"
                        "Your Telegram account session was invalidated because it was used "
                        "from two different locations at the same time.\n\n"
                        "The bot has automatically removed the old session.\n\n"
                        "➡️ Please send /authorize to re-link your account and resume forwarding."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        else:
            logger.error("Failed to start userbot for admin %s: %s", admin_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Connection health monitor — detects silent disconnects
# ─────────────────────────────────────────────────────────────────────────────

async def _connection_monitor(client: TelegramClient, admin_id: int, bot) -> None:
    """Periodically verify the Telethon connection is alive."""
    while admin_id in _clients and _clients[admin_id] is client:
        await asyncio.sleep(120)  # Check every 2 minutes
        try:
            if not client.is_connected():
                logger.warning("⚠️ Telethon disconnected for admin %s — reconnecting...", admin_id)
                await client.connect()
                if await client.is_user_authorized():
                    logger.info("✅ Telethon reconnected for admin %s", admin_id)
                else:
                    logger.error("❌ Session revoked for admin %s after reconnect", admin_id)
                    break
            else:
                # Ping to verify the connection is truly alive (not just TCP-open)
                await client.get_me()
        except Exception as e:
            logger.error("Connection monitor error for admin %s: %s", admin_id, e)


_dispatcher = OrderedDispatcher()
_poll_cursors: dict[int, int] = {}
_target_slots = asyncio.Semaphore(8)


async def _channel_poller(client: TelegramClient, bot, admin_id: int, source_channel_id: int) -> None:
    """Recover gaps since startup in chronological pages, retaining album boundaries."""
    from telethon.errors import FloodWaitError
    while admin_id in _clients and _clients[admin_id] is client:
        await asyncio.sleep(15)
        try:
            cursor = _poll_cursors.get(admin_id, 0)
            # Snapshot the upper bound so a busy source cannot monopolize the loop.
            latest = await client.get_messages(source_channel_id, limit=1)
            upper = latest[0].id if latest else cursor
            album = []
            async for msg in client.iter_messages(source_channel_id, min_id=cursor,
                                                  max_id=upper + 1, reverse=True):
                if album and msg.grouped_id != album[0].grouped_id:
                    await _enqueue_messages(client, bot, admin_id, source_channel_id, album)
                    _poll_cursors[admin_id] = album[-1].id
                    album = []
                if msg.grouped_id:
                    album.append(msg)
                else:
                    await _enqueue_messages(client, bot, admin_id, source_channel_id, [msg])
                    _poll_cursors[admin_id] = msg.id
            if album:
                # A fresh album may still be arriving; finish it on the next pass.
                age = (datetime.now(timezone.utc) - album[-1].date).total_seconds()
                if age >= 3:
                    await _enqueue_messages(client, bot, admin_id, source_channel_id, album)
                    _poll_cursors[admin_id] = album[-1].id
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recovery poll failed for admin %s", admin_id)


def _register_forwarding_listener(client, admin_id: int, source_channel_id: int, bot) -> None:
    # Integer peer IDs let Telethon filter updates before running our handlers.
    @client.on(events.NewMessage(chats=source_channel_id))
    async def handler(event):
        if not event.message.grouped_id:
            await _enqueue_messages(client, bot, admin_id, source_channel_id, [event.message])

    @client.on(events.Album(chats=source_channel_id))
    async def album_handler(event):
        await _enqueue_messages(client, bot, admin_id, source_channel_id, event.messages)

    _listeners[admin_id] = [handler, album_handler]


async def _enqueue_messages(client, bot, admin_id, source_id, messages):
    messages = sorted(messages, key=lambda m: m.id)
    group = messages[0].grouped_id
    key = (source_id, "album", str(group)) if group else (source_id, "message", messages[0].id)
    await _dispatcher.submit(admin_id, key,
                             lambda: _deliver_messages(client, bot, admin_id, source_id, messages))


async def _deliver_messages(client, bot, admin_id, source_id, messages):
    if not await _get_cached_working_status(admin_id):
        return
    targets = await _get_cached_targets(admin_id)
    rules = await _get_cached_filters(admin_id)
    prepared = []
    for msg in messages:
        if getattr(msg, "action", None):
            continue  # Service messages are not copyable content.
        if msg.file and msg.file.name and msg.file.name.lower().endswith('.apk'):
            if any(r['find_text'] == '<BLOCK_APK>' for r in rules):
                return
        text, entities = _apply_cached_filters_with_entities(rules, msg.message or '', msg.entities or [])
        if text is None:
            return
        prepared.append((msg, text, entities))
    if not prepared:
        return

    async def deliver(target):
        target_id = target['channel_id']
        if target_id == source_id:
            logger.error("Skipping source-to-itself route for admin %s", admin_id)
            return
        async with _target_slots:
            await _deliver_target(client, bot, admin_id, target_id, prepared)

    failed = []
    for offset in range(0, len(targets), 8):
        batch = targets[offset:offset + 8]
        results = await asyncio.gather(*(deliver(t) for t in batch), return_exceptions=True)
        failed.extend((t['channel_id'], r) for t, r in zip(batch, results) if isinstance(r, BaseException))
    for tid, error in failed:
        logger.error("Delivery failed: admin=%s source=%s message=%s target=%s error=%s",
                     admin_id, source_id, messages[0].id, tid, type(error).__name__)
    if failed:
        raise RuntimeError(f"Delivery failed for {len(failed)} of {len(targets)} targets")


async def _deliver_target(client, bot, admin_id, target_id, prepared):
    from telethon.errors import ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError
    async def native_send():
        entity = await _get_target_entity(client, admin_id, target_id)
        if len(prepared) > 1:
            await client.send_file(entity, file=[m.media for m, _, _ in prepared],
                                   caption=[text for _, text, _ in prepared],
                                   formatting_entities=[ents for _, _, ents in prepared], parse_mode=None)
        else:
            msg, text, entities = prepared[0]
            unchanged = text == (msg.message or '') and [e.to_dict() for e in entities] == [e.to_dict() for e in (msg.entities or [])]
            if unchanged:
                await client.send_message(entity, msg)
            elif msg.media and not getattr(msg, 'web_preview', None):
                await client.send_file(entity, file=msg.media, caption=text,
                                       formatting_entities=entities, parse_mode=None)
            elif text:
                await client.send_message(entity, text, formatting_entities=entities, parse_mode=None)

    try:
        await send_with_retry(native_send)
    except (TargetResolutionError, ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError):
        # Only explicit permission rejection is safe to retry through another
        # transport. A timeout may already have delivered the original request.
        await _bot_fallback(bot, target_id, prepared)


async def _bot_fallback(bot, target_id, prepared):
    """Copy with Bot API first, then upload once per rejected target if necessary."""
    from contextlib import ExitStack
    from tempfile import TemporaryDirectory
    from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
    from telegram.error import BadRequest
    from bot.utils.formatting import to_bot_entities

    async def copy():
        if len(prepared) > 1:
            # CopyMessages cannot replace captions, so edited albums use upload.
            if any(text != (msg.message or '') or [e.to_dict() for e in ents] != [e.to_dict() for e in (msg.entities or [])] for msg, text, ents in prepared):
                raise ValueError('Album captions require upload')
            result = await bot.copy_messages(chat_id=target_id, from_chat_id=prepared[0][0].chat_id,
                                            message_ids=[m.id for m, _, _ in prepared])
            if len(result) != len(prepared):
                # Some messages may have arrived: do not retry the whole album.
                raise RuntimeError('Telegram copied only part of the album')
        else:
            msg, text, entities = prepared[0]
            if not msg.media or getattr(msg, 'web_preview', None):
                if text:
                    await bot.send_message(chat_id=target_id, text=text,
                                           entities=to_bot_entities(entities), parse_mode=None)
            else:
                kwargs = {}
                if text != (msg.message or '') or [e.to_dict() for e in entities] != [e.to_dict() for e in (msg.entities or [])]:
                    kwargs = {'caption': text, 'caption_entities': to_bot_entities(entities), 'parse_mode': None}
                await bot.copy_message(chat_id=target_id, from_chat_id=msg.chat_id, message_id=msg.id, **kwargs)
    try:
        await send_with_retry(copy)
        return
    except (BadRequest, ValueError) as exc:
        # Permission/protection failures are not solved by downloading content.
        if isinstance(exc, BadRequest) and not any(
            phrase in str(exc).lower() for phrase in ('message to copy not found', 'chat not found')
        ):
            raise
    if any(getattr(m, 'noforwards', False) for m, _, _ in prepared):
        raise RuntimeError('Protected content cannot be copied')
    # Private directory per operation prevents concurrent downloads overwriting files.
    with TemporaryDirectory(prefix='tg-forward-') as temp_dir, ExitStack() as stack:
        media = []
        for i, (msg, text, entities) in enumerate(prepared):
            path = await msg.download_media(file=os.path.join(temp_dir, str(i)) + '/')
            if not path:
                raise RuntimeError('Media download returned no file')
            handle = stack.enter_context(open(path, 'rb'))
            kwargs = {'media': handle, 'caption': text or None,
                      'caption_entities': to_bot_entities(entities), 'parse_mode': None}
            kind = (InputMediaPhoto if msg.photo else InputMediaVideo if msg.video
                    else InputMediaAudio if msg.audio else InputMediaDocument)
            media.append(kind(**kwargs))
        async def upload():
            if len(media) > 1:
                await bot.send_media_group(chat_id=target_id, media=media)
            else:
                msg, text, entities = prepared[0]
                item = media[0]
                kwargs = {'chat_id': target_id}
                if msg.sticker:
                    await bot.send_sticker(sticker=item.media, **kwargs)
                elif msg.video_note:
                    await bot.send_video_note(video_note=item.media, **kwargs)
                else:
                    kwargs.update(caption=text or None, caption_entities=to_bot_entities(entities), parse_mode=None)
                    method, name = ((bot.send_photo, 'photo') if msg.photo else
                                    (bot.send_animation, 'animation') if msg.gif else
                                    (bot.send_video, 'video') if msg.video else
                                    (bot.send_audio, 'audio') if msg.audio else
                                    (bot.send_voice, 'voice') if msg.voice else (bot.send_document, 'document'))
                    await method(**{name: item.media}, **kwargs)
        await send_with_retry(upload)


# # Lifecycle helpers
# ─────────────────────────────────────────────────────────────────────────────

async def restart_userbot_listener(admin_id: int, bot) -> None:
    invalidate_cache(admin_id)
    # Clear pre-resolved entities for this admin
    _resolved_entities.pop(admin_id, None)
    await stop_userbot(admin_id)
    await start_userbot(admin_id, bot)


async def stop_userbot(admin_id: int) -> None:
    client = _clients.pop(admin_id, None)
    if client:
        for listener in _listeners.pop(admin_id, []):
            client.remove_event_handler(listener)
    # Cancel all background tasks first so they don't get "destroyed while pending"
    for task in _tasks.pop(admin_id, []):
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    await _dispatcher.stop(admin_id)
    invalidate_cache(admin_id)
    _poll_cursors.pop(admin_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.error("Error stopping userbot for admin %s: %s", admin_id, exc)


async def start_all_userbots(bot) -> None:
    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        res = await asyncio.to_thread(lambda: db.table("users").select("user_id, session_string").not_.is_("session_string", "null").execute())
        data = getattr(res, "data", [])
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("session_string"):
                    await start_userbot(row["user_id"], bot)
    except Exception:
        logger.exception("Could not load userbot sessions from database")

    for fname in os.listdir(_session_dir) if os.path.isdir(_session_dir) else []:
        if fname.endswith(".session_str"):
            try:
                admin_id = int(fname.replace("admin_", "").replace(".session_str", ""))
                if admin_id not in _clients:
                    await start_userbot(admin_id, bot)
            except ValueError:
                continue


async def stop_all_userbots() -> None:
    for admin_id in list(_clients.keys()):
        await stop_userbot(admin_id)
    for admin_id in list(_pending_auth):
        await clear_pending_auth(admin_id)


def is_userbot_running(admin_id: int) -> bool:
    client = _clients.get(admin_id)
    return bool(client and client.is_connected() and admin_id in _poll_cursors)


def delivery_status(admin_id: int | None = None) -> dict:
    return _dispatcher.snapshot(admin_id)
