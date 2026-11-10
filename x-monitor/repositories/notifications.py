from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import KeyValue, Notification, NotificationStatus


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self, event_id: int, user_id: int, chat_id: int, status: NotificationStatus, reason: str | None = None
    ) -> None:
        stmt = (
            insert(Notification)
            .values(event_id=event_id, user_id=user_id, chat_id=chat_id, status=status, reason=reason)
            .on_conflict_do_nothing(constraint="uq_notifications_event_user")
        )
        await self.session.execute(stmt)

    async def pending_for_update(self, limit: int) -> list[Notification]:
        """Locks pending rows (SKIP LOCKED) so parallel dispatchers never take the same notification."""
        rows = await self.session.scalars(
            select(Notification)
            .where(Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Notification)
            .options(selectinload(Notification.event), selectinload(Notification.user))
        )
        return list(rows)

    async def mark(
        self,
        ids: list[int],
        status: NotificationStatus,
        *,
        sent_at: datetime | None = None,
        reason: str | None = None,
        message_id: int | None = None,
        bump_attempts: bool = False,
    ) -> None:
        if not ids:
            return
        values: dict = {"status": status}
        if sent_at is not None:
            values["sent_at"] = sent_at
        if reason is not None:
            values["reason"] = reason[:256]
        if message_id is not None:
            values["telegram_message_id"] = message_id
        if bump_attempts:
            values["attempts"] = Notification.attempts + 1
        await self.session.execute(
            update(Notification).where(Notification.id.in_(ids)).values(**values),
            execution_options={"synchronize_session": False},
        )

    async def resolve_stuck_sending(self, now: datetime) -> int:
        """After a crash we cannot know whether a 'sending' message reached Telegram.
        Treat it as sent: a lost notification is preferable to a duplicate."""
        result = await self.session.execute(
            update(Notification)
            .where(Notification.status == NotificationStatus.SENDING)
            .values(status=NotificationStatus.SENT, sent_at=now, reason="delivery state unknown after restart")
        )
        return result.rowcount or 0


class KeyValueRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str) -> dict | None:
        row = await self.session.get(KeyValue, key)
        return row.value if row else None

    async def set(self, key: str, value: dict) -> None:
        stmt = insert(KeyValue).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(
            index_elements=[KeyValue.key], set_={"value": value, "updated_at": func.now()}
        )
        await self.session.execute(stmt)
