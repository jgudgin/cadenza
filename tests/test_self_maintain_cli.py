"""print_subtask_outcomes reads real Task rows and prints a summary - the
DB read itself is a plain `select`, already exercised for real elsewhere
(test_orchestrator.py), so what's worth testing here in isolation is the
formatting logic: given some subtasks, does it report the right thing for
each. A fake session/session_factory stands in for Postgres so these stay
fast, deterministic unit tests; `console.print` is swapped for a recorder
instead of relying on capturing rich's real stdout writes.
"""

from __future__ import annotations

import pytest

from cadenza.agents.self_maintain import cli
from cadenza.models import Task


class _FakeScalars:
    def __init__(self, tasks):
        self._tasks = tasks

    def all(self):
        return self._tasks


class _FakeResult:
    def __init__(self, tasks):
        self._tasks = tasks

    def scalars(self):
        return _FakeScalars(self._tasks)


class _FakeSession:
    def __init__(self, tasks):
        self._tasks = tasks

    async def execute(self, query):  # noqa: ANN001 - query is unused, tasks are fixed per test
        return _FakeResult(self._tasks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session_factory(tasks):
    def factory():
        return _FakeSession(tasks)

    return factory


def _task(**kw):
    defaults = dict(id=1, run_id=1, type="self_maintain", status="completed", output=None, last_error=None)
    defaults.update(kw)
    return Task(**defaults)


class _Recorder:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(cli, "console", rec)
    return rec


async def test_no_subtasks_reports_that_decomposition_may_have_escalated(recorder):
    await cli.print_subtask_outcomes(_session_factory([]), run_id=1)

    assert len(recorder.lines) == 1
    assert "No subtasks were created" in recorder.lines[0]


async def test_mixed_success_and_failure_outcomes_are_reported_per_task(recorder):
    tasks = [
        _task(id=1, status="completed", output={"pr_url": "https://github.com/x/y/pull/1"}),
        _task(id=2, status="failed", last_error="boom, something went wrong in the coding loop"),
        _task(id=3, status="needs_review", output={"reason": "ambiguous requirements"}),
    ]

    await cli.print_subtask_outcomes(_session_factory(tasks), run_id=1)

    header, *rows = recorder.lines
    assert "3 subtask(s)" in header
    assert "done" in rows[0] and "https://github.com/x/y/pull/1" in rows[0] and "task 1" in rows[0]
    assert "failed" in rows[1] and "boom, something went wrong" in rows[1] and "task 2" in rows[1]
    assert "needs_review" in rows[2] and "ambiguous requirements" in rows[2] and "task 3" in rows[2]


async def test_all_subtasks_succeed(recorder):
    tasks = [
        _task(id=1, status="completed", output={"pr_url": "https://example.com/pr/1"}),
        _task(id=2, status="completed", output={"pr_url": "https://example.com/pr/2"}),
    ]

    await cli.print_subtask_outcomes(_session_factory(tasks), run_id=1)

    header, *rows = recorder.lines
    assert "2 subtask(s)" in header
    assert all("done" in row for row in rows)
    assert "https://example.com/pr/1" in rows[0]
    assert "https://example.com/pr/2" in rows[1]


async def test_all_subtasks_fail(recorder):
    tasks = [
        _task(id=1, status="failed", last_error="ran out of retries"),
        _task(id=2, status="failed", last_error=None),
    ]

    await cli.print_subtask_outcomes(_session_factory(tasks), run_id=1)

    header, *rows = recorder.lines
    assert "2 subtask(s)" in header
    assert all("failed" in row for row in rows)
    assert "ran out of retries" in rows[0]
    # a missing last_error must not blow up formatting - just prints an empty reason
    assert "task 2" in rows[1]


async def test_long_error_messages_are_truncated(recorder):
    long_error = "x" * 500
    tasks = [_task(id=1, status="failed", last_error=long_error)]

    await cli.print_subtask_outcomes(_session_factory(tasks), run_id=1)

    _, row = recorder.lines
    assert long_error not in row  # too long to appear in full
    assert "x" * 100 in row


async def test_status_without_pr_url_or_reason_falls_back_to_the_raw_status(recorder):
    """No pr_url (not done), not 'failed', and no reason/summary in
    output - e.g. some other pending/blocked status - still needs to print
    something intelligible rather than raising or emitting a blank line."""
    tasks = [_task(id=1, status="blocked", output=None)]

    await cli.print_subtask_outcomes(_session_factory(tasks), run_id=1)

    _, row = recorder.lines
    assert "blocked" in row
    assert "task 1" in row
