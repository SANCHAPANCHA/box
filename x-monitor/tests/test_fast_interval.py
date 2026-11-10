"""Monitor honours a 5 s interval: the account becomes due again ~5 s after a check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.database import session_scope
from models import XAccount
from services.account_manager import AccountManager
from services.alerts import Alerter
from services.cache import Cache
from services.monitor import Monitor
from tests.conftest import AlertRecorder, FakeX, make_user


async def test_five_second_interval_schedules_next_check(sf, settings) -> None:
    fast = settings.model_copy(update={"min_poll_interval_seconds": 5})
    x = FakeX()
    x.add_user("elonmusk", 44196397)
    user = await make_user(sf, poll_interval_seconds=5)
    async with session_scope(sf) as session:
        await AccountManager(x, Cache()).add(session, user, "@elonmusk")
    monitor = Monitor(sf, x, Alerter(sf, AlertRecorder()), fast)

    before = datetime.now(UTC)
    await monitor.run_once()
    async with sf() as session:
        account = await session.scalar(select(XAccount))
    delay = account.next_check_at - before
    assert timedelta(seconds=4) <= delay <= timedelta(seconds=6), delay

    # With the old 15 s server minimum the same user setting is clamped to 15 s.
    slow_monitor = Monitor(sf, x, Alerter(sf, AlertRecorder()), settings)
    async with session_scope(sf) as session:
        acc = await session.scalar(select(XAccount))
        acc.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
    before = datetime.now(UTC)
    await slow_monitor.run_once()
    async with sf() as session:
        account = await session.scalar(select(XAccount))
    assert account.next_check_at - before >= timedelta(seconds=14)
