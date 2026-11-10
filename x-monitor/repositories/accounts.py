from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import AccountStatus, Event, Subscription, User, XAccount


@dataclass(slots=True)
class SubscriptionView:
    subscription: Subscription
    account: XAccount
    events_count: int


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, account_id: int) -> XAccount | None:
        return await self.session.get(XAccount, account_id)

    async def get_by_x_user_id(self, x_user_id: int) -> XAccount | None:
        return await self.session.scalar(select(XAccount).where(XAccount.x_user_id == x_user_id))

    async def lock(self, account_id: int) -> XAccount | None:
        """Row lock so two pollers never process the same account concurrently."""
        return await self.session.scalar(
            select(XAccount).where(XAccount.id == account_id).with_for_update(skip_locked=True)
        )

    # ------------------------------------------------------------ subscriptions

    async def get_subscription(self, user_id: int, account_id: int) -> Subscription | None:
        return await self.session.scalar(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.x_account_id == account_id)
            .options(selectinload(Subscription.account))
        )

    async def count_subscriptions(self, user_id: int) -> int:
        return await self.session.scalar(select(func.count()).where(Subscription.user_id == user_id)) or 0

    async def list_subscriptions(
        self, user_id: int, *, offset: int = 0, limit: int | None = None
    ) -> list[SubscriptionView]:
        events_count = (
            select(func.count(Event.id)).where(Event.x_account_id == XAccount.id).correlate(XAccount).scalar_subquery()
        )
        stmt: Select = (
            select(Subscription, XAccount, events_count)
            .join(XAccount, XAccount.id == Subscription.x_account_id)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at, Subscription.id)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = await self.session.execute(stmt)
        return [SubscriptionView(s, a, int(c or 0)) for s, a, c in rows.all()]

    async def delete_subscription(self, user_id: int, account_id: int) -> bool:
        result = await self.session.execute(
            delete(Subscription).where(Subscription.user_id == user_id, Subscription.x_account_id == account_id)
        )
        return (result.rowcount or 0) > 0

    async def active_subscribers(self, account_id: int) -> list[tuple[Subscription, User]]:
        rows = await self.session.execute(
            select(Subscription, User)
            .join(User, User.id == Subscription.user_id)
            .where(Subscription.x_account_id == account_id, Subscription.is_paused.is_(False))
        )
        return [(s, u) for s, u in rows.all()]

    # ------------------------------------------------------------ monitoring

    async def due_account_ids(self, now: datetime, limit: int) -> list[int]:
        has_active_sub = exists().where(
            and_(Subscription.x_account_id == XAccount.id, Subscription.is_paused.is_(False))
        )
        rows = await self.session.scalars(
            select(XAccount.id)
            .where(has_active_sub, XAccount.next_check_at <= now)
            .order_by(XAccount.next_check_at)
            .limit(limit)
        )
        return list(rows)

    async def count_monitored(self) -> int:
        has_active_sub = exists().where(
            and_(Subscription.x_account_id == XAccount.id, Subscription.is_paused.is_(False))
        )
        return await self.session.scalar(select(func.count(XAccount.id)).where(has_active_sub)) or 0

    async def count_by_status(self, user_id: int) -> dict[AccountStatus, int]:
        rows = await self.session.execute(
            select(XAccount.status, func.count())
            .join(Subscription, Subscription.x_account_id == XAccount.id)
            .where(Subscription.user_id == user_id)
            .group_by(XAccount.status)
        )
        return {status: int(n) for status, n in rows.all()}
