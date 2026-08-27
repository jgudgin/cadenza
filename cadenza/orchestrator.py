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

That lease/heartbeat alternative lives in this module too, further down
(`process_one_with_lease`, `claim_with_lease`, `sweep_expired_leases`) -
purely additive and opt-in. It does not replace `process_one` /
`run_to_completion`; a project picks whichever model fits a given task
type, per task type, and both can be in flight in the same run.
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
from .registry import AgentContext, AgentSpec, PlanOutcome, Registry, TaskSpec

log = logging.getLogger("cadenza")

# Shared by both claim queries below (the held-transaction model and the
# lease model) - same "what's ready" rule either way, only the SET clause
# differs (the lease model also stamps an expiry). Keeping it as one string
# means that rule can't drift out of sync between the two claim paths.
_NEXT_TASK_CTE = """
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
"""

_CLAIM_SQL = text(
    _NEXT_TASK_CTE
    + """
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


async def _settle_task(
    session: AsyncSession,
    run: WorkflowRun,
    task: Task,
    spec: AgentSpec,
    *,
    outcome_kind: str,
    output: dict | None = None,
    error: Exception | None = None,
) -> None:
    """Given a task that just finished - successfully, or with one of the
    Drop/Permanent/retry outcomes - apply the resulting state transition.
    Shared by `process_one`'s inline try/except and
    `process_one_with_lease`'s split-transaction equivalent, so the two
    execution models can't drift apart on what 'dropped', 'permanent',
    'exhausted retries', or 'completed' actually means. Does not commit -
    callers share one transaction boundary around this, but not always the
    same one (this can run inside the claim's own transaction, or in a
    fresh one opened after the handler already returned)."""
    run_id = run.id

    if outcome_kind == "drop":
        task.status = TaskStatus.dropped.value
        task.last_error = str(error)
        session.add(Event(run_id=run_id, task_id=task.id, type="task_dropped", payload={"reason": str(error)}))
        await _block_dependents(session, run_id, task.id, f"upstream task dropped: {error}")
        return

    if outcome_kind == "permanent":
        task.status = TaskStatus.failed.value
        task.last_error = str(error)
        session.add(
            Event(run_id=run_id, task_id=task.id, type="task_failed", payload={"reason": str(error), "permanent": True})
        )
        await _block_dependents(session, run_id, task.id, f"upstream task failed: {error}")
        return

    if outcome_kind == "retry":  # Retry, or anything unexpected - same treatment
        if task.attempts >= spec.max_attempts:
            task.status = TaskStatus.failed.value
            task.last_error = f"gave up after {task.attempts} attempts: {error}"
            log.warning("task %s (%s) gave up after %s attempts: %s", task.id, task.type, task.attempts, error)
            session.add(
                Event(run_id=run_id, task_id=task.id, type="task_failed", payload={"reason": str(error), "attempts": task.attempts})
            )
            await _block_dependents(session, run_id, task.id, f"upstream task exhausted retries: {error}")
        else:
            task.status = TaskStatus.pending.value
            task.last_error = str(error)
            task.next_attempt_at = backoff(task.attempts)
            session.add(
                Event(run_id=run_id, task_id=task.id, type="task_retry", payload={"reason": str(error), "attempt": task.attempts})
            )
        return

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
            await _settle_task(session, run, task, spec, outcome_kind="drop", error=exc)
        except Permanent as exc:
            await _settle_task(session, run, task, spec, outcome_kind="permanent", error=exc)
        except Exception as exc:  # Retry, or anything unexpected - same treatment
            await _settle_task(session, run, task, spec, outcome_kind="retry", error=exc)
        else:
            await _settle_task(session, run, task, spec, outcome_kind="completed", output=output)

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


# ---------------------------------------------------------------------------
# Opt-in lease/heartbeat model, for handlers that legitimately run for hours
# instead of seconds.
#
# `process_one` above holds one open transaction (and therefore one row
# lock) for the entire handler call, on purpose: it's what makes "claimed
# twice" and "half-applied plan" structurally impossible, for free, as long
# as the handler is fast enough that a held lock is cheap. When it isn't -
# a handler that legitimately runs for hours - holding a transaction (and a
# database connection) open that whole time is the wrong trade.
#
# The model here instead:
#   1. claim_with_lease: one short transaction claims the task, sets
#      status='running' and lease_expires_at = now() + lease_seconds, and
#      commits immediately - the row lock is released right away, not held
#      for the handler's duration.
#   2. the handler runs with *no* transaction held open at all.
#   3. a second short transaction records the result and applies the
#      planner's decision, exactly like the tail of process_one does.
#
# The cost of that trade: between (1) and (3) nothing but lease_expires_at
# says the task is spoken for. If the worker crashes before (3), the task
# sits in 'running' until sweep_expired_leases() notices the lease expired
# and resets it to 'pending' - recovery is a periodic sweep instead of an
# automatic rollback. That's the deliberate trade for not pinning a
# connection/lock for hours: a real bound (the lease) on how long a crash
# can leave a task stuck, instead of an unbounded one.
# ---------------------------------------------------------------------------

DEFAULT_LEASE_SECONDS = 3600.0  # 1 hour; pick per task type based on the handler

_CLAIM_LEASE_SQL = text(
    _NEXT_TASK_CTE
    + """
    UPDATE cadenza_tasks
    SET status = 'running',
        attempts = attempts + 1,
        updated_at = now(),
        lease_expires_at = now() + make_interval(secs => :lease_seconds)
    FROM next_task
    WHERE cadenza_tasks.id = next_task.id
    RETURNING cadenza_tasks.id
    """
)

_SWEEP_LEASE_SQL = text(
    """
    UPDATE cadenza_tasks
    SET status = 'pending', lease_expires_at = NULL, updated_at = now()
    WHERE run_id = :run_id
      AND status = 'running'
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at <= now()
    RETURNING id
    """
)


async def claim_with_lease(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> int | None:
    """The claim step of the lease model: one short transaction, committed
    immediately, so the row lock is gone before anyone even thinks about
    calling a handler. Returns the claimed task's id, or None if nothing
    was ready."""
    async with session_factory() as session:
        result = await session.execute(
            _CLAIM_LEASE_SQL, {"run_id": run_id, "lease_seconds": lease_seconds}
        )
        row = result.first()
        if row is None:
            return None
        task_id = row.id
        session.add(
            Event(
                run_id=run_id,
                task_id=task_id,
                type="task_leased",
                payload={"lease_seconds": lease_seconds},
            )
        )
        await session.commit()
        return task_id


async def process_one_with_lease(
    session_factory: async_sessionmaker[AsyncSession],
    registry: Registry,
    run_id: int,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> bool:
    """The lease/heartbeat alternative to `process_one`. Claim and commit
    are two independent short transactions instead of one held open for
    the whole handler call; between them the handler runs free of any open
    transaction or row lock. Returns False if nothing was ready to claim.

    Opt-in and additive: nothing about `process_one` changes, and a
    project can use either model per task type."""
    task_id = await claim_with_lease(session_factory, run_id, lease_seconds)
    if task_id is None:
        return False

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        run = await session.get(WorkflowRun, run_id)
        spec = registry.get(task.type)
        ctx = AgentContext(run_id=run_id, task_id=task.id, input=task.input, run_context=run.context)

    # No transaction (and no lock) held here, however long the handler
    # takes - the entire point of this model.
    error: Exception | None = None
    outcome_kind = "completed"
    output: dict | None = None
    try:
        output = await spec.handler(ctx)
    except Drop as exc:
        error, outcome_kind = exc, "drop"
    except Permanent as exc:
        error, outcome_kind = exc, "permanent"
    except Exception as exc:  # Retry, or anything unexpected - same treatment
        error, outcome_kind = exc, "retry"

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        run = await session.get(WorkflowRun, run_id)
        spec = registry.get(task.type)
        # The lease has done its job; whatever happens next resolves the
        # task's fate immediately, so there's nothing left to lease.
        task.lease_expires_at = None

        await _settle_task(session, run, task, spec, outcome_kind=outcome_kind, output=output, error=error)

        await session.commit()
        return True


async def sweep_expired_leases(
    session_factory: async_sessionmaker[AsyncSession], run_id: int
) -> list[int]:
    """Recover tasks whose lease expired before the worker holding it ever
    reached the commit step - most likely because that worker crashed.
    Resets status back to 'pending' (attempts unchanged, since the task
    never actually got a chance to run to a result) so it becomes
    claimable again, exactly like a plain pending task.

    Call this periodically (a cron job, a background task, a poll loop)
    for any run using the lease model. The held-transaction model
    (`process_one`) needs no equivalent - a rollback there does the same
    job for free, immediately, rather than after a sweep interval."""
    async with session_factory() as session:
        result = await session.execute(_SWEEP_LEASE_SQL, {"run_id": run_id})
        recovered_ids = [row.id for row in result.all()]
        for task_id in recovered_ids:
            session.add(
                Event(
                    run_id=run_id,
                    task_id=task_id,
                    type="lease_expired_reclaimed",
                    payload={},
                )
            )
        await session.commit()
        return recovered_ids
