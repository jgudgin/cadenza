"""Reusable pytest fixture for real-Postgres tests: one throwaway schema
per test, created before the test and dropped after. This repo's own
test suite (tests/conftest.py) and every project built on cadenza used
to each carry an identical copy of this fixture, differing only in the
default DSN - exactly the kind of duplication a shared library should
absorb instead of leaving every downstream project to maintain its own
copy. Real Postgres throughout, never mocked: the whole point is
catching what only shows up against it (SKIP LOCKED, the dependency
NOT EXISTS check, the jsonb `||` merge under real concurrency).

Only ever imported from a conftest.py, so pytest-asyncio only needs to be
installed when tests actually run - the same "not a core dependency"
status pytest itself has. Not declared as a cadenza dependency for that
reason; whichever project imports this already has it, via its own dev
extra.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Callable

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .models import Base


def pg_schema_engine_fixture(default_dsn: str) -> Callable[[], AsyncIterator[AsyncEngine]]:
    """Returns a pytest-asyncio fixture yielding an AsyncEngine pointed at
    a freshly created, throwaway Postgres schema. `default_dsn` is used
    when the DATABASE_URL environment variable isn't set - each project
    passes its own (a different port and/or database name), so test runs
    against different projects never collide.

    Usage in a project's own conftest.py - the name `engine` matters,
    since that's what pytest matches test functions' `engine` parameter
    against:

        from cadenza.testing import pg_schema_engine_fixture

        engine = pg_schema_engine_fixture(
            "postgresql+asyncpg://postgres:cadenza@localhost:5435/cadenza_modeler"
        )
    """

    @pytest_asyncio.fixture
    async def engine() -> AsyncIterator[AsyncEngine]:
        base_dsn = os.environ.get("DATABASE_URL", default_dsn)
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

    return engine
