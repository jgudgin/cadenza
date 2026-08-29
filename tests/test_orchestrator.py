"""The engine's core guarantees, proven against real Postgres:

- a task can fan out to independent siblings and fan back in on a join
  task that depends on all of them (SKIP LOCKED concurrent dispatch, the
  NOT EXISTS dependency check)
- Retry backs off and eventually succeeds; exhausting retries fails the
  task and cascades to block anything waiting on it
- Permanent fails immediately and cascades the same way
- Drop marks a task as no longer needed (not an error) and cascades the
  same way
- a crash between "claim" and "commit" - even one from a bug in the
  planner itself, not just the agent - rolls back completely and the task
  is exactly as claimable as if nothing had happened
- the opt-in lease/heartbeat model: claiming a task releases its row lock
  immediately rather than holding it for the handler's duration, and a
  lease that expires before a worker finishes is recovered by a sweep
  instead of being stuck forever

These are synthetic agents, not the finance ones: no ANTHROPIC_API_KEY
needed, and each test isolates exactly one engine behaviour.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text

from cadenza.exceptions import Drop, Permanent, Retry
from cadenza.models import RunStatus, Task, TaskDependency, WorkflowRun
from cadenza.orchestrator import (
    claim_with_lease,
    process_one,
    process_one_with_lease,
    run_to_completion,
    start_run,
    sweep_expired_leases,
)
from cadenza.registry import PlanOutcome, Registry, TaskSpec


async def _clear_backoff(session_factory, run_id: int) -> None:  # noqa: ANN001
    async with session_factory() as session:
        await session.execute(
            text("UPDATE cadenza_tasks SET next_attempt_at = NULL WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        await session.commit()


async def test_fan_out_fan_in_completes(session_factory):
    reg = Registry()

    async def start(ctx):
        return {"started": True}

    async def plan_start(input):
        return PlanOutcome(
            reasoning="fan out to two independent legs, then join on both",
            tasks=[
                TaskSpec(type="leg", input={"name": "a"}, key="a"),
                TaskSpec(type="leg", input={"name": "b"}, key="b"),
                TaskSpec(type="join", depends_on=["a", "b"]),
            ],
        )

    reg.agent("start", plan_next=plan_start)(start)

    async def leg(ctx):
        return {"name": ctx.input["name"]}

    async def plan_leg(input):
        return PlanOutcome(context_updates={f"leg_{input.output['name']}": True})

    reg.agent("leg", plan_next=plan_leg)(leg)

    async def join(ctx):
        return {"joined": True}

    async def plan_join(input):
        return PlanOutcome(reasoning="both legs done", run_complete=True)

    reg.agent("join", plan_next=plan_join)(join)

    run_id = await start_run(session_factory, "fan out and back in", TaskSpec(type="start"))
    await run_to_completion(session_factory, reg, run_id, concurrency=3, poll_interval=0.05)

    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
        result = await session.execute(select(Task).where(Task.run_id == run_id))
        tasks = result.scalars().all()

    assert run.status == RunStatus.completed.value
    assert run.context["leg_a"] is True
    assert run.context["leg_b"] is True
    assert len(tasks) == 4
    assert all(t.status == "completed" for t in tasks)


async def test_retry_then_succeeds(session_factory):
    reg = Registry()
    calls = {"n": 0}

    async def flaky(ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Retry("simulated transient failure")
        return {"ok": True}

    async def plan_flaky(input):
        return PlanOutcome(run_complete=True)

    reg.agent("flaky", plan_next=plan_flaky, max_attempts=5)(flaky)

    run_id = await start_run(session_factory, "flaky", TaskSpec(type="flaky"))

    # Drive it directly instead of through run_to_completion so the real
    # backoff wait can be skipped between attempts, the same trick cadence's
    # own tests use - see NOTES.md: "a run doing nothing is usually
    # backoff, not breakage."
    for _ in range(3):
        assert await process_one(session_factory, reg, run_id)
        await _clear_backoff(session_factory, run_id)

    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
    assert run.status == RunStatus.completed.value
    assert calls["n"] == 3


async def test_exhausted_retries_fails_and_blocks_dependents(session_factory):
    reg = Registry()

    async def always_fails(ctx):
        raise Retry("nope")

    async def plan_unreachable(input):
        raise AssertionError("plan_next must never run for a task that never succeeds")

    reg.agent("always_fails", plan_next=plan_unreachable, max_attempts=2)(always_fails)

    async def dependent(ctx):
        return {}

    async def plan_dependent(input):
        return PlanOutcome(run_complete=True)

    reg.agent("dependent", plan_next=plan_dependent)(dependent)

    run_id = await start_run(session_factory, "will exhaust retries", TaskSpec(type="always_fails"))

    async with session_factory() as session:
        seed = (await session.execute(select(Task).where(Task.run_id == run_id))).scalar_one()
        dep = Task(run_id=run_id, type="dependent")
        session.add(dep)
        await session.flush()
        session.add(TaskDependency(task_id=dep.id, depends_on_task_id=seed.id))
        await session.commit()
        seed_id, dep_id = seed.id, dep.id

    for _ in range(2):  # max_attempts=2
        assert await process_one(session_factory, reg, run_id)
        await _clear_backoff(session_factory, run_id)

    async with session_factory() as session:
        seed_after = await session.get(Task, seed_id)
        dep_after = await session.get(Task, dep_id)

    assert seed_after.status == "failed"
    assert dep_after.status == "blocked"


async def test_permanent_failure_blocks_dependents(session_factory):
    reg = Registry()

    async def doomed(ctx):
        raise Permanent("malformed input, retrying will not help")

    async def plan_unreachable(input):
        raise AssertionError

    reg.agent("doomed", plan_next=plan_unreachable)(doomed)

    async def dependent(ctx):
        return {}

    async def plan_dependent(input):
        return PlanOutcome(run_complete=True)

    reg.agent("dependent", plan_next=plan_dependent)(dependent)

    run_id = await start_run(session_factory, "doomed", TaskSpec(type="doomed"))

    async with session_factory() as session:
        seed = (await session.execute(select(Task).where(Task.run_id == run_id))).scalar_one()
        dep = Task(run_id=run_id, type="dependent")
        session.add(dep)
        await session.flush()
        session.add(TaskDependency(task_id=dep.id, depends_on_task_id=seed.id))
        await session.commit()
        seed_id, dep_id = seed.id, dep.id

    assert await process_one(session_factory, reg, run_id)

    async with session_factory() as session:
        seed_after = await session.get(Task, seed_id)
        dep_after = await session.get(Task, dep_id)

    assert seed_after.status == "failed"
    assert dep_after.status == "blocked"


async def test_dropped_task_blocks_dependents(session_factory):
    """Drop is not an error - the planner decided the task is no longer
    needed - but anything depending on it still can never become ready, so
    it must cascade to 'blocked' exactly like a real failure does."""
    reg = Registry()

    async def superseded(ctx):
        raise Drop("no longer needed, superseded by a replan")

    async def plan_unreachable(input):
        raise AssertionError("plan_next must never run for a dropped task")

    reg.agent("superseded", plan_next=plan_unreachable)(superseded)

    async def dependent(ctx):
        return {}

    async def plan_dependent(input):
        return PlanOutcome(run_complete=True)

    reg.agent("dependent", plan_next=plan_dependent)(dependent)

    run_id = await start_run(session_factory, "superseded", TaskSpec(type="superseded"))

    async with session_factory() as session:
        seed = (await session.execute(select(Task).where(Task.run_id == run_id))).scalar_one()
        dep = Task(run_id=run_id, type="dependent")
        session.add(dep)
        await session.flush()
        session.add(TaskDependency(task_id=dep.id, depends_on_task_id=seed.id))
        await session.commit()
        seed_id, dep_id = seed.id, dep.id

    assert await process_one(session_factory, reg, run_id)

    async with session_factory() as session:
        seed_after = await session.get(Task, seed_id)
        dep_after = await session.get(Task, dep_id)

    assert seed_after.status == "dropped"
    assert dep_after.status == "blocked"


async def test_crash_between_claim_and_commit_rolls_back_and_is_resumable(session_factory):
    """The central claim of this whole design: a task, one transaction.
    A bug in the planner itself - not a handled Retry/Permanent/Drop, a
    genuine unhandled exception - must not leave the task half-claimed.
    """
    reg = Registry()
    calls = {"n": 0}

    async def unstable(ctx):
        return {"value": 42}

    async def buggy_plan(input):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated bug - not Retry/Permanent/Drop")
        return PlanOutcome(reasoning="fixed on the next attempt", run_complete=True)

    reg.agent("unstable", plan_next=buggy_plan)(unstable)

    run_id = await start_run(session_factory, "will crash once", TaskSpec(type="unstable"))

    with pytest.raises(RuntimeError):
        await process_one(session_factory, reg, run_id)

    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.run_id == run_id))).scalar_one()
    # Everything the crashed transaction did - including the claim's own
    # status flip and attempts increment - rolled back. Nothing was
    # half-applied; a fresh worker sees exactly what it would have seen if
    # this had never been attempted.
    assert task.status == "pending"
    assert task.output is None
    assert task.attempts == 0

    assert await process_one(session_factory, reg, run_id)

    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
    assert run.status == RunStatus.completed.value
    assert calls["n"] == 2


async def test_lease_claim_releases_row_lock_before_handler_finishes(session_factory):
    """The whole point of the lease model: unlike process_one, the claim
    transaction commits (and releases its row lock) before the handler is
    even called, instead of holding the lock for however long the handler
    takes. Proven by grabbing the same row with FOR UPDATE NOWAIT - which
    raises immediately instead of blocking - while the handler is still
    sitting there, deliberately paused."""
    reg = Registry()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def slow_handler(ctx):
        handler_started.set()
        await release_handler.wait()
        return {"ok": True}

    async def plan_next(input):
        return PlanOutcome(run_complete=True)

    reg.agent("slow", plan_next=plan_next)(slow_handler)

    run_id = await start_run(session_factory, "slow lease task", TaskSpec(type="slow"))

    task_future = asyncio.ensure_future(process_one_with_lease(session_factory, reg, run_id))
    try:
        await asyncio.wait_for(handler_started.wait(), timeout=5)

        # The handler is now blocked mid-flight, on purpose. If the claim
        # were still holding its transaction open (as process_one's would
        # be), this FOR UPDATE NOWAIT would raise immediately for the
        # opposite reason - a lock held by someone else. Instead the claim
        # already committed, so a brand-new session can lock the row with
        # no contention at all.
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id, status FROM cadenza_tasks WHERE run_id = :run_id FOR UPDATE NOWAIT"),
                {"run_id": run_id},
            )
            row = result.first()
            assert row is not None
            assert row.status == "running"
            await session.commit()
    finally:
        release_handler.set()

    assert await asyncio.wait_for(task_future, timeout=5)

    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
        task = (await session.execute(select(Task).where(Task.run_id == run_id))).scalar_one()
    assert run.status == RunStatus.completed.value
    assert task.status == "completed"
    assert task.lease_expires_at is None


async def test_report_progress_is_visible_before_the_task_settles(session_factory):
    """ctx.report_progress must commit independently of the task's own
    held-open transaction (process_one holds one for the handler's entire
    duration) - proven by pausing the handler mid-flight, calling
    report_progress, and reading the event back from a separate session
    while the claim itself is still uncommitted."""
    reg = Registry()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def slow_handler(ctx):
        handler_started.set()
        await ctx.report_progress("halfway there")
        await release_handler.wait()
        return {"ok": True}

    async def plan_next(input):
        return PlanOutcome(run_complete=True)

    reg.agent("slow", plan_next=plan_next)(slow_handler)

    run_id = await start_run(session_factory, "slow task with progress", TaskSpec(type="slow"))

    task_future = asyncio.ensure_future(process_one(session_factory, reg, run_id))
    try:
        await asyncio.wait_for(handler_started.wait(), timeout=5)

        # report_progress is a separate commit, not synchronized with
        # handler_started - poll briefly rather than assuming it has
        # already landed by the time we get here.
        row = None
        for _ in range(50):
            async with session_factory() as session:
                result = await session.execute(
                    text("SELECT payload FROM cadenza_events WHERE run_id = :run_id AND type = 'task_progress'"),
                    {"run_id": run_id},
                )
                row = result.first()
            if row is not None:
                break
            await asyncio.sleep(0.05)
        assert row is not None
        assert row.payload["message"] == "halfway there"

        # The claim's own UPDATE is still sitting inside process_one's open
        # transaction, uncommitted - a separate session reading under READ
        # COMMITTED still sees the pre-claim 'pending' state. The progress
        # event landed anyway: exactly the point of routing it through its
        # own short transaction instead of the handler's.
        async with session_factory() as session:
            task = (await session.execute(select(Task).where(Task.run_id == run_id))).scalar_one()
        assert task.status == "pending"
    finally:
        release_handler.set()

    assert await asyncio.wait_for(task_future, timeout=5)

    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
    assert run.status == RunStatus.completed.value


async def test_sweep_expired_leases_recovers_stuck_task(session_factory):
    """A worker claims a task with a lease and then, say, crashes: nothing
    ever runs a commit transaction for it. sweep_expired_leases must
    notice the lease is in the past and put the task back to 'pending'
    (attempts unchanged) so it's claimable again - by either model."""
    reg = Registry()

    async def handler(ctx):
        return {"ok": True}

    async def plan_next(input):
        return PlanOutcome(run_complete=True)

    reg.agent("leased", plan_next=plan_next)(handler)

    run_id = await start_run(session_factory, "will crash after leasing", TaskSpec(type="leased"))

    # Claim with a lease that's already expired - simulating a worker that
    # claimed the task and then vanished before ever reaching the commit
    # step.
    task_id = await claim_with_lease(session_factory, run_id, lease_seconds=-1)
    assert task_id is not None

    async with session_factory() as session:
        task = await session.get(Task, task_id)
    assert task.status == "running"
    assert task.lease_expires_at is not None
    assert task.attempts == 1

    # Sweeping too early (nothing expired yet) must be a no-op - only
    # matters here because we've asserted the lease already is expired
    # above; a second, unexpired task should be left alone.
    other_task_id = await claim_with_lease(session_factory, run_id, lease_seconds=3600)
    assert other_task_id is None  # nothing else pending yet, just documenting the call is safe

    recovered = await sweep_expired_leases(session_factory, run_id)
    assert recovered == [task_id]

    async with session_factory() as session:
        task_after = await session.get(Task, task_id)
    assert task_after.status == "pending"
    assert task_after.lease_expires_at is None
    assert task_after.attempts == 1  # sweep resets status, not the attempt counter

    # A crashed worker's task becomes claimable again instead of stuck
    # forever - by the lease path here, but it would look identical to
    # process_one too, since it's just a plain 'pending' row now.
    reclaimed_id = await claim_with_lease(session_factory, run_id)
    assert reclaimed_id == task_id

    async with session_factory() as session:
        task_reclaimed = await session.get(Task, task_id)
    assert task_reclaimed.status == "running"
    assert task_reclaimed.attempts == 2
