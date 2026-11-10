from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from core.database import session_scope
from models import Event, EventType, Notification, NotificationStatus, User, XAccount
from repositories import EventRepository, HistoryFilter, NotificationRepository
from services.notification import NotificationDispatcher, PermanentDeliveryError, RetryLater
from tests.conftest import FakeSender, make_user


async def _seed(sf, user: User, n: int, *, start: int = 1, account_username: str = "elonmusk", **event_kw) -> None:
    async with session_scope(sf) as session:
        account = await session.scalar(select(XAccount).where(XAccount.username == account_username))
        if account is None:
            account = XAccount(x_user_id=start * 1000, username=account_username, display_name=account_username)
            session.add(account)
            await session.flush()
        events = EventRepository(session)
        notifications = NotificationRepository(session)
        for i in range(start, start + n):
            values = dict(
                post_id=i,
                x_account_id=account.id,
                event_type=EventType.POST,
                author_username=account_username,
                author_name="Name",
                text=f"post number {i}",
                url=f"https://x.com/{account_username}/status/{i}",
                posted_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC) + timedelta(minutes=i),
                media=[],
            )
            values.update(event_kw)
            event_id = await events.insert_if_new(values)
            await notifications.create(event_id, user.id, user.chat_id, NotificationStatus.PENDING)


async def _statuses(sf) -> list[NotificationStatus]:
    async with sf() as session:
        return list(await session.scalars(select(Notification.status).order_by(Notification.id)))


async def test_delivers_pending_once(sf) -> None:
    user = await make_user(sf)
    await _seed(sf, user, 2)
    sender = FakeSender()
    dispatcher = NotificationDispatcher(sf, sender, send_delay=0)
    assert await dispatcher.run_once() == 2
    assert await dispatcher.run_once() == 0
    assert len(sender.events) == 2 and sender.events[0][0] == user.chat_id
    assert await _statuses(sf) == [NotificationStatus.SENT] * 2
    async with sf() as session:
        assert await session.scalar(select(Notification.telegram_message_id).limit(1))


async def test_quiet_hours_defer_delivery(sf) -> None:
    hour = datetime.now(UTC).hour
    user = await make_user(sf, quiet_hours_enabled=True, quiet_start_hour=hour, quiet_end_hour=(hour + 2) % 24)
    await _seed(sf, user, 1)
    sender = FakeSender()
    dispatcher = NotificationDispatcher(sf, sender, send_delay=0)
    assert await dispatcher.run_once() == 0
    assert await _statuses(sf) == [NotificationStatus.PENDING]

    async with session_scope(sf) as session:
        await session.execute(update(User).values(quiet_hours_enabled=False))
    assert await dispatcher.run_once() == 1


async def test_grouping_sends_digest(sf) -> None:
    user = await make_user(sf, grouping_enabled=True)
    await _seed(sf, user, 4)
    sender = FakeSender()
    assert await NotificationDispatcher(sf, sender, send_delay=0).run_once() == 4
    assert sender.events == [] and len(sender.texts) == 1
    assert "Сводка активности X" in sender.texts[0][1] and "post number 4" in sender.texts[0][1]


async def test_recover_never_resends(sf) -> None:
    user = await make_user(sf)
    await _seed(sf, user, 1)
    async with session_scope(sf) as session:
        await session.execute(update(Notification).values(status=NotificationStatus.SENDING))
    sender = FakeSender()
    dispatcher = NotificationDispatcher(sf, sender, send_delay=0)
    assert await dispatcher.recover() == 1
    assert await dispatcher.run_once() == 0
    assert sender.events == []
    assert await _statuses(sf) == [NotificationStatus.SENT]


