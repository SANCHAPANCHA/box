from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from core.database import session_scope
from models import AccountStatus, Event, EventType, Notification, NotificationStatus, Subscription, XAccount
from repositories import EventRepository, HistoryFilter
from services.account_manager import AccountManager, AddStatus
from services.alerts import Alerter
from services.cache import Cache
from services.monitor import Monitor
from services.x_client import XNotFound, XPaymentRequired, snowflake_from_datetime
from tests.conftest import AlertRecorder, FakeX, future_id, make_user, post

UID = 44196397


async def _setup(sf, settings, *, username="elonmusk", **user_kw):
    x = FakeX()
    x.add_user(username, UID)
    alerts = AlertRecorder()
    user = await make_user(sf, **user_kw)
    manager = AccountManager(x, Cache())
    async with session_scope(sf) as session:
        result = await manager.add(session, user, f"https://x.com/{username}")
    monitor = Monitor(sf, x, Alerter(sf, alerts), settings)
    return x, alerts, user, manager, monitor, result


async def _make_due(sf) -> None:
    async with session_scope(sf) as session:
        await session.execute(update(XAccount).values(next_check_at=datetime.now(UTC) - timedelta(seconds=1)))


async def _counts(sf) -> tuple[int, int]:
    async with sf() as session:
        events = await session.scalar(select(func.count()).select_from(Event))
        notifications = await session.scalar(select(func.count()).select_from(Notification))
    return events, notifications


async def test_add_account_sets_baseline_and_rejects_duplicates(sf, settings) -> None:
    before = snowflake_from_datetime(datetime.now(UTC))
    x, _, user, manager, _, result = await _setup(sf, settings)
    assert result.status is AddStatus.ADDED
    assert result.account.display_name == "Elon Musk"
    assert result.account.last_seen_post_id >= before

    async with session_scope(sf) as session:
        again = await manager.add(session, user, "@ElonMusk")
        missing = await manager.add(session, user, "@nobody")
        invalid = await manager.add(session, user, "not a user!")
        suspended = await manager.add(session, user, "@suspended")
    assert again.status is AddStatus.ALREADY
    assert missing.status is AddStatus.NOT_FOUND
    assert invalid.status is AddStatus.INVALID
    assert suspended.status is AddStatus.UNAVAILABLE
    assert x.lookups == 3  # second @elonmusk lookup came from cache

    x.add_user("locked", 5, protected=True)
    async with session_scope(sf) as session:
        assert (await manager.add(session, user, "locked")).status is AddStatus.PROTECTED


async def test_old_posts_are_not_sent_on_first_add(sf, settings) -> None:
    x, _, _, _, monitor, _ = await _setup(sf, settings)
    old = snowflake_from_datetime(datetime.now(UTC) - timedelta(days=1))
    x.posts[UID] = [post(old), post(old + 1)]
    await monitor.run_once()
    assert await _counts(sf) == (0, 0)


async def test_new_posts_are_stored_once_even_after_restart(sf, settings) -> None:
    x, _, _, _, monitor, _ = await _setup(sf, settings)
    x.posts[UID] = [post(future_id(1)), post(future_id(2), EventType.REPLY), post(future_id(3), EventType.REPOST)]
    results = await monitor.run_once()
    assert results[0].new_events == 3
    assert await _counts(sf) == (3, 3)

    # The account is not due again until its interval elapses.
    assert await monitor.run_once() == []

    # Simulate a crash before last_seen_post_id was advanced, then a fresh process.
    async with session_scope(sf) as session:
        await session.execute(update(XAccount).values(last_seen_post_id=future_id(-3600)))
    await _make_due(sf)
    restarted = Monitor(sf, x, Alerter(sf, AlertRecorder()), settings)
    results = await restarted.run_once()
    assert results[0].new_events == 0
    assert await _counts(sf) == (3, 3)

    async with sf() as session:
        statuses = set(await session.scalars(select(Notification.status)))
        account = await session.scalar(select(XAccount))
    assert statuses == {NotificationStatus.PENDING}
    assert account.last_seen_post_id == max(p.id for p in x.posts[UID])
    assert account.next_check_at > datetime.now(UTC)


