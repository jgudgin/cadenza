from __future__ import annotations

import asyncio

import asyncpg
import sqlalchemy
from sqlalchemy import inspect, select, text

from cadenza.db import make_session_factory
from cadenza.models import Event, Task, WorkflowRun
from cadenza.orchestrator import _apply_plan, _claim, process_one, start_run
from cadenza.registry import PlanOutcome, Registry, TaskSpec
from probe_error_handling import drop_schema, make_schema, print_tasks


def describe_state(label, obj):
    state = inspect(obj)
    print(
        f"  {label}: expired_attributes={sorted(state.expired_attributes)} "
        f"persistent={state.persistent} detached={state.detached}"
    )


def try_read(label, obj, attribute):
    try:
        value = getattr(obj, attribute)
        print(f"  reading {label}.{attribute} without refresh returned: {value!r}")
    except Exception as exc:
        print(f"  reading {label}.{attribute} without refresh raised: {type(exc).__name__}")


async def check_row_lock_held(session_factory, task_id):
    async with session_factory() as other:
        try:
            await other.execute(
                text("SELECT id FROM cadenza_tasks WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": task_id},
            )
            print("  another session locked the task row: the claim's lock is NOT held")
        except Exception as exc:
            original = getattr(exc, "orig", None)
            print(
                f"  another session could not lock the task row: {type(exc).__name__} "
                f"(driver error: {type(original).__name__})"
            )
        await other.rollback()


async def db_snapshot(session_factory, run_id):
    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
        task_result = await session.execute(select(Task).where(Task.run_id == run_id).order_by(Task.id))
        tasks = task_result.scalars().all()
        event_result = await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.id))
        events = event_result.scalars().all()
    print("  final run status:", run.status, "context:", run.context)
    print_tasks(tasks)
    for e in events:
        print(f"  event {e.id} type={e.type} task_id={e.task_id} payload={e.payload}")


async def settle_with_savepoint(session_factory, run_id, plan, flush_inside):
    async with session_factory() as session:
        task = await _claim(session, run_id)
        # TODO comment needed: why the id is captured before the savepoint opens
        task_id = task.id
        run = await session.get(WorkflowRun, run_id)
        output = {"handler": "ran"}
        failure = None

        try:
            async with session.begin_nested():
                task.status = "completed"
                task.output = output
                session.add(
                    Event(run_id=run_id, task_id=task_id, type="task_completed", payload={"output": output})
                )
                outcome = plan()
                await _apply_plan(session, run, task, outcome)
                # TODO comment needed: why the probe tries an explicit flush inside the savepoint
                if flush_inside:
                    await session.flush()
        except Exception as exc:
            failure = exc

        if failure is None:
            print("  exception at savepoint: none")
        else:
            print(f"  exception at savepoint: {type(failure).__name__}: {str(failure).splitlines()[0]}")

        describe_state("task", task)
        describe_state("run", run)
        pending_types = []
        for obj in session.new:
            pending_types.append(type(obj).__name__)
        print("  objects still pending in the session:", pending_types)
        print("  session.in_transaction():", session.in_transaction(), "in_nested_transaction():", session.in_nested_transaction())

        try_read("task", task, "attempts")
        try_read("run", run, "id")

        try:
            await session.refresh(task)
            print("  after refresh: task.attempts =", task.attempts, "task.status =", task.status, "task.output =", task.output)
        except Exception as exc:
            print(f"  session.refresh(task) raised: {type(exc).__name__}: {str(exc).splitlines()[0]}")

        # TODO comment needed: why the probe checks the row lock before the outer commit
        await check_row_lock_held(session_factory, task_id)

        if failure is not None:
            task.status = "pending"
            task.last_error = str(failure).splitlines()[0]
            session.add(
                Event(
                    run_id=run_id,
                    task_id=task_id,
                    type="task_retry",
                    payload={"phase": "plan", "discarded_output": output},
                )
            )

        try:
            await session.commit()
            print("  outer commit: ok")
        except Exception as exc:
            print(f"  outer commit raised: {type(exc).__name__}: {str(exc).splitlines()[0]}")

    await db_snapshot(session_factory, run_id)


