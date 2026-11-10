from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.config import Settings
from services.monitor import Monitor
from services.notification import NotificationDispatcher
from services.timeutils import utcnow

log = logging.getLogger(__name__)


def build_scheduler(monitor: Monitor, dispatcher: NotificationDispatcher, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 30, "next_run_time": utcnow()}
    scheduler.add_job(monitor.run_once, "interval", seconds=settings.monitor_tick_seconds, id="monitor", **common)
    scheduler.add_job(
        dispatcher.run_once, "interval", seconds=settings.dispatcher_tick_seconds, id="dispatcher", **common
    )
    return scheduler
