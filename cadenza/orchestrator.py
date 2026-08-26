"""The loop: agent completes -> result assessed -> next requirement
identified -> next agent deployed -> continue.

One task, one transaction - claim, run the handler, plan what happens
next, write the new tasks and the status change, all in a single commit.
Exactly cadence's crash-safety invariant, extended: if the process dies
mid-task, the whole transaction rolls back, the row reverts to `pending`,
and any worker (including a fresh process that remembers nothing) picks it
straight back up. The dynamic part - deciding what happens next - gets the
same guarantee as the static part always had.

Holding the transaction open for the duration of an LLM call is a
deliberate trade-off, not an oversight: it is what makes "claimed twice" or
"half-applied plan" structurally impossible, at the cost of a row lock for
the seconds the call takes. Fine at this scale; a system doing enormously
long-running tasks would want to split claim and commit with a lease/
heartbeat instead (closer to what Temporal does).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .exceptions import Drop, Permanent
from .models import Event, RunStatus, Task, TaskDependency, TaskStatus, WorkflowRun
from .registry import AgentContext, PlanOutcome, Registry, TaskSpec

log = logging.getLogger("cadenza")

_CLAIM_SQL = text(
    """
    WITH next_task AS (
        SELECT t.id
        FROM cadenza_tasks t
        WHERE t.run_id = :run_id
          AND t.status = 'pending'
          AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())
          AND NOT EXISTS (
              SELECT 1
              FROM cadenza_task_dependencies d
              JOIN cadenza_tasks dep ON dep.id = d.depends_on_task_id
              WHERE d.task_id = t.id AND dep.status <> 'completed'
          )
        ORDER BY t.created_at
        FOR UPDATE OF t SKIP LOCKED
        LIMIT 1
    )
    UPDATE cadenza_tasks
    SET status = 'running', attempts = attempts + 1, updated_at = now()
    FROM next_task
    WHERE cadenza_tasks.id = next_task.id
    RETURNING cadenza_tasks.id
    """
)


def backoff(attempts: int) -> datetime:
    seconds = min(2**attempts, 60)
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@dataclass
class PlannerInputData:
    goal: str
    run_context: dict
    completed_task_type: str
    completed_task_input: dict
    output: dict
    task_id: int


async def start_run(
    session_factory: async_sessionmaker[AsyncSession], goal: str, first: TaskSpec
) -> int:
    """Create a run and its one seed task. Everything else is discovered
    dynamically by planners as tasks complete."""
    async with session_factory() as session:
        run = WorkflowRun(goal=goal, status=RunStatus.running.value, context={})
        session.add(run)
        await session.flush()

        task = Task(run_id=run.id, type=first.type, input=first.input)
        session.add(task)
        await session.flush()

        session.add(Event(run_id=run.id, task_id=task.id, type="run_started", payload={"goal": goal}))
        await session.commit()
        return run.id


async def _claim(session: AsyncSession, run_id: int) -> Task | None:
    result = await session.execute(_CLAIM_SQL, {"run_id": run_id})
    row = result.first()
    if row is None:
        return None
    return await session.get(Task, row.id)


async def _block_dependents(session: AsyncSession, run_id: int, task_id: int, reason: str) -> None:
    """A task that can never complete takes everything waiting on it with
    it. BFS outward so a failure several layers deep in the graph doesn't
    leave downstream tasks waiting forever for a dependency that will
    never satisfy."""
    frontier = [task_id]
    while frontier:
        result = await session.execute(
            text(
                """
                SELECT t.id FROM cadenza_tasks t
                JOIN cadenza_task_dependencies d ON d.task_id = t.id
                WHERE d.depends_on_task_id = ANY(:ids) AND t.status = 'pending'
                """
            ),
            {"ids": frontier},
        )
        newly_blocked = [row.id for row in result.all()]
        if not newly_blocked:
            break
        await session.execute(
            text(
                "UPDATE cadenza_tasks SET status = 'blocked', last_error = :reason, "
                "updated_at = now() WHERE id = ANY(:ids)"
            ),
            {"ids": newly_blocked, "reason": reason},
        )
        for tid in newly_blocked:
            session.add(Event(run_id=run_id, task_id=tid, type="task_blocked", payload={"reason": reason}))
        frontier = newly_blocked


async def _apply_plan(
    session: AsyncSession, run: WorkflowRun, task: Task, outcome: PlanOutcome
) -> None:
    if outcome.context_updates:
        # A single atomic UPDATE, not read-modify-write: two tasks from a
        # fan-out (e.g. three sensitivity scenarios) can commit their
        # context updates in any order, from different transactions,
        # without one clobbering the other's write. jsonb `||` is a
        # shallow merge, so agents that may run concurrently must write
        # disjoint top-level keys - documented in each agent's plan_next.
        await session.execute(
            text("UPDATE cadenza_runs SET context = context || :patch WHERE id = :run_id").bindparams(
                bindparam("patch", type_=JSONB)
            ),
            {"patch": outcome.context_updates, "run_id": run.id},
        )

    key_to_id: dict[str, int] = {}
    new_tasks: list[Task] = []
    for spec in outcome.tasks:
        new_task = Task(run_id=run.id, type=spec.type, input=spec.input, created_by_task_id=task.id)
        session.add(new_task)
        new_tasks.append(new_task)
    await session.flush()  # assign ids before wiring dependencies

    for spec, new_task in zip(outcome.tasks, new_tasks):
        if spec.key:
            key_to_id[spec.key] = new_task.id

    for spec, new_task in zip(outcome.tasks, new_tasks):
        for dep in spec.depends_on:
            dep_id = key_to_id[dep] if isinstance(dep, str) else dep
            session.add(TaskDependency(task_id=new_task.id, depends_on_task_id=dep_id))
        session.add(
            Event(
                run_id=run.id,
                task_id=new_task.id,
                type="task_created",
                payload={"type": spec.type, "created_by": task.id, "reasoning": outcome.reasoning},
            )
        )

    session.add(
        Event(
            run_id=run.id,
            task_id=task.id,
            type="plan_decision",
            payload={
                "reasoning": outcome.reasoning,
                "run_complete": outcome.run_complete,
                "next_task_types": [s.type for s in outcome.tasks],
            },
        )
    )

    if outcome.run_complete:
        run.status = RunStatus.completed.value
        session.add(Event(run_id=run.id, task_id=task.id, type="run_completed", payload={}))
    elif outcome.escalate:
        run.status = RunStatus.needs_review.value
        session.add(
            Event(
                run_id=run.id,
                task_id=task.id,
                type="run_escalated",
                payload={"reasoning": outcome.reasoning},
            )
        )


async def process_one(
    session_factory: async_sessionmaker[AsyncSession], registry: Registry, run_id: int
) -> bool:
    """Claim and fully process one ready task, in one transaction.
    Returns False if nothing was ready to claim."""
    async with session_factory() as session:
        task = await _claim(session, run_id)
        if task is None:
            return False

        run = await session.get(WorkflowRun, run_id)
        spec = registry.get(task.type)
        ctx = AgentContext(run_id=run_id, task_id=task.id, input=task.input, run_context=run.context)

        try:
            output = await spec.handler(ctx)
        except Drop as exc:
            task.status = TaskStatus.dropped.value
            task.last_error = str(exc)
            session.add(Event(run_id=run_id, task_id=task.id, type="task_dropped", payload={"reason": str(exc)}))
            await _block_dependents(session, run_id, task.id, f"upstream task dropped: {exc}")
            await session.commit()
            return True
        except Permanent as exc:
            task.status = TaskStatus.failed.value
            task.last_error = str(exc)
            session.add(Event(run_id=run_id, task_id=task.id, type="task_failed", payload={"reason": str(exc), "permanent": True}))
            await _block_dependents(session, run_id, task.id, f"upstream task failed: {exc}")
            await session.commit()
            return True
        except Exception as exc:  # Retry, or anything unexpected - same treatment
            if task.attempts >= spec.max_attempts:
                task.status = TaskStatus.failed.value
                task.last_error = f"gave up after {task.attempts} attempts: {exc}"
                log.warning("task %s (%s) gave up after %s attempts: %s", task.id, task.type, task.attempts, exc)
                session.add(Event(run_id=run_id, task_id=task.id, type="task_failed", payload={"reason": str(exc), "attempts": task.attempts}))
                await _block_dependents(session, run_id, task.id, f"upstream task exhausted retries: {exc}")
            else:
                task.status = TaskStatus.pending.value
                task.last_error = str(exc)
                task.next_attempt_at = backoff(task.attempts)
                session.add(Event(run_id=run_id, task_id=task.id, type="task_retry", payload={"reason": str(exc), "attempt": task.attempts}))
            await session.commit()
            return True

        task.status = TaskStatus.completed.value
        task.output = output
        session.add(Event(run_id=run_id, task_id=task.id, type="task_completed", payload={"output": output}))

        planner_input = PlannerInputData(
            goal=run.goal,
            run_context=run.context,
            completed_task_type=task.type,
            completed_task_input=task.input,
            output=output,
            task_id=task.id,
        )
        outcome = await spec.plan_next(planner_input)
        await _apply_plan(session, run, task, outcome)

        await session.commit()
        return True


async def _run_settled(session: AsyncSession, run_id: int) -> bool:
    run = await session.get(WorkflowRun, run_id)
    if run.status != RunStatus.running.value:
        return True
    result = await session.execute(
        text("SELECT count(*) FROM cadenza_tasks WHERE run_id = :run_id AND status IN ('pending', 'running')"),
        {"run_id": run_id},
    )
    return result.scalar_one() == 0


async def _finalize_if_stuck(session_factory: async_sessionmaker[AsyncSession], run_id: int) -> None:
    """Safety net: if every task has settled (completed/failed/blocked/
    dropped) but no planner ever signalled run_complete, don't leave the
    run silently 'running' forever - mark it for review."""
    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run.status != RunStatus.running.value:
            return
        result = await session.execute(
            text(
                "SELECT count(*) FROM cadenza_tasks WHERE run_id = :run_id "
                "AND status IN ('failed', 'blocked')"
            ),
            {"run_id": run_id},
        )
        has_problems = result.scalar_one() > 0
        run.status = RunStatus.needs_review.value if has_problems else RunStatus.completed.value
        session.add(
            Event(
                run_id=run_id,
                task_id=None,
                type="run_finalized_by_safety_net",
                payload={"status": run.status},
            )
        )
        await session.commit()


async def run_to_completion(
    session_factory: async_sessionmaker[AsyncSession],
    registry: Registry,
    run_id: int,
    *,
    concurrency: int = 3,
    poll_interval: float = 0.3,
) -> None:
    """Drive a run to a terminal state with `concurrency` workers pulling
    from the same ready queue - real concurrent dispatch (via SKIP LOCKED),
    not a for-loop pretending to be one."""

    async def worker() -> None:
        idle_polls = 0
        while True:
            worked = await process_one(session_factory, registry, run_id)
            if worked:
                idle_polls = 0
                continue
            async with session_factory() as session:
                if await _run_settled(session, run_id):
                    return
            idle_polls += 1
            await asyncio.sleep(poll_interval)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    await _finalize_if_stuck(session_factory, run_id)
