from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from aiogram.types import User as TgUser

from core.config import Settings
from core.database import SessionFactory, session_scope
from repositories import UserRepository

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class AuthMiddleware(BaseMiddleware):
    """Drops every update from Telegram users that are not in TELEGRAM_ALLOWED_USER_IDS."""

    def __init__(self, allowed_ids: frozenset[int]) -> None:
        self.allowed_ids = allowed_ids
        self._warned: set[int] = set()

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is not None and tg_user.id in self.allowed_ids:
            return await handler(event, data)

        if tg_user is not None and tg_user.id not in self._warned:
            self._warned.add(tg_user.id)
            log.warning("unauthorized access attempt from Telegram user id %s", tg_user.id)
        if isinstance(event, Update) and tg_user is not None:
            text = (
                "⛔ <b>Доступ запрещён.</b>\n\n"
                f"Ваш Telegram ID: <code>{tg_user.id}</code>\n"
                "Владелец бота может добавить его в TELEGRAM_ALLOWED_USER_IDS."
            )
            if isinstance(event.message, Message) and event.message.chat.type == "private":
                await event.message.answer(text)
            elif isinstance(event.callback_query, CallbackQuery):
                await event.callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return None


class DbSessionMiddleware(BaseMiddleware):
    """One DB transaction per update; injects ``session`` and the ``db_user`` row."""

    def __init__(self, session_factory: SessionFactory, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        chat = data.get("event_chat")
        async with session_scope(self.session_factory) as session:
            data["session"] = session
            if tg_user is not None:
                data["db_user"] = await UserRepository(session).get_or_create(
                    tg_user.id,
                    chat.id if chat is not None else tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    default_timezone=self.settings.default_timezone,
                    default_poll_interval=self.settings.default_poll_interval_seconds,
                )
            return await handler(event, data)
