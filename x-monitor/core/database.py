from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=10)


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Transaction scope: commit on success, rollback on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
