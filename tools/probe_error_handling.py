from __future__ import annotations

import asyncio
import os
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from cadenza.db import make_session_factory
from cadenza.exceptions import Permanent, Retry
from cadenza.models import Base, Task, WorkflowRun
from cadenza.orchestrator import (
    process_one,
    process_one_with_lease,
    run_to_completion,
    start_run,
    sweep_expired_leases,
)
from cadenza.registry import PlanOutcome, Registry, TaskSpec

DSN = os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres:cadenza@localhost:5434/cadenza")


async def make_schema():
    schema = "probe_" + uuid.uuid4().hex[:12]
    admin = create_async_engine(DSN)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await admin.dispose()

    engine = create_async_engine(DSN, connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return schema, engine


async def drop_schema(schema, engine):
    await engine.dispose()
    admin = create_async_engine(DSN)
    async with admin.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await admin.dispose()


async def task_rows(session_factory, run_id):
    async with session_factory() as session:
        result = await session.execute(select(Task).where(Task.run_id == run_id).order_by(Task.id))
        return result.scalars().all()


async def run_status(session_factory, run_id):
    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
        return run.status


def print_tasks(tasks):
    for t in tasks:
        print(f"  task {t.id} type={t.type} status={t.status} attempts={t.attempts} last_error={t.last_error!r}")


async def probe_concurrency_zero(session_factory):
    reg = Registry()

    async def seed(ctx):
        return {}

    async def plan_seed(planner_input):
        return PlanOutcome(run_complete=True)

    reg.agent("seed", plan_next=plan_seed)(seed)

    run_id = await start_run(session_factory, "concurrency zero", TaskSpec(type="seed"))
    await run_to_completion(session_factory, reg, run_id, concurrency=0)

    print("run status:", await run_status(session_factory, run_id))
    print_tasks(await task_rows(session_factory, run_id))


async def probe_planner_always_crashes(session_factory):
    reg = Registry()
    calls = {"handler": 0}

    async def seed(ctx):
        calls["handler"] += 1
        return {}

    async def plan_seed(planner_input):
        raise RuntimeError("planner bug")

    reg.agent("seed", plan_next=plan_seed, max_attempts=3)(seed)

    run_id = await start_run(session_factory, "planner always crashes", TaskSpec(type="seed"))
    for _ in range(5):
        try:
            await process_one(session_factory, reg, run_id)
        except RuntimeError:
            pass

    print("process_one called 5 times, max_attempts=3")
    print("handler calls:", calls["handler"])
    print("run status:", await run_status(session_factory, run_id))
    print_tasks(await task_rows(session_factory, run_id))


async def probe_unknown_task_type(session_factory):
    reg = Registry()

    async def seed(ctx):
        return {}

    async def plan_seed(planner_input):
        return PlanOutcome(tasks=[TaskSpec(type="not_registered")])

    reg.agent("seed", plan_next=plan_seed)(seed)

    run_id = await start_run(session_factory, "unknown task type", TaskSpec(type="seed"))
    await process_one(session_factory, reg, run_id)

    key_errors = 0
    for _ in range(3):
        try:
            await process_one(session_factory, reg, run_id)
        except KeyError:
            key_errors += 1

    print("KeyError raised on", key_errors, "of 3 further process_one calls")
    print("run status:", await run_status(session_factory, run_id))
    print_tasks(await task_rows(session_factory, run_id))


async def probe_dependency_on_failed_task(session_factory):
    reg = Registry()

    async def seed(ctx):
        return {}

    async def plan_seed(planner_input):
        return PlanOutcome(tasks=[TaskSpec(type="fails"), TaskSpec(type="later")])

    reg.agent("seed", plan_next=plan_seed)(seed)

    async def fails(ctx):
        raise Permanent("always fails")

    async def plan_fails(planner_input):
        return PlanOutcome()

    reg.agent("fails", plan_next=plan_fails)(fails)

    async def later(ctx):
        async with session_factory() as session:
            result = await session.execute(select(Task).where(Task.run_id == ctx.run_id, Task.type == "fails"))
            fails_task = result.scalar_one()
        # TODO comment needed: why this handler retries until the "fails" task has settled
        if fails_task.status != "failed":
            raise Retry("fails task has not failed yet")
        return {"fails_id": fails_task.id}

    async def plan_later(planner_input):
        return PlanOutcome(tasks=[TaskSpec(type="orphan", depends_on=[planner_input.output["fails_id"]])])

    reg.agent("later", plan_next=plan_later, max_attempts=5)(later)

    async def orphan(ctx):
        return {}

    async def plan_orphan(planner_input):
        return PlanOutcome(run_complete=True)

    reg.agent("orphan", plan_next=plan_orphan)(orphan)

    run_id = await start_run(session_factory, "dependency on a failed task", TaskSpec(type="seed"))
    try:
        await asyncio.wait_for(
            run_to_completion(session_factory, reg, run_id, concurrency=1, poll_interval=0.1),
            timeout=10,
        )
        print("run_to_completion returned")
    except TimeoutError:
        print("run_to_completion had not returned after 10s")

    print("run status:", await run_status(session_factory, run_id))
    print_tasks(await task_rows(session_factory, run_id))


async def probe_lease_late_settle_after_sweep(session_factory):
    reg = Registry()
    calls = {"handler": 0}
    first_call_started = asyncio.Event()
    release_first_call = asyncio.Event()

    async def slow(ctx):
        calls["handler"] += 1
        if calls["handler"] == 1:
            first_call_started.set()
            await release_first_call.wait()
        return {}

    async def plan_slow(planner_input):
        return PlanOutcome(tasks=[TaskSpec(type="child")])

    reg.agent("slow", plan_next=plan_slow)(slow)

    async def child(ctx):
        return {}

    async def plan_child(planner_input):
        return PlanOutcome()

    reg.agent("child", plan_next=plan_child)(child)

    run_id = await start_run(session_factory, "late settle after sweep", TaskSpec(type="slow"))

    first_worker = asyncio.ensure_future(
        process_one_with_lease(session_factory, reg, run_id, lease_seconds=0.5)
    )
    try:
        await asyncio.wait_for(first_call_started.wait(), timeout=5)
        # TODO comment needed: why the probe sleeps past the 0.5s lease before sweeping
        await asyncio.sleep(1.0)
        swept = await sweep_expired_leases(session_factory, run_id)
        print("sweep reset task ids:", swept)

        second_result = await process_one_with_lease(session_factory, reg, run_id, lease_seconds=0.5)
        print("second worker claimed and settled a task:", second_result)
    finally:
        release_first_call.set()

    first_result = await asyncio.wait_for(first_worker, timeout=5)
    print("first worker settled a task:", first_result)

    tasks = await task_rows(session_factory, run_id)
    child_count = 0
    for t in tasks:
        if t.type == "child":
            child_count += 1
    print("handler calls for the single slow task:", calls["handler"])
    print("child tasks created by its planner:", child_count)
    print_tasks(tasks)


async def probe_run_updated_at_on_context_update(session_factory):
    reg = Registry()

    async def seed(ctx):
        return {}

    async def plan_seed(planner_input):
        return PlanOutcome(tasks=[TaskSpec(type="next")], context_updates={"progress": 1})

    reg.agent("seed", plan_next=plan_seed)(seed)

    run_id = await start_run(session_factory, "context update only", TaskSpec(type="seed"))
    async with session_factory() as session:
        before = (await session.get(WorkflowRun, run_id)).updated_at

    await asyncio.sleep(0.05)
    await process_one(session_factory, reg, run_id)

    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
    print("run context after plan:", run.context)
    print("run.updated_at before:", before)
    print("run.updated_at after: ", run.updated_at)


async def probe_escalation_after_completion(session_factory):
    reg = Registry()

    async def seed(ctx):
        return {}

    async def plan_seed(planner_input):
        return PlanOutcome(tasks=[TaskSpec(type="finisher"), TaskSpec(type="escalator")])

    reg.agent("seed", plan_next=plan_seed)(seed)

    async def finisher(ctx):
        return {}

    async def plan_finisher(planner_input):
        return PlanOutcome(run_complete=True, reasoning="finisher says done")

    reg.agent("finisher", plan_next=plan_finisher)(finisher)

    async def escalator(ctx):
        return {}

    async def plan_escalator(planner_input):
        return PlanOutcome(escalate=True, reasoning="escalator says a human should look")

    reg.agent("escalator", plan_next=plan_escalator)(escalator)

    run_id = await start_run(session_factory, "complete then escalate", TaskSpec(type="seed"))
    for step in range(1, 4):
        claimed = await process_one(session_factory, reg, run_id)
        tasks = await task_rows(session_factory, run_id)
        completed_types = []
        for t in tasks:
            if t.status == "completed":
                completed_types.append(t.type)
        status = await run_status(session_factory, run_id)
        print(f"step {step}: claimed={claimed} run status={status} completed tasks={completed_types}")


PROBES = [
    ("run_to_completion with concurrency=0", probe_concurrency_zero),
    ("planner raises on every attempt", probe_planner_always_crashes),
    ("planner creates an unregistered task type", probe_unknown_task_type),
    ("planner creates a task depending on an already-failed task", probe_dependency_on_failed_task),
    ("lease expires while the handler is still running", probe_lease_late_settle_after_sweep),
    ("context-only plan and run.updated_at", probe_run_updated_at_on_context_update),
    ("one sibling completes the run, another escalates", probe_escalation_after_completion),
]


async def main():
    for name, probe in PROBES:
        print()
        print("=== " + name + " ===")
        schema, engine = await make_schema()
        try:
            await probe(make_session_factory(engine))
        finally:
            await drop_schema(schema, engine)


if __name__ == "__main__":
    asyncio.run(main())
