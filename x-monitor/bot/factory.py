from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.handlers import accounts, fallback, history, notifications, start, stats
from bot.handlers import settings as settings_handlers
from bot.middlewares import AuthMiddleware, DbSessionMiddleware
from core.config import Settings
from core.database import SessionFactory


def create_bot(token: str, api_base_url: str | None = None, **kwargs) -> Bot:
    if api_base_url:
        kwargs["session"] = AiohttpSession(api=TelegramAPIServer.from_base(api_base_url))
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), **kwargs)


def create_dispatcher(
    settings: Settings, session_factory: SessionFactory, storage: BaseStorage | None = None
) -> Dispatcher:
    dp = Dispatcher(storage=storage or MemoryStorage())
    dp.update.outer_middleware(AuthMiddleware(settings.allowed_user_ids))
    dp.update.outer_middleware(DbSessionMiddleware(session_factory, settings))
    dp.include_routers(
        start.router,
        accounts.router,
        notifications.router,
        settings_handlers.router,
        history.router,
        stats.router,
        fallback.router,  # must stay last
    )
    return dp


BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить ввод"),
]