async def test_notification_preferences(sf, settings) -> None:
    x, _, user, _, monitor, _ = await _setup(sf, settings, notify_reply=False)
    x.posts[UID] = [post(future_id(1)), post(future_id(2), EventType.REPLY)]
    await monitor.run_once()
    async with sf() as session:
        rows = dict((await session.execute(select(Event.event_type, Notification.status).join(Event))).all())
    assert rows == {EventType.POST: NotificationStatus.PENDING, EventType.REPLY: NotificationStatus.SKIPPED}

    async with session_scope(sf) as session:
        await session.execute(update(Subscription).values(notify_enabled=False))
    x.posts[UID].append(post(future_id(3), EventType.QUOTE))
    await _make_due(sf)
    await monitor.run_once()
    async with sf() as session:
        quote_status = await session.scalar(
            select(Notification.status).join(Event).where(Event.event_type == EventType.QUOTE)
        )
    assert quote_status is NotificationStatus.SKIPPED


async def test_paused_and_removed_accounts(sf, settings) -> None:
    x, _, user, manager, monitor, result = await _setup(sf, settings)
    x.posts[UID] = [post(future_id(1))]
    await monitor.run_once()

    async with session_scope(sf) as session:
        await manager.set_paused(session, user, result.account.id, True)
    await _make_due(sf)
    x.calls.clear()
    assert await monitor.run_once() == []
    assert x.calls == []

    async with session_scope(sf) as session:
        assert await manager.remove(session, user, result.account.id)
    # History survives deletion
    async with sf() as session:
        events, total = await EventRepository(session).history(user.id, HistoryFilter(), offset=0, limit=10)
    assert total == 1 and events[0].author_username == "elonmusk"


async def test_account_errors_backoff_and_alerts(sf, settings) -> None:
    x, alerts, _, _, monitor, _ = await _setup(sf, settings)
    x.errors[UID] = XNotFound("gone")
    for _ in range(settings.error_notify_threshold + 1):
        await _make_due(sf)
        await monitor.run_once()
    async with sf() as session:
        account = await session.scalar(select(XAccount))
    assert account.status is AccountStatus.UNAVAILABLE
    assert account.consecutive_errors == settings.error_notify_threshold + 1
    assert account.next_check_at > datetime.now(UTC) + timedelta(minutes=5)
    assert len(alerts.sent) == 1 and "Ошибка мониторинга" in alerts.sent[0][1]

    del x.errors[UID]
    await _make_due(sf)
    await monitor.run_once()
    async with sf() as session:
        account = await session.scalar(select(XAccount))
    assert account.status is AccountStatus.ACTIVE and account.consecutive_errors == 0
    assert "восстановлен" in alerts.sent[-1][1]


async def test_global_errors_are_throttled(sf, settings) -> None:
    x, alerts, _, _, monitor, _ = await _setup(sf, settings)
    x.errors[UID] = XPaymentRequired("402")
    for _ in range(3):
        await _make_due(sf)
        await monitor.run_once()
    assert len(alerts.sent) == 1 and "402" in alerts.sent[0][1]


async def test_two_users_share_account(sf, settings) -> None:
    x, _, _, manager, monitor, _ = await _setup(sf, settings)
    second = await make_user(sf, telegram_id=1002)
    async with session_scope(sf) as session:
        assert (await manager.add(session, second, "@elonmusk")).status is AddStatus.ADDED
    x.posts[UID] = [post(future_id(1))]
    await _make_due(sf)
    await monitor.run_once()
    assert await _counts(sf) == (1, 2)
    assert len(x.calls) == 1  # one API request serves both users
