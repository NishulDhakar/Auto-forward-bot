"""Translate Telethon entities to the Bot API without losing UTF-16 offsets."""
from telegram import MessageEntity

_TYPES = {
    'MessageEntityBold': 'bold', 'MessageEntityItalic': 'italic',
    'MessageEntityUnderline': 'underline', 'MessageEntityStrike': 'strikethrough',
    'MessageEntityCode': 'code', 'MessageEntityPre': 'pre',
    'MessageEntityTextUrl': 'text_link', 'MessageEntityUrl': 'url',
    'MessageEntityMention': 'mention', 'MessageEntityHashtag': 'hashtag',
    'MessageEntityCashtag': 'cashtag', 'MessageEntityBotCommand': 'bot_command',
    'MessageEntityEmail': 'email', 'MessageEntityPhone': 'phone_number',
    'MessageEntitySpoiler': 'spoiler', 'MessageEntityCustomEmoji': 'custom_emoji',
    'MessageEntityBlockquote': 'blockquote',
}


def to_bot_entities(entities):
    result = []
    for entity in entities:
        kind = _TYPES.get(type(entity).__name__)
        if not kind:
            continue
        kwargs = {}
        if kind == 'text_link':
            kwargs['url'] = entity.url
        elif kind == 'pre':
            kwargs['language'] = entity.language
        elif kind == 'custom_emoji':
            kwargs['custom_emoji_id'] = str(entity.document_id)
        elif kind == 'blockquote' and getattr(entity, 'collapsed', False):
            kind = 'expandable_blockquote'
        result.append(MessageEntity(kind, entity.offset, entity.length, **kwargs))
    return result
