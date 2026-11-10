"""Delivers queued notifications (outbox pattern) to Telegram.

Each row goes PENDING -> SENDING (committed) -> SENT. If the process dies while a row is
SENDING it is resolved as SENT on startup: at-most-once delivery, never a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Protocol

from sqlalchemy import select

from core.database import SessionFactory, session_scope
from models import Notification, NotificationStatus, User
from repositories import NotificationRepository
from services.formatting import RenderedMessage, render_digest, render_event
from services.timeutils import is_quiet_time, utcnow

log = logging.getLogger(__name__)

GROUP_THRESHOLD = 3
MAX_ATTEMPTS = 5


class DeliveryError(Exception):
    pass


class RetryLater(DeliveryError):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


class PermanentDeliveryError(DeliveryError):
    """Chat not found, bot blocked by the user, etc."""


class Sender(Protocol):
    async def send_event(self, chat_id: int, message: RenderedMessage, *, silent: bool = False) -> int: ...

    async def send_text(self, chat_id: int, text: str, *, silent: bool = False) -> int: ...


def user_in_quiet_hours(user: User) -> bool:
    return is_quiet_time(utcnow(), user.timezone, user.quiet_hours_enabled, user.quiet_start_hour, user.quiet_end_hour)


class NotificationDispatcher:
    def __init__(self, session_factory: SessionFactory, sender: Sender, batch_size: int = 50, send_delay: float = 0.05):
        self._sf = session_factory
        self._sender = sender
        self._batch = batch_size
        self._delay = send_delay
        self._paused_until = 0.0

    async def recover(self) -> int:
        async with session_scope(self._sf) as session:
            count = await NotificationRepository(session).resolve_stuck_sending(utcnow())
        if count:
            log.warning("%d notification(s) were mid-delivery during shutdown; marked as sent", count)
        return count

    async def _claim_batch(self) -> list[Notification]:
        async with session_scope(self._sf) as session:
            repo = NotificationRepository(session)
            user_ids = list(
                await session.scalars(
                    select(Notification.user_id).where(Notification.status == NotificationStatus.PENDING).distinct()
                )
            )
            if not user_ids:
                return []
            users = list(await session.scalars(select(User).where(User.id.in_(user_ids))))
            quiet = {u.id for u in users if user_in_quiet_hours(u)}
            if len(quiet) == len(users):
                return []
            rows = [n for n in await repo.pending_for_update(self._batch * 4) if n.user_id not in quiet][: self._batch]
            skipped = [n.id for n in rows if not n.user.notifications_enabled or not n.user.wants(n.event.event_type)]
            await repo.mark(skipped, NotificationStatus.SKIPPED, reason="disabled before delivery")
            claimed = [n for n in rows if n.id not in set(skipped)]
            await repo.mark([n.id for n in claimed], NotificationStatus.SENDING, bump_attempts=True)
            return claimed

    async def run_once(self) -> int:
        loop = asyncio.get_running_loop()
        if loop.time() < self._paused_until:
            return 0
        batch = await self._claim_batch()
        if not batch:
            return 0
        by_user: dict[int, list[Notification]] = defaultdict(list)
        for n in batch:
            by_user[n.user_id].append(n)

        sent = 0
        try:
            for items in by_user.values():
                user = items[0].user
                if user.grouping_enabled and len(items) >= GROUP_THRESHOLD:
                    sent += await self._send_digest(user, items)
                else:
                    for n in items:
                        sent += await self._send_single(user, n)
        except RetryLater as exc:
            self._paused_until = loop.time() + exc.seconds
            log.warning("Telegram flood control: pausing delivery for %.0fs", exc.seconds)
            await self._release(batch)  # only rows still in SENDING are affected
        return sent

    async def _release(self, items: list[Notification]) -> None:
        """Put not-yet-sent rows back to PENDING (they were never handed to Telegram)."""
        async with session_scope(self._sf) as session:
            ids = [n.id for n in items]
            await session.execute(
                Notification.__table__.update()
                .where(Notification.id.in_(ids), Notification.status == NotificationStatus.SENDING)
                .values(status=NotificationStatus.PENDING, attempts=Notification.attempts - 1)
            )

    async def _finish(self, items: list[Notification], status: NotificationStatus, **kw) -> None:
        async with session_scope(self._sf) as session:
            await NotificationRepository(session).mark([n.id for n in items], status, **kw)

    async def _deliver(self, items: list[Notification], send) -> int:
        try:
            message_id = await send()
        except RetryLater:
            raise
        except PermanentDeliveryError as exc:
            log.warning("permanent delivery failure for notifications %s: %s", [n.id for n in items], exc)
            await self._finish(items, NotificationStatus.FAILED, reason=str(exc))
            return 0
        except Exception as exc:
            log.warning("delivery failed for notifications %s: %s", [n.id for n in items], exc)
            exhausted = [n for n in items if n.attempts + 1 >= MAX_ATTEMPTS]
            retry = [n for n in items if n not in exhausted]
            await self._finish(exhausted, NotificationStatus.FAILED, reason=str(exc))
            await self._finish(retry, NotificationStatus.PENDING, reason=str(exc))
            return 0
        await self._finish(items, NotificationStatus.SENT, sent_at=utcnow(), message_id=message_id)
        await asyncio.sleep(self._delay)
        return len(items)

    async def _send_single(self, user: User, n: Notification) -> int:
        message = render_event(n.event, user.timezone)
        return await self._deliver([n], lambda: self._sender.send_event(n.chat_id, message))

    async def _send_digest(self, user: User, items: list[Notification]) -> int:
        events = sorted((n.event for n in items), key=lambda e: e.posted_at)
        chunks = render_digest(events, user.timezone)
        chat_id = items[0].chat_id

        async def send_all() -> int:
            message_id = await self._sender.send_text(chat_id, chunks[0])
            for chunk in chunks[1:]:
                # The first part is already delivered; never resend it because of a later failure.
                try:
                    message_id = await self._sender.send_text(chat_id, chunk)
                except Exception:
                    log.exception("failed to deliver a digest continuation to chat %s", chat_id)
                    break
            return message_id

        return await self._deliver(items, send_all)
