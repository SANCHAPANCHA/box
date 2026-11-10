from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.database import SessionFactory, create_engine, create_session_factory, session_scope
from models import Base, EventType, User
from services.formatting import RenderedMessage
from services.x_client import XAccountUnavailable, XNotFound, XPost, XUser, snowflake_from_datetime

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5433/xmonitor_test")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        telegram_bot_token="123456:TEST_TOKEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        x_bearer_token="AAAAAAAAAAAAAAAAAAAAAtestbearer",
        telegram_allowed_user_ids="1001,1002",
        database_url=TEST_DATABASE_URL,
        default_timezone="Europe/Moscow",
        min_poll_interval_seconds=15,
        _env_file=None,
    )


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sf(engine: AsyncEngine) -> SessionFactory:
    async with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return create_session_factory(engine)


@pytest.fixture(scope="session")
def bot_env(engine, settings):
    """One Dispatcher per test session: aiogram routers can be attached only once."""
    from aiogram import Bot

    from bot.factory import create_dispatcher
    from tests.tg_helpers import MockedSession

    session = MockedSession()
    bot = Bot("42:TEST-token", session=session)
    dp = create_dispatcher(settings, create_session_factory(engine))
    return bot, dp, session


async def make_user(sf: SessionFactory, telegram_id: int = 1001, **kw) -> User:
    async with session_scope(sf) as session:
        user = User(telegram_id=telegram_id, chat_id=telegram_id, timezone=kw.pop("timezone", "UTC"), **kw)
        session.add(user)
    return user


def post(
    post_id: int,
    type_: EventType = EventType.POST,
    text_: str = "hello",
    created_at: datetime | None = None,
) -> XPost:
    return XPost(
        id=post_id,
        author_id=44196397,
        text=text_,
        created_at=created_at or datetime.now(UTC),
        type=type_,
    )


def future_id(seconds: int = 60) -> int:
    """A post ID that lies after the baseline set when an account is added now."""
    return snowflake_from_datetime(datetime.now(UTC) + timedelta(seconds=seconds))


class FakeX:
    """Stand-in for XClient used by service tests."""

    def __init__(self) -> None:
        self.users: dict[str, XUser] = {}
        self.posts: dict[int, list[XPost]] = {}
        self.errors: dict[int, Exception] = {}
        self.calls: list[tuple[int, int]] = []
        self.lookups = 0

    def add_user(self, username: str, user_id: int = 44196397, name: str = "Elon Musk", protected=False) -> XUser:
        user = XUser(id=user_id, username=username, name=name, avatar_url=None, protected=protected)
        self.users[username.lower()] = user
        return user

    async def get_user_by_username(self, username: str) -> XUser:
        self.lookups += 1
        if username.lower() == "suspended":
            raise XAccountUnavailable("suspended")
        if username.lower() not in self.users:
            raise XNotFound(username)
        return self.users[username.lower()]

    async def get_user_posts(self, user_id: int, since_id: int, *, max_pages: int = 3) -> list[XPost]:
        self.calls.append((user_id, since_id))
        if user_id in self.errors:
            raise self.errors[user_id]
        return sorted((p for p in self.posts.get(user_id, []) if p.id > since_id), key=lambda p: p.id)


class FakeSender:
    def __init__(self) -> None:
        self.events: list[tuple[int, RenderedMessage]] = []
        self.texts: list[tuple[int, str]] = []
        self.fail_with: Exception | None = None

    async def send_event(self, chat_id: int, message: RenderedMessage, *, silent: bool = False) -> int:
        if self.fail_with:
            raise self.fail_with
        self.events.append((chat_id, message))
        return len(self.events) + len(self.texts)

    async def send_text(self, chat_id: int, text: str, *, silent: bool = False) -> int:
        if self.fail_with:
            raise self.fail_with
        self.texts.append((chat_id, text))
        return len(self.events) + len(self.texts)


class AlertRecorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
