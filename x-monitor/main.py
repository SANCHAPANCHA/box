"""X Monitor entry point: Telegram bot + monitoring scheduler + health API in one asyncio process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import uvicorn
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import LinkPreviewOptions
from redis.asyncio import Redis

from api.app import create_app
from bot.factory import BOT_COMMANDS, create_bot, create_dispatcher
from bot.sender import TelegramSender
from core.config import get_settings
from core.database import create_engine, create_session_factory
from core.logging import setup_logging
from services.account_manager import AccountManager
from services.alerts import Alerter
from services.cache import Cache
from services.monitor import Monitor
from services.notification import NotificationDispatcher
from services.x_client import XClient
from tasks.scheduler import build_scheduler

log = logging.getLogger("x-monitor")


class _ApiServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # signals are handled by main()
        yield


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values(), settings.log_json)
    if not settings.allowed_user_ids:
        log.warning("TELEGRAM_ALLOWED_USER_IDS is empty: the bot will reject every user")

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    redis: Redis | None = None
    storage: BaseStorage = MemoryStorage()
    if settings.redis_url:
        redis = Redis.from_url(settings.redis_url)
        await redis.ping()
        storage = RedisStorage(redis)  # FSM state (dialogs, history filters) survives restarts
    else:
        log.warning("REDIS_URL not set: dialog state is kept in memory")

    x_client = XClient(
        settings.x_bearer_token.get_secret_value(),
        settings.x_api_base_url,
        dialect=settings.x_api_param_dialect,
        timeout=settings.x_request_timeout_seconds,
        max_retries=settings.x_max_retries,
        fetch_references=settings.x_fetch_references,
    )
    bot = create_bot(settings.telegram_bot_token.get_secret_value(), settings.telegram_api_base_url)
    me = await bot.get_me()
    log.info("Telegram bot @%s started", me.username)

    async def send_alert(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))

    monitor = Monitor(session_factory, x_client, Alerter(session_factory, send_alert), settings)
    dispatcher = NotificationDispatcher(session_factory, TelegramSender(bot))
    await dispatcher.recover()

    dp = create_dispatcher(settings, session_factory, storage)
    await bot.set_my_commands(BOT_COMMANDS)
    scheduler = build_scheduler(monitor, dispatcher, settings)
    scheduler.start()

    tasks = [
        asyncio.create_task(
            dp.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                account_manager=AccountManager(x_client, Cache(redis)),
                settings=settings,
                allowed_updates=dp.resolve_used_update_types(),
            ),
            name="telegram-polling",
        )
    ]
    api_server: _ApiServer | None = None
    if settings.api_enabled:
        api_server = _ApiServer(
            uvicorn.Config(
                create_app(session_factory, monitor, x_client),
                host=settings.api_host,
                port=settings.api_port,
                log_level="warning",
                access_log=False,
            )
        )
        tasks.append(asyncio.create_task(api_server.serve(), name="health-api"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        if task is not stop_task and task.exception():
            log.error("component %s crashed", task.get_name(), exc_info=task.exception())

    log.info("shutting down…")
    scheduler.shutdown(wait=False)
    with contextlib.suppress(RuntimeError):
        await dp.stop_polling()
    if api_server is not None:
        api_server.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    stop_task.cancel()
    await x_client.close()
    await bot.session.close()
    if redis is not None:
        await redis.aclose()
    await engine.dispose()
    log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())
