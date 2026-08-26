"""The engine's core guarantees, proven against real Postgres:

- a task can fan out to independent siblings and fan back in on a join
  task that depends on all of them (SKIP LOCKED concurrent dispatch, the
  NOT EXISTS dependency check)
- Retry backs off and eventually succeeds; exhausting retries fails the
  task and cascades to block anything waiting on it
- Permanent fails immediately and cascades the same way
- a crash between "claim" and "commit" - even one from a bug in the
  planner itself, not just the agent - rolls back completely and the task
  is exactly as claimable as if nothing had happened

These are synthetic agents, not the finance ones: no ANTHROPIC_API_KEY
needed, and each test isolates exactly one engine behaviour.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from cadenza.exceptions import Permanent, Retry
from cadenza.models import RunStatus, Task, TaskDependency, WorkflowRun
from cadenza.orchestrator import process_one, run_to_completion, start_run
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
