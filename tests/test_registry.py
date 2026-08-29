"""Direct unit coverage for the small pieces of cadenza.registry that
aren't already exercised indirectly through the orchestrator tests."""

from __future__ import annotations

import inspect

from cadenza.registry import _default_report_progress


def test_default_report_progress_is_async_callable():
    assert inspect.iscoroutinefunction(_default_report_progress)


async def test_default_report_progress_returns_none_without_raising():
    result = await _default_report_progress("anything, it's a no-op")
    assert result is None
