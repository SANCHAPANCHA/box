from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import AccountStatus, Subscription, User, XAccount
from repositories import AccountRepository
from services.cache import Cache
from services.timeutils import utcnow
from services.x_client import (
    XAccountUnavailable,
    XApiError,
    XClient,
    XNotFound,
    XRateLimited,
    XUser,
    snowflake_from_datetime,
)

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/(?:#!/)?@?([A-Za-z0-9_]{1,15})(?:[/?#].*)?$",
    re.IGNORECASE,
)
# Paths on x.com that look like usernames but are not profiles.
RESERVED = {
    "home", "explore", "notifications", "messages", "settings", "search", "i", "intent", "share",
    "compose", "login", "logout", "signup", "tos", "privacy", "hashtag", "jobs", "lists", "who_to_follow",
}  # fmt: skip
USER_CACHE_TTL = 3600


def parse_username(raw: str) -> str | None:
    """'@elonmusk', 'elonmusk', 'https://x.com/elonmusk', 'twitter.com/elonmusk/status/1' -> 'elonmusk'."""
    value = raw.strip()
    if not value:
        return None
    match = _URL_RE.match(value)
    if match:
        candidate = match.group(1)
    elif "/" in value or "." in value:
        return None
    else:
        candidate = value.lstrip("@")
    if not USERNAME_RE.match(candidate) or candidate.lower() in RESERVED:
        return None
    return candidate


def split_inputs(text: str) -> list[str]:
    return [p for p in re.split(r"[\s,;]+", text) if p]


class AddStatus(enum.Enum):
    ADDED = "added"
    ALREADY = "already"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    PROTECTED = "protected"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    API_ERROR = "api_error"


@dataclass(slots=True)
class AddResult:
    status: AddStatus
    input: str
    account: XAccount | None = None
    detail: str | None = None


class AccountManager:
    def __init__(self, x_client: XClient, cache: Cache) -> None:
        self.x = x_client
        self.cache = cache

    async def lookup(self, username: str) -> XUser:
        key = f"xuser:{username.lower()}"
        cached = await self.cache.get(key)
        if cached:
            return XUser(**cached)
        user = await self.x.get_user_by_username(username)
        await self.cache.set(
            key,
            {
                "id": user.id,
                "username": user.username,
                "name": user.name,
                "avatar_url": user.avatar_url,
                "protected": user.protected,
            },
            USER_CACHE_TTL,
        )
        return user

    async def add(self, session: AsyncSession, user: User, raw: str) -> AddResult:
        username = parse_username(raw)
        if not username:
            return AddResult(AddStatus.INVALID, raw)
        try:
            x_user = await self.lookup(username)
        except XNotFound:
            return AddResult(AddStatus.NOT_FOUND, raw)
        except XAccountUnavailable as exc:
            return AddResult(AddStatus.UNAVAILABLE, raw, detail=str(exc))
        except XRateLimited:
            return AddResult(AddStatus.RATE_LIMITED, raw)
        except XApiError as exc:
            log.warning("lookup of @%s failed: %s", username, exc)
            return AddResult(AddStatus.API_ERROR, raw, detail=type(exc).__name__)
        if x_user.protected:
            return AddResult(AddStatus.PROTECTED, raw)

        repo = AccountRepository(session)
        account = await repo.get_by_x_user_id(x_user.id)
        if account is None:
            account = XAccount(x_user_id=x_user.id, username=x_user.username)
            session.add(account)
        account.username = x_user.username
        account.display_name = x_user.name
        account.avatar_url = x_user.avatar_url
        account.protected = x_user.protected
        await session.flush()

        if account.id and await repo.get_subscription(user.id, account.id):
            return AddResult(AddStatus.ALREADY, raw, account=account)

        active_subs = await session.scalar(
            select(func.count()).where(Subscription.x_account_id == account.id, Subscription.is_paused.is_(False))
        )
        if not active_subs:
            # Start (or restart) monitoring from "now" so older posts are never sent as new.
            self.reset_baseline(account)
        session.add(Subscription(user_id=user.id, x_account_id=account.id))
        await session.flush()
        return AddResult(AddStatus.ADDED, raw, account=account)

    @staticmethod
    def reset_baseline(account: XAccount) -> None:
        now = utcnow()
        baseline = snowflake_from_datetime(now)
        account.last_seen_post_id = max(account.last_seen_post_id or 0, baseline)
        account.status = AccountStatus.ACTIVE
        account.consecutive_errors = 0
        account.last_error = None
        account.error_alert_sent = False
        account.next_check_at = now

    async def remove(self, session: AsyncSession, user: User, account_id: int) -> bool:
        return await AccountRepository(session).delete_subscription(user.id, account_id)

    async def set_paused(self, session: AsyncSession, user: User, account_id: int, paused: bool) -> bool:
        repo = AccountRepository(session)
        sub = await repo.get_subscription(user.id, account_id)
        if sub is None:
            return False
        if sub.is_paused and not paused:
            others = await session.scalar(
                select(func.count()).where(
                    Subscription.x_account_id == account_id,
                    Subscription.is_paused.is_(False),
                    Subscription.id != sub.id,
                )
            )
            if not others:
                # Nobody watched it while paused: skip what was posted in the meantime.
                self.reset_baseline(sub.account)
        sub.is_paused = paused
        return True

    async def toggle_notify(self, session: AsyncSession, user: User, account_id: int) -> bool | None:
        sub = await AccountRepository(session).get_subscription(user.id, account_id)
        if sub is None:
            return None
        sub.notify_enabled = not sub.notify_enabled
        return sub.notify_enabled
