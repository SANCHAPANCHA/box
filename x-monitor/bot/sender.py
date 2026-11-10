from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from services.formatting import RenderedMessage
from services.notification import PermanentDeliveryError, RetryLater

log = logging.getLogger(__name__)

_PERMANENT_MARKERS = ("chat not found", "user is deactivated", "bot was blocked", "peer_id_invalid")


class TelegramSender:
    """Implements services.notification.Sender on top of aiogram."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def _send(self, chat_id: int, text: str, markup, preview: LinkPreviewOptions, silent: bool) -> int:
        try:
            msg = await self.bot.send_message(
                chat_id, text, reply_markup=markup, link_preview_options=preview, disable_notification=silent
            )
            return msg.message_id
        except TelegramRetryAfter as exc:
            raise RetryLater(exc.retry_after) from exc
        except TelegramForbiddenError as exc:
            raise PermanentDeliveryError(str(exc)) from exc
        except TelegramBadRequest as exc:
            if any(m in str(exc).lower() for m in _PERMANENT_MARKERS):
                raise PermanentDeliveryError(str(exc)) from exc
            raise

    async def send_event(self, chat_id: int, message: RenderedMessage, *, silent: bool = False) -> int:
        rows = [[InlineKeyboardButton(text=t, url=u)] for t, u in message.buttons[:1]]
        if len(message.buttons) > 1:
            rows.append([InlineKeyboardButton(text=t, url=u) for t, u in message.buttons[1:]])
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        preview = (
            LinkPreviewOptions(url=message.preview_url, prefer_large_media=True)
            if message.preview_url
            else LinkPreviewOptions(is_disabled=True)
        )
        try:
            return await self._send(chat_id, message.text, markup, preview, silent)
        except TelegramBadRequest as exc:
            # e.g. an URL button Telegram does not accept: retry as plain text, the link is in the body.
            log.warning("send_event rejected (%s), retrying without buttons/preview", exc)
            return await self._send(chat_id, message.text, None, LinkPreviewOptions(is_disabled=True), silent)

    async def send_text(self, chat_id: int, text: str, *, silent: bool = False) -> int:
        return await self._send(chat_id, text, None, LinkPreviewOptions(is_disabled=True), silent)
