from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def get_or_create(
        self,
        telegram_id: int,
        chat_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        default_timezone: str = "UTC",
        default_poll_interval: int = 60,
    ) -> User:
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            user = User(
                telegram_id=telegram_id,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                timezone=default_timezone,
                poll_interval_seconds=default_poll_interval,
            )
            self.session.add(user)
            await self.session.flush()
        else:
            # Keep profile info fresh; chat_id may change if the user moves to another chat.
            user.username = username
            user.first_name = first_name
            user.chat_id = chat_id
        return user

    async def list_with_error_alerts(self) -> list[User]:
        return list(await self.session.scalars(select(User).where(User.error_notifications_enabled.is_(True))))
