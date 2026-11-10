from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import Event, EventType, Notification, NotificationStatus, XAccount


@dataclass(slots=True)
class HistoryFilter:
    account_id: int | None = None
    event_type: EventType | None = None
    search: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    newest_first: bool = True


@dataclass(slots=True)
class PeriodStats:
    total: int = 0
    by_type: dict[EventType, int] = field(default_factory=dict)
    top_accounts: list[tuple[XAccount, int]] = field(default_factory=list)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_if_new(self, values: dict) -> int | None:
        """INSERT ... ON CONFLICT (post_id) DO NOTHING. Returns the new id or None if already stored."""
        stmt = insert(Event).values(**values).on_conflict_do_nothing(index_elements=[Event.post_id]).returning(Event.id)
        return await self.session.scalar(stmt)

    async def get(self, event_id: int) -> Event | None:
        return await self.session.get(Event, event_id)

    # ------------------------------------------------------------ history (scoped to one user)

    def _user_events(self, user_id: int, flt: HistoryFilter) -> Select:
        stmt = (
            select(Event).join(Notification, Notification.event_id == Event.id).where(Notification.user_id == user_id)
        )
        if flt.account_id:
            stmt = stmt.where(Event.x_account_id == flt.account_id)
        if flt.event_type:
            stmt = stmt.where(Event.event_type == flt.event_type)
        if flt.search:
            pattern = f"%{_escape_like(flt.search)}%"
            stmt = stmt.where(
                Event.text.ilike(pattern, escape="\\") | Event.referenced_text.ilike(pattern, escape="\\")
            )
        if flt.date_from:
            stmt = stmt.where(Event.posted_at >= flt.date_from)
        if flt.date_to:
            stmt = stmt.where(Event.posted_at < flt.date_to)
        return stmt

    async def history(self, user_id: int, flt: HistoryFilter, *, offset: int, limit: int) -> tuple[list[Event], int]:
        base = self._user_events(user_id, flt)
        total = await self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        order = Event.posted_at.desc() if flt.newest_first else Event.posted_at.asc()
        rows = await self.session.scalars(base.order_by(order, Event.id).offset(offset).limit(limit))
        return list(rows), int(total)

    async def history_accounts(self, user_id: int) -> list[XAccount]:
        """Accounts that appear in the user's history, including ones no longer monitored."""
        rows = await self.session.scalars(
            select(XAccount)
            .where(
                XAccount.id.in_(
                    select(Event.x_account_id)
                    .join(Notification, Notification.event_id == Event.id)
                    .where(Notification.user_id == user_id)
                )
            )
            .order_by(XAccount.username)
        )
        return list(rows)

    # ------------------------------------------------------------ statistics

    async def period_stats(self, user_id: int, since: datetime | None, top: int = 5) -> PeriodStats:
        flt = HistoryFilter(date_from=since)
        sub = self._user_events(user_id, flt).subquery()
        rows = await self.session.execute(select(sub.c.event_type, func.count()).group_by(sub.c.event_type))
        stats = PeriodStats()
        for event_type, count in rows.all():
            stats.by_type[EventType(event_type)] = int(count)
        stats.total = sum(stats.by_type.values())
        top_rows = await self.session.execute(
            select(XAccount, func.count().label("n"))
            .join(sub, sub.c.x_account_id == XAccount.id)
            .group_by(XAccount.id)
            .order_by(func.count().desc(), XAccount.username)
            .limit(top)
        )
        stats.top_accounts = [(acc, int(n)) for acc, n in top_rows.all()]
        return stats

    async def count_for_account(self, account_id: int) -> dict[EventType, int]:
        rows = await self.session.execute(
            select(Event.event_type, func.count()).where(Event.x_account_id == account_id).group_by(Event.event_type)
        )
        return {EventType(t): int(n) for t, n in rows.all()}

    async def notifications_sent_since(self, user_id: int, since: datetime) -> int:
        return (
            await self.session.scalar(
                select(func.count()).where(
                    Notification.user_id == user_id,
                    Notification.status == NotificationStatus.SENT,
                    Notification.sent_at >= since,
                )
            )
            or 0
        )
