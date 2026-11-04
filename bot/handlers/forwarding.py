"""Bot API forwarding with cached rules, album collection and bounded delivery."""
from __future__ import annotations

import asyncio
import logging
from telegram import Message, Update, InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
from telegram.ext import ContextTypes, MessageHandler, filters
from bot.db.channels import get_admins_by_source_channel
from bot.utils import userbot_manager
from bot.utils.delivery import send_with_retry
from bot.utils.formatting import to_bot_entities

logger = logging.getLogger(__name__)
_dispatcher = userbot_manager._dispatcher
_albums: dict[tuple, list[Message]] = {}
_album_tasks: dict[tuple, asyncio.Task] = {}


async def forward_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post
    if msg is None:
        return
    if msg.media_group_id:
        key = (msg.chat.id, msg.media_group_id)
        if key not in _albums:
            _albums[key] = []
            _album_tasks[key] = asyncio.create_task(_collect_album(key, context))
        _albums[key].append(msg)
    else:
        await _dispatch(context, [msg])


async def _collect_album(key, context):
    try:
        await asyncio.sleep(1)
        await _dispatch(context, sorted(_albums.pop(key), key=lambda msg: msg.message_id))
    except Exception:
        logger.exception("Failed to collect album %r", key)
    finally:
        _albums.pop(key, None)
        _album_tasks.pop(key, None)


async def _dispatch(context, messages):
    source_id = messages[0].chat.id
    admin_ids = await asyncio.to_thread(get_admins_by_source_channel, source_id)
    for admin_id in admin_ids:
        # A saved session isn't proof that its listener is actually running.
        if userbot_manager.is_userbot_running(admin_id):
            continue
        group = messages[0].media_group_id
        key = ((source_id, 'album', str(group)) if group
               else (source_id, 'message', messages[0].message_id))
        await _dispatcher.submit(admin_id, key,
                                 lambda aid=admin_id: _forward_for_admin(context, messages, aid))


def _filtered(msg, rules):
    """Use the same UTF-16 entity-preserving filter engine as Telethon."""
    from telethon import types
    reverse = {
        'bold': types.MessageEntityBold, 'italic': types.MessageEntityItalic,
        'underline': types.MessageEntityUnderline, 'strikethrough': types.MessageEntityStrike,
        'code': types.MessageEntityCode, 'spoiler': types.MessageEntitySpoiler,
        'url': types.MessageEntityUrl, 'mention': types.MessageEntityMention,
        'hashtag': types.MessageEntityHashtag, 'cashtag': types.MessageEntityCashtag,
        'bot_command': types.MessageEntityBotCommand, 'email': types.MessageEntityEmail,
        'phone_number': types.MessageEntityPhone,
    }
    text = msg.text if msg.text is not None else msg.caption or ''
    original = msg.entities if msg.text is not None else msg.caption_entities
    entities = []
    for entity in original or []:
        args = (entity.offset, entity.length)
        if entity.type == 'text_link':
            entities.append(types.MessageEntityTextUrl(*args, entity.url))
        elif entity.type == 'pre':
            entities.append(types.MessageEntityPre(*args, entity.language or ''))
        elif entity.type == 'custom_emoji':
            entities.append(types.MessageEntityCustomEmoji(*args, int(entity.custom_emoji_id)))
        elif entity.type in ('blockquote', 'expandable_blockquote'):
            # Telethon 1.38 models regular blockquotes only.
            entities.append(types.MessageEntityBlockquote(*args))
        elif entity.type in reverse:
            entities.append(reverse[entity.type](*args))
    original_text = text
    original_entities = [entity.to_dict() for entity in entities]
    text, entities = userbot_manager._apply_cached_filters_with_entities(rules, text, entities)
    if text == original_text and [entity.to_dict() for entity in entities] == original_entities:
        return text, list(original or [])
    return text, to_bot_entities(entities)


async def _forward_for_admin(context, messages, admin_id: int) -> None:
    if not await userbot_manager.is_admin_working(admin_id):
        return
    targets = await userbot_manager._get_cached_targets(admin_id)
    rules = await userbot_manager._get_cached_filters(admin_id)
    prepared = []
    for msg in messages:
        if msg.document and (msg.document.file_name or '').lower().endswith('.apk'):
            if any(r['find_text'] == '<BLOCK_APK>' for r in rules):
                return
        text, entities = _filtered(msg, rules)
        if text is None:
            return  # None means blocked; an empty string means deliberate deletion.
        prepared.append((msg, text, entities))

    async def deliver(target):
        tid = target['channel_id']
        if tid == messages[0].chat.id:
            return
        async def send():
            if len(prepared) > 1:
                media = []
                for msg, text, entities in prepared:
                    obj, kind = ((msg.photo[-1], InputMediaPhoto) if msg.photo else
                                 (msg.video, InputMediaVideo) if msg.video else
                                 (msg.audio, InputMediaAudio) if msg.audio else (msg.document, InputMediaDocument))
                    if obj is None:
                        raise ValueError('Unsupported album media')
                    media.append(kind(obj.file_id, caption=text, caption_entities=entities, parse_mode=None))
                await context.bot.send_media_group(chat_id=tid, media=media)
            else:
                msg, text, entities = prepared[0]
                if msg.text is not None:
                    if text:
                        await context.bot.send_message(chat_id=tid, text=text, entities=entities, parse_mode=None)
                else:
                    await context.bot.copy_message(chat_id=tid, from_chat_id=msg.chat.id,
                                                   message_id=msg.message_id, caption=text,
                                                   caption_entities=entities, parse_mode=None)
        await send_with_retry(send)

    # Bound fan-out for this account; don't create unbounded target tasks.
    errors = []
    for offset in range(0, len(targets), 8):
        results = await asyncio.gather(*(deliver(t) for t in targets[offset:offset+8]), return_exceptions=True)
        errors.extend(r for r in results if isinstance(r, BaseException))
    if errors:
        raise RuntimeError(f'{len(errors)} target deliveries failed: {errors[0]}')


async def stop_forwarding():
    if _album_tasks:
        await asyncio.gather(*list(_album_tasks.values()), return_exceptions=True)
    for admin_id in list(_dispatcher.workers):
        await _dispatcher.stop(admin_id)


def register(application):
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.UpdateType.CHANNEL_POST,
                                           forward_channel_post), group=1)
