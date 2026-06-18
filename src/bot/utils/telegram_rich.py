"""Thin typed wrapper over bot._post() for Telegram Bot API 10.1 Rich Messages.

Provides send_rich_message and send_rich_message_draft without depending on
python-telegram-bot native support (which doesn't exist yet in PTB 22.7/22.8).

Once PTB adds native Rich Message methods, replace the internals of these
functions with the native calls — the rest of the codebase won't need changes.
"""

from typing import Any, Dict, Optional

import structlog
import telegram

logger = structlog.get_logger()

RICH_MESSAGE_ENDPOINT = "sendRichMessage"
RICH_MESSAGE_DRAFT_ENDPOINT = "sendRichMessageDraft"


async def send_rich_message(
    bot: telegram.Bot,
    chat_id: int,
    markdown: str,
    *,
    reply_parameters: Optional[Dict[str, Any]] = None,
    reply_markup: Optional[Any] = None,
    message_thread_id: Optional[int] = None,
    disable_notification: bool = False,
) -> Any:
    """Send a rich message via the sendRichMessage endpoint.

    Args:
        bot: The Telegram Bot instance.
        chat_id: Target chat ID.
        markdown: Raw markdown string to render as a rich message.
        reply_parameters: Optional reply parameters dict.
        reply_markup: Optional reply markup (will be serialized to JSON if needed).
        message_thread_id: Optional forum topic thread ID.
        disable_notification: Send silently.

    Returns:
        The raw API response (sent Message dict).
    """
    data: Dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": {"markdown": markdown},
        "disable_notification": disable_notification,
    }
    if message_thread_id is not None:
        data["message_thread_id"] = message_thread_id
    if reply_parameters is not None:
        data["reply_parameters"] = reply_parameters
    if reply_markup is not None:
        # PTB objects have to_json(), dicts are passed as-is
        if hasattr(reply_markup, "to_json"):
            data["reply_markup"] = reply_markup.to_json()
        else:
            data["reply_markup"] = reply_markup

    logger.debug(
        "Sending rich message",
        chat_id=chat_id,
        markdown_len=len(markdown),
        endpoint=RICH_MESSAGE_ENDPOINT,
    )
    return await bot._post(RICH_MESSAGE_ENDPOINT, data)


async def send_rich_message_draft(
    bot: telegram.Bot,
    chat_id: int,
    draft_id: int,
    markdown: str,
    *,
    message_thread_id: Optional[int] = None,
) -> bool:
    """Stream a partial rich message via sendRichMessageDraft.

    The draft is ephemeral — Telegram shows it as a 30-second preview.
    Once finalized, call send_rich_message to persist the message.

    Args:
        bot: The Telegram Bot instance.
        chat_id: Target private chat ID.
        draft_id: Non-zero draft ID (same ID = animated transitions).
        markdown: Partial markdown string to stream.
        message_thread_id: Optional forum topic thread ID.

    Returns:
        True on success.
    """
    data: Dict[str, Any] = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": {"markdown": markdown},
    }
    if message_thread_id is not None:
        data["message_thread_id"] = message_thread_id

    result = await bot._post(RICH_MESSAGE_DRAFT_ENDPOINT, data)
    return bool(result)