async def test_flood_control_and_failures(sf) -> None:
    user = await make_user(sf)
    await _seed(sf, user, 1)
    sender = FakeSender()
    sender.fail_with = RetryLater(30)
    dispatcher = NotificationDispatcher(sf, sender, send_delay=0)
    assert await dispatcher.run_once() == 0
    assert await _statuses(sf) == [NotificationStatus.PENDING]
    sender.fail_with = None
    assert await dispatcher.run_once() == 0  # paused for 30 s

    fresh = NotificationDispatcher(sf, sender, send_delay=0)
    sender.fail_with = PermanentDeliveryError("bot was blocked by the user")
    assert await fresh.run_once() == 0
    assert await _statuses(sf) == [NotificationStatus.FAILED]


async def test_transient_errors_retry_then_fail(sf) -> None:
    user = await make_user(sf)
    await _seed(sf, user, 1)
    sender = FakeSender()
    sender.fail_with = RuntimeError("network")
    dispatcher = NotificationDispatcher(sf, sender, send_delay=0)
    for _ in range(4):
        await dispatcher.run_once()
        assert await _statuses(sf) == [NotificationStatus.PENDING]
    await dispatcher.run_once()
    assert await _statuses(sf) == [NotificationStatus.FAILED]


async def test_disabled_before_delivery_is_skipped(sf) -> None:
    user = await make_user(sf, notifications_enabled=False)
    await _seed(sf, user, 1)
    sender = FakeSender()
    assert await NotificationDispatcher(sf, sender, send_delay=0).run_once() == 0
    assert await _statuses(sf) == [NotificationStatus.SKIPPED]


async def test_history_filters_and_stats(sf) -> None:
    user = await make_user(sf)
    other = await make_user(sf, telegram_id=1002)
    await _seed(sf, user, 3)
    await _seed(sf, user, 2, start=10, account_username="nasa", event_type=EventType.REPLY, text="100% liftoff_ok")
    await _seed(sf, other, 1, start=50, account_username="private")

    async with sf() as session:
        repo = EventRepository(session)
        _, total = await repo.history(user.id, HistoryFilter(), offset=0, limit=10)
        assert total == 5  # the other user's events are not visible

        nasa = await session.scalar(select(XAccount.id).where(XAccount.username == "nasa"))
        events, total = await repo.history(user.id, HistoryFilter(account_id=nasa), offset=0, limit=10)
        assert total == 2 and {e.author_username for e in events} == {"nasa"}

        _, total = await repo.history(user.id, HistoryFilter(event_type=EventType.REPLY), offset=0, limit=10)
        assert total == 2
        _, total = await repo.history(user.id, HistoryFilter(search="100%"), offset=0, limit=10)
        assert total == 2
        _, total = await repo.history(user.id, HistoryFilter(search="0% l"), offset=0, limit=10)
        assert total == 2
        _, total = await repo.history(user.id, HistoryFilter(search="number 2"), offset=0, limit=10)
        assert total == 1

        newest, _ = await repo.history(user.id, HistoryFilter(), offset=0, limit=1)
        oldest, _ = await repo.history(user.id, HistoryFilter(newest_first=False), offset=0, limit=1)
        assert newest[0].post_id == 11 and oldest[0].post_id == 1

        start = datetime(2026, 11, 10, 9, 5, tzinfo=UTC)
        ranged, total = await repo.history(
            user.id, HistoryFilter(date_from=start, date_to=start + timedelta(minutes=6)), offset=0, limit=10
        )
        assert total == 1 and ranged[0].post_id == 10

        page2, total = await repo.history(user.id, HistoryFilter(), offset=4, limit=4)
        assert total == 5 and len(page2) == 1

        stats = await repo.period_stats(user.id, None)
        assert stats.total == 5
        assert stats.by_type == {EventType.POST: 3, EventType.REPLY: 2}
        assert [(a.username, n) for a, n in stats.top_accounts] == [("elonmusk", 3), ("nasa", 2)]
        assert (await repo.period_stats(user.id, datetime(2026, 11, 10, 9, 10, tzinfo=UTC))).total == 2
        assert {a.username for a in await repo.history_accounts(user.id)} == {"elonmusk", "nasa"}

        event = await session.scalar(select(Event).limit(1))
        assert event is not None
