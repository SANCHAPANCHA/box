"""End-to-end handler tests: real aiogram Dispatcher, routers, middlewares and DB; Telegram HTTP is mocked."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import select

from bot.callbacks import Acc, Hist, Nav, Notif, Sett, Stat
from bot.factory import create_dispatcher
from core.database import create_session_factory
from models import Subscription, User
from services.account_manager import AccountManager
from services.cache import Cache
from tests.conftest import FakeX

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


@pytest.fixture(scope="session")
def bot_env(engine, settings):
    session = MockedSession()
    bot = Bot("42:TEST-token", session=session)
    dp = create_dispatcher(settings, create_session_factory(engine))
    return bot, dp, session


@pytest.fixture
def tg(bot_env, sf, settings):
    bot, dp, session = bot_env
    session.requests.clear()
    x = FakeX()
    x.add_user("elonmusk", 44196397)
    manager = AccountManager(x, Cache())

    async def message(text: str, user_id: int = OWNER) -> list[TelegramMethod]:
        start = len(session.requests)
        update = Update(
            update_id=next(_ids),
            message=Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                from_user=TgUser(id=user_id, is_bot=False, first_name="Tester"),
                text=text,
            ),
        )
        await dp.feed_update(bot, update, account_manager=manager, settings=settings)
        return session.requests[start:]

    async def callback(data, user_id: int = OWNER) -> list[TelegramMethod]:
        start = len(session.requests)
        update = Update(
            update_id=next(_ids),
            callback_query=CallbackQuery(
                id=str(next(_ids)),
                from_user=TgUser(id=user_id, is_bot=False, first_name="Tester"),
                chat_instance="ci",
                data=data.pack(),
                message=Message(
                    message_id=next(_ids),
                    date=datetime.now(UTC),
                    chat=Chat(id=user_id, type="private"),
                    text="menu",
                ),
            ),
        )
        await dp.feed_update(bot, update, account_manager=manager, settings=settings)
        return session.requests[start:]

    return message, callback, sf


def texts(requests: list[TelegramMethod]) -> str:
    return "\n".join(r.text for r in requests if isinstance(r, SendMessage | EditMessageText))


async def test_unauthorized_user_is_rejected(tg) -> None:
    message, callback, sf = tg
    out = await message("/start", STRANGER)
    assert "Доступ запрещён" in texts(out) and str(STRANGER) in texts(out)
    out = await callback(Nav(to="main"), STRANGER)
    assert any(isinstance(r, AnswerCallbackQuery) and r.show_alert for r in out)
    async with sf() as session:
        assert await session.scalar(select(User).where(User.telegram_id == STRANGER)) is None


async def test_start_and_every_menu_renders(tg) -> None:
    message, callback, sf = tg
    out = await message("/start")
    assert "X Monitor" in texts(out) and "Мои аккаунты" in str(out[-1].reply_markup)
    async with sf() as session:
        assert await session.scalar(select(User).where(User.telegram_id == OWNER)) is not None

    for data in [
        Nav(to="main"), Nav(to="help"), Nav(to="stats"), Nav(to="settings"), Nav(to="notif"), Nav(to="history"),
        Acc(action="list"), Acc(action="del_list"), Notif(action="accounts"), Sett(action="interval_menu"),
        Sett(action="quiet_menu"), Sett(action="quiet_pick", value="start"), Sett(action="tz_menu"),
        Hist(action="acc_menu"), Hist(action="type_menu"), Hist(action="period_menu"), Stat(period="month"),
    ]:  # fmt: skip
        out = await callback(data)
        edited = [r for r in out if isinstance(r, EditMessageText)]
        assert edited, data
        assert not any(isinstance(r, AnswerCallbackQuery) and r.text and "устарело" in r.text for r in out), data


async def test_add_view_and_delete_account(tg) -> None:
    message, callback, sf = tg
    await message("/start")
    out = await callback(Nav(to="add"))
    assert "Добавление аккаунта" in texts(out)
    out = await message("https://x.com/elonmusk")
    assert "Аккаунт успешно добавлен" in texts(out) and "@elonmusk" in texts(out)

    out = await message("@elonmusk")  # free text outside the dialog also adds
    assert "уже отслеживается" in texts(out)

    async with sf() as session:
        sub = await session.scalar(select(Subscription))
    out = await callback(Acc(action="list"))
    assert "Elon Musk" in texts(out) and "Активный мониторинг" in texts(out)
    out = await callback(Acc(action="view", id=sub.x_account_id))
    assert "Событий обнаружено: 0" in texts(out)

    await callback(Acc(action="pause", id=sub.x_account_id))
    await callback(Acc(action="mute", id=sub.x_account_id))
    async with sf() as session:
        sub = await session.scalar(select(Subscription))
    assert sub.is_paused and not sub.notify_enabled

    out = await callback(Acc(action="del_ask", id=sub.x_account_id))
    assert "Удалить @elonmusk" in texts(out)
    out = await callback(Acc(action="del_yes", id=sub.x_account_id))
    assert "удалён из мониторинга" in texts(out)
    async with sf() as session:
        assert await session.scalar(select(Subscription)) is None


async def test_settings_are_persisted(tg) -> None:
    message, callback, sf = tg
    await message("/start")
    await callback(Notif(action="type", value="reply"))
    await callback(Notif(action="all"))
    await callback(Notif(action="grouping"))
    await callback(Sett(action="interval", value="300"))
    await callback(Sett(action="quiet_toggle"))
    await callback(Sett(action="quiet_start", value="22"))
    await callback(Sett(action="errors"))
    await callback(Sett(action="tz_manual"))
    out = await message("UTC+3")
    assert "Часовой пояс сохранён" in texts(out)

    async with sf() as session:
        user = await session.scalar(select(User).where(User.telegram_id == OWNER))
    assert not user.notify_reply and not user.notifications_enabled and user.grouping_enabled
    assert user.poll_interval_seconds == 300
    assert user.quiet_hours_enabled and user.quiet_start_hour == 22
    assert not user.error_notifications_enabled
    assert user.timezone == "Etc/GMT-3"

    out = await callback(Sett(action="interval", value="7"))
    assert any(isinstance(r, AnswerCallbackQuery) and r.show_alert for r in out)


async def test_history_search_and_dates(tg) -> None:
    message, callback, _ = tg
    await message("/start")
    await callback(Hist(action="search"))
    out = await message("launch")
    assert "Поиск: launch" in texts(out)
    await callback(Hist(action="dates"))
    out = await message("01.11.2026-10.11.2026")
    assert "01.11.2026" in texts(out)
    out = await message("31.13.2026")  # not in the dates dialog anymore -> main menu
    assert "X Monitor" in texts(out)
    out = await callback(Hist(action="reset"))
    assert "Всё время" in texts(out)
