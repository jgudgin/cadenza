from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

DEFAULT_DSN = "postgresql+asyncpg://postgres:cadenza@localhost:5434/cadenza"


def make_engine(dsn: str | None = None) -> AsyncEngine:
    return create_async_engine(dsn or os.environ.get("DATABASE_URL", DEFAULT_DSN))


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
