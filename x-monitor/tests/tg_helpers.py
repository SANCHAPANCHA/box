"""Telegram test doubles shared by the bot and trading tests."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

OWNER = 1001
STRANGER = 666
_ids = itertools.count(1)


class MockedSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            chat_id = getattr(method, "chat_id", None) or OWNER
            return Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=method.text,
            ).as_(bot)
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""


def message_update(text: str, user_id: int = OWNER) -> Update:
    return Update(
        update_id=next(_ids),
        message=Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=TgUser(id=user_id, is_bot=False, first_name="Tester"),
            text=text,
        ),
    )


def callback_update(data, user_id: int = OWNER) -> Update:
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)),
            from_user=TgUser(id=user_id, is_bot=False, first_name="Tester"),
            chat_instance="ci",
            data=data.pack(),
            message=Message(
                message_id=next(_ids), date=datetime.now(UTC), chat=Chat(id=user_id, type="private"), text="menu"
            ),
        ),
    )


def texts(requests: list[TelegramMethod]) -> str:
    return "\n".join(r.text for r in requests if isinstance(r, SendMessage | EditMessageText))
