# TODO comment needed: what this file proves about planning failures and how it relates to test_orchestrator.py

from __future__ import annotations

from sqlalchemy import select, text

from cadenza.exceptions import Drop, Permanent
from cadenza.models import Event, RunStatus, Task, TaskDependency, WorkflowRun
from cadenza.orchestrator import process_one, process_one_with_lease, start_run
from cadenza.registry import PlanOutcome, Registry, TaskSpec


async def _clear_backoff(session_factory, run_id: int) -> None:  # noqa: ANN001
    async with session_factory() as session:
        await session.execute(
            text("UPDATE cadenza_tasks SET next_attempt_at = NULL WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        await session.commit()


async def _tasks(session_factory, run_id: int) -> list[Task]:  # noqa: ANN001
    async with session_factory() as session:
        result = await session.execute(select(Task).where(Task.run_id == run_id).order_by(Task.id))
        return list(result.scalars().all())


async def _events(session_factory, run_id: int, event_type: str) -> list[Event]:  # noqa: ANN001
    async with session_factory() as session:
        result = await session.execute(
            select(Event).where(Event.run_id == run_id, Event.type == event_type).order_by(Event.id)
        )
        return list(result.scalars().all())


async def _run_status(session_factory, run_id: int) -> str:  # noqa: ANN001
    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
        return run.status


# TODO comment needed: why the dependent task is inserted directly instead of through a planner
async def _add_dependent(session_factory, run_id: int, depends_on_task_id: int) -> int:  # noqa: ANN001
    async with session_factory() as session:
        dependent = Task(run_id=run_id, type="dependent")
        session.add(dependent)
        await session.flush()
        session.add(TaskDependency(task_id=dependent.id, depends_on_task_id=depends_on_task_id))
        await session.commit()
        return dependent.id


async def test_planner_failure_counts_as_an_attempt_and_retries_with_backoff(session_factory):
    reg = Registry()
    calls = {"handler": 0, "planner": 0}

    async def unstable(ctx):
        calls["handler"] += 1
        return {"value": 42}

    async def plan_unstable(input):
        calls["planner"] += 1
        if calls["planner"] == 1:
            raise RuntimeError("simulated planner bug")
        return PlanOutcome(run_complete=True)

    reg.agent("unstable", plan_next=plan_unstable)(unstable)
    run_id = await start_run(session_factory, "planner fails once", TaskSpec(type="unstable"))

    assert await process_one(session_factory, reg, run_id)

    tasks = await _tasks(session_factory, run_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == "pending"
    assert task.attempts == 1
    assert task.output is None
    assert task.next_attempt_at is not None
    assert "simulated planner bug" in task.last_error

    retries = await _events(session_factory, run_id, "task_retry")
    assert len(retries) == 1
    assert retries[0].payload["phase"] == "plan"
    assert retries[0].payload["discarded_output"] == {"value": 42}
    # TODO comment needed: why no task_completed event may survive a failed planning step
    assert await _events(session_factory, run_id, "task_completed") == []

    await _clear_backoff(session_factory, run_id)
    assert await process_one(session_factory, reg, run_id)

    tasks = await _tasks(session_factory, run_id)
    assert await _run_status(session_factory, run_id) == RunStatus.completed.value
    assert tasks[0].status == "completed"
    assert tasks[0].attempts == 2
    assert calls["handler"] == 2


async def test_planner_that_always_fails_exhausts_max_attempts_and_blocks_dependents(session_factory):
    reg = Registry()
    calls = {"handler": 0}

    async def handler(ctx):
        calls["handler"] += 1
        return {"value": 1}

    async def always_broken(input):
        raise RuntimeError("planner bug on every attempt")

    reg.agent("broken_plan", plan_next=always_broken, max_attempts=3)(handler)
    run_id = await start_run(session_factory, "planner always fails", TaskSpec(type="broken_plan"))
    seed_tasks = await _tasks(session_factory, run_id)
    seed_id = seed_tasks[0].id
    dependent_id = await _add_dependent(session_factory, run_id, seed_id)

    for _ in range(3):
        assert await process_one(session_factory, reg, run_id)
        await _clear_backoff(session_factory, run_id)

    assert calls["handler"] == 3

    tasks_by_id = {}
    for t in await _tasks(session_factory, run_id):
        tasks_by_id[t.id] = t
    assert tasks_by_id[seed_id].status == "failed"
    assert tasks_by_id[seed_id].attempts == 3
    assert tasks_by_id[dependent_id].status == "blocked"

    failures = await _events(session_factory, run_id, "task_failed")
    assert len(failures) == 1
    assert failures[0].payload["phase"] == "plan"
    assert failures[0].payload["discarded_output"] == {"value": 1}

    # TODO comment needed: why nothing is claimable once the seed has failed
    assert not await process_one(session_factory, reg, run_id)


async def test_planner_raising_permanent_fails_the_task_without_retrying(session_factory):
    reg = Registry()
    calls = {"handler": 0}

    async def handler(ctx):
        calls["handler"] += 1
        return {"draft": "malformed"}

    async def rejects_output(input):
        raise Permanent("this output can never be planned from")

    reg.agent("rejected", plan_next=rejects_output)(handler)
    run_id = await start_run(session_factory, "planner raises Permanent", TaskSpec(type="rejected"))
    seed_tasks = await _tasks(session_factory, run_id)
    seed_id = seed_tasks[0].id
    dependent_id = await _add_dependent(session_factory, run_id, seed_id)

    assert await process_one(session_factory, reg, run_id)

    tasks_by_id = {}
    for t in await _tasks(session_factory, run_id):
        tasks_by_id[t.id] = t
    assert tasks_by_id[seed_id].status == "failed"
    assert tasks_by_id[seed_id].attempts == 1
    assert tasks_by_id[dependent_id].status == "blocked"
    assert calls["handler"] == 1

    failures = await _events(session_factory, run_id, "task_failed")
    assert len(failures) == 1
    assert failures[0].payload["phase"] == "plan"
    assert failures[0].payload["discarded_output"] == {"draft": "malformed"}
    assert await _events(session_factory, run_id, "task_retry") == []


async def test_planner_raising_drop_drops_the_task_and_blocks_dependents(session_factory):
    reg = Registry()

    async def handler(ctx):
        return {"superseded": True}

    async def drops_task(input):
        raise Drop("a replan made this task unnecessary")

    reg.agent("droppable", plan_next=drops_task)(handler)
    run_id = await start_run(session_factory, "planner raises Drop", TaskSpec(type="droppable"))
    seed_tasks = await _tasks(session_factory, run_id)
    seed_id = seed_tasks[0].id
    dependent_id = await _add_dependent(session_factory, run_id, seed_id)

    assert await process_one(session_factory, reg, run_id)

    tasks_by_id = {}
    for t in await _tasks(session_factory, run_id):
        tasks_by_id[t.id] = t
    assert tasks_by_id[seed_id].status == "dropped"
    assert tasks_by_id[dependent_id].status == "blocked"

    drops = await _events(session_factory, run_id, "task_dropped")
    assert len(drops) == 1
    assert drops[0].payload["phase"] == "plan"
    assert drops[0].payload["discarded_output"] == {"superseded": True}


# TODO comment needed: why an unregistered task type is a permanent failure and not a retry
async def test_unregistered_task_type_fails_permanently_and_blocks_dependents(session_factory):
    reg = Registry()

    async def seed(ctx):
        return {}

    async def plan_seed(input):
        return PlanOutcome(
            tasks=[
                TaskSpec(type="not_registered", key="unknown"),
                TaskSpec(type="after_unknown", depends_on=["unknown"]),
            ]
        )

    reg.agent("seed", plan_next=plan_seed)(seed)

    async def after_unknown(ctx):
        return {}

    async def plan_after_unknown(input):
        return PlanOutcome(run_complete=True)

    reg.agent("after_unknown", plan_next=plan_after_unknown)(after_unknown)

    run_id = await start_run(session_factory, "planner names an unregistered type", TaskSpec(type="seed"))
    assert await process_one(session_factory, reg, run_id)
    assert await process_one(session_factory, reg, run_id)

    tasks_by_type = {}
    for t in await _tasks(session_factory, run_id):
        tasks_by_type[t.type] = t
    assert tasks_by_type["not_registered"].status == "failed"
    assert "not_registered" in tasks_by_type["not_registered"].last_error
    assert tasks_by_type["after_unknown"].status == "blocked"
    assert await _events(session_factory, run_id, "task_retry") == []
    assert not await process_one(session_factory, reg, run_id)


# TODO comment needed: why no part of a plan may persist when applying it fails partway through
async def test_bad_dependency_key_retries_without_applying_any_of_the_plan(session_factory):
    reg = Registry()

    async def seed(ctx):
        return {"ok": True}

    async def plan_with_unknown_key(input):
        return PlanOutcome(
            context_updates={"should_not_persist": True},
            tasks=[
                TaskSpec(type="child", key="child"),
                TaskSpec(type="orphan", depends_on=["no_such_key"]),
            ],
        )

    reg.agent("seed", plan_next=plan_with_unknown_key)(seed)
    run_id = await start_run(session_factory, "plan names a missing key", TaskSpec(type="seed"))

    assert await process_one(session_factory, reg, run_id)

    tasks = await _tasks(session_factory, run_id)
    assert len(tasks) == 1
    assert tasks[0].status == "pending"
    assert tasks[0].attempts == 1

    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
    assert run.context == {}
    assert await _events(session_factory, run_id, "task_created") == []
    assert await _events(session_factory, run_id, "plan_decision") == []

    retries = await _events(session_factory, run_id, "task_retry")
    assert len(retries) == 1
    assert retries[0].payload["phase"] == "plan"
    assert retries[0].payload["discarded_output"] == {"ok": True}


# TODO comment needed: why the lease must be cleared when a planning failure sends the task back to pending
async def test_lease_path_planner_failure_retries_and_clears_the_lease(session_factory):
    reg = Registry()
    calls = {"planner": 0}

    async def handler(ctx):
        return {"value": 7}

    async def plan_fails_once(input):
        calls["planner"] += 1
        if calls["planner"] == 1:
            raise RuntimeError("simulated planner bug")
        return PlanOutcome(run_complete=True)

    reg.agent("leased", plan_next=plan_fails_once)(handler)
    run_id = await start_run(session_factory, "lease path planner fails once", TaskSpec(type="leased"))

    assert await process_one_with_lease(session_factory, reg, run_id)

    tasks = await _tasks(session_factory, run_id)
    task = tasks[0]
    assert task.status == "pending"
    assert task.attempts == 1
    assert task.lease_expires_at is None
    assert task.next_attempt_at is not None

    retries = await _events(session_factory, run_id, "task_retry")
    assert len(retries) == 1
    assert retries[0].payload["phase"] == "plan"
    assert retries[0].payload["discarded_output"] == {"value": 7}

    await _clear_backoff(session_factory, run_id)
    assert await process_one_with_lease(session_factory, reg, run_id)

    tasks = await _tasks(session_factory, run_id)
    assert await _run_status(session_factory, run_id) == RunStatus.completed.value
    assert tasks[0].status == "completed"
    assert tasks[0].lease_expires_at is None
