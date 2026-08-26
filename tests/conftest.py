"""One throwaway Postgres schema per test - same approach as cadence's
tests/conftest.py, ported to an async engine. Real Postgres throughout:
the whole point of these tests is to catch what only shows up against it
(SKIP LOCKED, the NOT EXISTS dependency check, the jsonb `||` merge under
real concurrency), not to mock any of that away.
"""

from __future__ import annotations

import os
import uuid

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from cadenza.db import make_session_factory
from cadenza.models import Base

DEFAULT_DSN = "postgresql+asyncpg://postgres:cadenza@localhost:5434/cadenza"


@pytest_asyncio.fixture
async def engine():
    base_dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    schema = f"test_{uuid.uuid4().hex[:16]}"

    admin = create_async_engine(base_dsn)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await admin.dispose()

    eng = create_async_engine(base_dsn, connect_args={"server_settings": {"search_path": schema}})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield eng
    finally:
        await eng.dispose()
        admin = create_async_engine(base_dsn)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
def session_factory(engine):
    return make_session_factory(engine)
