"""This repo's own test suite dogfoods the same fixture any project built
on cadenza uses - see cadenza/testing.py for what it actually does and
why it lives there instead of being duplicated here.
"""

from __future__ import annotations

import pytest_asyncio

from cadenza.db import make_session_factory
from cadenza.testing import pg_schema_engine_fixture

DEFAULT_DSN = "postgresql+asyncpg://postgres:cadenza@localhost:5434/cadenza"

engine = pg_schema_engine_fixture(DEFAULT_DSN)


@pytest_asyncio.fixture
def session_factory(engine):
    return make_session_factory(engine)
