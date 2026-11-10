"""Polling loop: fetches new posts for due accounts, stores events and queues notifications.

Guarantees:
* events.post_id is unique -> a post is stored once, no matter how often it is fetched;
* notifications (event_id, user_id) is unique -> each user gets each event at most once;
* last_seen_post_id is advanced in the same transaction as the inserts -> restarts resume exactly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import update

from core.config import Settings
from core.database import SessionFactory, session_scope
from models import AccountStatus, EventType, NotificationStatus, Subscription, User, XAccount
from repositories import AccountRepository, EventRepository, NotificationRepository
from services.account_manager import AccountManager
from services.alerts import Alerter
from services.timeutils import utcnow
from services.trading import TradePlanner
from services.x_client import (
    XAccountUnavailable,
    XApiError,
    XAuthError,
    XClient,
    XForbidden,
    XNotFound,
    XPaymentRequired,
    XPost,
    XRateLimited,
)

log = logging.getLogger(__name__)

CLAIM_LEASE = timedelta(minutes=3)
GLOBAL_ERROR_RETRY = timedelta(minutes=5)
MAX_ERROR_BACKOFF = timedelta(minutes=30)
MAX_UNAVAILABLE_BACKOFF = timedelta(hours=6)


@dataclass(slots=True)
class CheckResult:
    account_id: int
    new_events: int = 0
    error: str | None = None


def notification_decision(
    user: User, sub: Subscription, event_type: EventType
) -> tuple[NotificationStatus, str | None]:
    if not user.notifications_enabled:
        return NotificationStatus.SKIPPED, "all notifications disabled"
    if not sub.notify_enabled:
        return NotificationStatus.SKIPPED, "account notifications disabled"
    if not user.wants(event_type):
        return NotificationStatus.SKIPPED, f"{event_type.value} notifications disabled"
    return NotificationStatus.PENDING, None


def event_values(account: XAccount, post: XPost) -> dict:
    ref = post.referenced
    return {
        "post_id": post.id,
        "x_account_id": account.id,
        "event_type": post.type,
        "author_username": account.username,
        "author_name": account.display_name or "",
        "text": post.text,
        "url": post.url_for(account.username),
        "posted_at": post.created_at,
        "in_reply_to_user_id": post.in_reply_to_user_id,
        "in_reply_to_username": post.in_reply_to_username,
        "referenced_post_id": ref.id if ref else None,
        "referenced_author_username": ref.author_username if ref else None,
        "referenced_author_name": ref.author_name if ref else None,
        "referenced_text": ref.text if ref else None,
        "referenced_url": ref.url if ref else None,
        "quoted_post_id": post.quoted.id if post.quoted else None,
        "media": [m.as_dict() for m in post.media],
    }


class Monitor:
    def __init__(
        self,
        session_factory: SessionFactory,
        x_client: XClient,
        alerter: Alerter,
        settings: Settings,
        planner: TradePlanner | None = None,
    ):
        self._sf = session_factory
        self._x = x_client
        self._alerter = alerter
        self._settings = settings
        self._planner = planner
        self._sem = asyncio.Semaphore(settings.monitor_concurrency)
        self.last_tick_at: datetime | None = None

    # ------------------------------------------------------------ scheduling

    async def run_once(self) -> list[CheckResult]:
        now = utcnow()
        self.last_tick_at = now
        async with self._sf() as session:
            ids = await AccountRepository(session).due_account_ids(now, self._settings.monitor_concurrency * 4)
        if not ids:
            return []
        return list(await asyncio.gather(*(self._guarded(i) for i in ids)))

    async def _guarded(self, account_id: int) -> CheckResult:
        async with self._sem:
            try:
                return await self.check_account(account_id)
            except Exception as exc:
                log.exception("unexpected error while checking account %s", account_id)
                return CheckResult(account_id, error=repr(exc))

    def _interval(self, subscribers: list[tuple[Subscription, User]]) -> timedelta:
        seconds = min(
            (u.poll_interval_seconds for _, u in subscribers), default=self._settings.default_poll_interval_seconds
        )
        return timedelta(seconds=max(seconds, self._settings.min_poll_interval_seconds))

    async def _claim(self, account_id: int) -> XAccount | None:
        """Atomically pushes next_check_at forward; only one worker/instance wins the claim."""
        now = utcnow()
        async with session_scope(self._sf) as session:
            claimed = await session.scalar(
                update(XAccount)
                .where(XAccount.id == account_id, XAccount.next_check_at <= now)
                .values(next_check_at=now + CLAIM_LEASE)
                .returning(XAccount)
            )
            if claimed is not None and claimed.last_seen_post_id is None:
                AccountManager.reset_baseline(claimed)
                return None
            return claimed

    # ------------------------------------------------------------ one account

    async def check_account(self, account_id: int) -> CheckResult:
        account = await self._claim(account_id)
        if account is None:
            return CheckResult(account_id)
        since_id = account.last_seen_post_id or 0
        try:
            posts = await self._x.get_user_posts(
                account.x_user_id, since_id, max_pages=self._settings.x_max_pages_per_poll
            )
        except XRateLimited as exc:
            retry_at = datetime.fromtimestamp(exc.reset_at + 1, tz=utcnow().tzinfo)
            log.warning("rate limited, @%s postponed until %s", account.username, retry_at.isoformat())
            await self._reschedule(account_id, retry_at)
            return CheckResult(account_id, error="rate_limited")
        except (XAuthError, XPaymentRequired, XForbidden) as exc:
            kind = {XAuthError: "auth", XPaymentRequired: "payment", XForbidden: "forbidden"}[type(exc)]
            log.error("X API %s error: %s", kind, exc)
            await self._reschedule(account_id, utcnow() + GLOBAL_ERROR_RETRY, error=str(exc))
            await self._alerter.global_error(kind, str(exc))
            return CheckResult(account_id, error=kind)
        except (XNotFound, XAccountUnavailable) as exc:
            await self._record_failure(account_id, str(exc), unavailable=True)
            return CheckResult(account_id, error="unavailable")
        except XApiError as exc:
            await self._record_failure(account_id, str(exc), unavailable=False)
            return CheckResult(account_id, error="api_error")

        return await self._store(account_id, posts)

    async def _store(self, account_id: int, posts: list[XPost]) -> CheckResult:
        now = utcnow()
        new_events = 0
        recovered: XAccount | None = None
        async with session_scope(self._sf) as session:
            repo = AccountRepository(session)
            account = await repo.lock(account_id)
            if account is None:
                return CheckResult(account_id)
            subscribers = await repo.active_subscribers(account_id)
            events = EventRepository(session)
            notifications = NotificationRepository(session)
            last_seen = account.last_seen_post_id or 0
            for post in posts:
                if post.id <= last_seen:
                    continue
                event_id = await events.insert_if_new(event_values(account, post))
                if event_id is None:
                    continue  # already stored earlier -> never notify twice
                new_events += 1
                for sub, user in subscribers:
                    status, reason = notification_decision(user, sub, post.type)
                    await notifications.create(event_id, user.id, user.chat_id, status, reason)
                if self._planner is not None:
                    # Same transaction as the event (restart-safe), inside a savepoint so a
                    # trading problem can never block notifications.
                    try:
                        async with session.begin_nested():
                            await self._planner.plan(session, event_id, account, post, subscribers)
                    except Exception:
                        log.exception("auto-buy planning failed for post %s", post.id)
            if posts:
                account.last_seen_post_id = max(last_seen, posts[-1].id)
            account.last_checked_at = now
            account.next_check_at = now + self._interval(subscribers)
            if account.error_alert_sent:
                recovered = account
            account.status = AccountStatus.ACTIVE
            account.consecutive_errors = 0
            account.error_alert_sent = False
        if new_events:
            log.info("@%s: %d new event(s)", account.username, new_events)
        if recovered is not None:
            await self._alerter.account_recovered(recovered)
        return CheckResult(account_id, new_events=new_events)

    async def _reschedule(self, account_id: int, at: datetime, error: str | None = None) -> None:
        async with session_scope(self._sf) as session:
            values: dict = {"next_check_at": at}
            if error:
                values.update(last_error=error[:1000], last_error_at=utcnow())
            await session.execute(update(XAccount).where(XAccount.id == account_id).values(**values))

    async def _record_failure(self, account_id: int, error: str, *, unavailable: bool) -> None:
        now = utcnow()
        alert: XAccount | None = None
        async with session_scope(self._sf) as session:
            account = await session.get(XAccount, account_id)
            if account is None:
                return
            subscribers = await AccountRepository(session).active_subscribers(account_id)
            account.consecutive_errors += 1
            account.last_error = error[:1000]
            account.last_error_at = now
            account.last_checked_at = now
            account.status = AccountStatus.UNAVAILABLE if unavailable else AccountStatus.ERROR
            cap = MAX_UNAVAILABLE_BACKOFF if unavailable else MAX_ERROR_BACKOFF
            backoff = self._interval(subscribers) * (2 ** min(account.consecutive_errors - 1, 10))
            account.next_check_at = now + min(backoff, cap)
            if account.consecutive_errors >= self._settings.error_notify_threshold and not account.error_alert_sent:
                account.error_alert_sent = True
                alert = account
        log.warning("account %s check failed (%s): %s", account_id, "unavailable" if unavailable else "error", error)
        if alert is not None:
            await self._alerter.account_failed(alert, error, unavailable)