def plan_succeeds():
    return PlanOutcome(
        reasoning="control",
        context_updates={"from_plan": True},
        tasks=[TaskSpec(type="child", key="child")],
    )


def plan_with_missing_key():
    return PlanOutcome(
        reasoning="second spec names a key that does not exist",
        context_updates={"leaked": True},
        tasks=[
            TaskSpec(type="child", key="child"),
            TaskSpec(type="orphan", depends_on=["missing_key"]),
        ],
    )


def plan_raises():
    raise RuntimeError("planner bug before any plan is applied")


def plan_with_missing_task_id():
    return PlanOutcome(
        reasoning="depends on a task id that does not exist",
        context_updates={"leaked": True},
        tasks=[TaskSpec(type="child", depends_on=[999999])],
    )


async def run_savepoint_scenario(session_factory, plan, flush_inside):
    run_id = await start_run(session_factory, "savepoint probe", TaskSpec(type="seed"))
    await settle_with_savepoint(session_factory, run_id, plan, flush_inside)


async def probe_cancel_mid_handler(session_factory):
    reg = Registry()
    calls = {"handler": 0}
    handler_started = asyncio.Event()

    async def slow(ctx):
        calls["handler"] += 1
        if calls["handler"] == 1:
            handler_started.set()
            await asyncio.sleep(30)
        return {"call": calls["handler"]}

    async def plan_slow(planner_input):
        return PlanOutcome(run_complete=True)

    reg.agent("slow", plan_next=plan_slow)(slow)

    run_id = await start_run(session_factory, "cancel mid handler", TaskSpec(type="slow"))
    async with session_factory() as session:
        seed_result = await session.execute(select(Task.id).where(Task.run_id == run_id))
        task_id = seed_result.scalar_one()

    worker = asyncio.ensure_future(process_one(session_factory, reg, run_id))
    await asyncio.wait_for(handler_started.wait(), timeout=5)
    worker.cancel()
    try:
        await worker
        print("  cancelled worker returned normally")
    except asyncio.CancelledError:
        print("  cancelled worker raised CancelledError")

    await check_row_lock_held(session_factory, task_id)

    # TODO comment needed: why a second process_one call proves the cancelled transaction rolled back
    second = await asyncio.wait_for(process_one(session_factory, reg, run_id), timeout=5)
    print("  second process_one claimed and settled the task:", second)
    print("  handler calls:", calls["handler"])
    await db_snapshot(session_factory, run_id)


SCENARIOS = [
    ("control: planning succeeds inside the savepoint", plan_succeeds, False),
    ("Python error partway through _apply_plan", plan_with_missing_key, False),
    ("planner raises before anything is applied", plan_raises, False),
    ("database error, no explicit flush inside the savepoint", plan_with_missing_task_id, False),
    ("database error, explicit flush inside the savepoint", plan_with_missing_task_id, True),
]


async def main():
    print("sqlalchemy", sqlalchemy.__version__, "asyncpg", asyncpg.__version__)

    for name, plan, flush_inside in SCENARIOS:
        print()
        print("=== " + name + " ===")
        schema, engine = await make_schema()
        try:
            await asyncio.wait_for(
                run_savepoint_scenario(make_session_factory(engine), plan, flush_inside),
                timeout=30,
            )
        finally:
            await drop_schema(schema, engine)

    print()
    print("=== cancelling process_one mid-handler ===")
    schema, engine = await make_schema()
    try:
        await asyncio.wait_for(probe_cancel_mid_handler(make_session_factory(engine)), timeout=30)
    finally:
        await drop_schema(schema, engine)


if __name__ == "__main__":
    asyncio.run(main())
