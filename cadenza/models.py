"""The whole engine's state lives here, in Postgres. Nothing about a run is
held in process memory between steps - the same rule cadence is built on,
extended from a status column to a dependency graph.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    needs_review = "needs_review"


class TaskStatus(str, enum.Enum):
    pending = "pending"      # exists, but waiting on dependencies or backoff
    running = "running"      # claimed by a worker right now
    completed = "completed"
    failed = "failed"        # exhausted retries, or Permanent
    dropped = "dropped"      # planner decided it's no longer needed
    blocked = "blocked"      # a dependency failed; this can never become ready


class WorkflowRun(Base):
    __tablename__ = "cadenza_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.running.value)
    # The shared blackboard: accumulated facts every agent and planner can
    # read. Deliberately different from cadence, where steps share nothing -
    # here the tasks in one run are collaborating on one artifact, not
    # processing independent items, so shared context is the point rather
    # than the thing being avoided.
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    tasks: Mapped[list["Task"]] = relationship(back_populates="run")


class Task(Base):
    __tablename__ = "cadenza_tasks"
    # Postgres does not auto-index foreign key columns - only the primary
    # key gets one for free. This composite is what the claim query
    # (orchestrator.py::_NEXT_TASK_CTE, filtering run_id + status='pending')
    # and every status-scoped listing actually hit; without it those become
    # sequential scans as a run's task count grows.
    __table_args__ = (Index("ix_cadenza_tasks_run_id_status", "run_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("cadenza_runs.id"))
    type: Mapped[str] = mapped_column(String(100))  # registry key
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.pending.value)
    input: Mapped[dict] = mapped_column(JSONB, default=dict)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Only ever set by the opt-in claim/execute/commit lease path
    # (cadenza/orchestrator.py::process_one_with_lease) - the
    # one-transaction-per-task path (process_one) never touches this
    # column. While status == 'running' and this is in the past, the task
    # is considered abandoned (crashed worker, killed process, ...) and
    # sweep_expired_leases() will reset it to 'pending' so it becomes
    # claimable again instead of stuck forever behind a lock that no
    # longer exists.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Which task's planning decision produced this one, and why. This is
    # the audit trail that answers "why did the system decide to do this?"
    created_by_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("cadenza_tasks.id"), nullable=True
    )
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    run: Mapped[WorkflowRun] = relationship(back_populates="tasks")
    dependencies: Mapped[list["TaskDependency"]] = relationship(
        foreign_keys="TaskDependency.task_id", back_populates="task"
    )


class TaskDependency(Base):
    """task_id cannot become ready until depends_on_task_id is completed.

    A real join table rather than a JSON array of ids: it gets referential
    integrity for free, and lets the readiness query below be a plain
    NOT EXISTS rather than an unindexed array scan.
    """

    __tablename__ = "cadenza_task_dependencies"
    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id"),
        # The unique constraint above indexes (task_id, depends_on_task_id)
        # with task_id leading, which covers the readiness check
        # (`WHERE d.task_id = t.id`) but not _block_dependents' reverse
        # lookup (`WHERE d.depends_on_task_id = ANY(:ids)`) - that needs its
        # own index or it's a sequential scan every time a task fails.
        Index("ix_cadenza_task_dependencies_depends_on_task_id", "depends_on_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("cadenza_tasks.id"))
    depends_on_task_id: Mapped[int] = mapped_column(ForeignKey("cadenza_tasks.id"))

    task: Mapped[Task] = relationship(foreign_keys=[task_id], back_populates="dependencies")


class Event(Base):
    """Append-only trace of everything the orchestrator did and decided.

    This is the observability layer: `cadenza trace <run_id>` reads nothing
    but this table. Every dispatch, every planner decision (with its
    reasoning), every failure is one row here, in order.
    """

    __tablename__ = "cadenza_events"
    __table_args__ = (
        # GET /runs/{id}/trace and `cadenza trace` both do exactly this
        # lookup.
        Index("ix_cadenza_events_run_id", "run_id"),
        Index("ix_cadenza_events_task_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("cadenza_runs.id"))
    # Deliberately no ForeignKey here, unlike every other id column in this
    # file: AgentContext.report_progress (orchestrator.py::_progress_reporter)
    # writes an Event from a second, concurrent connection while the task's
    # own row is still held under process_one's open transaction. A foreign
    # key check needs a FOR KEY SHARE lock on the referenced task row, which
    # conflicts with the FOR NO KEY UPDATE the claim's UPDATE is still
    # holding - so a real FK here self-deadlocks the moment a handler calls
    # report_progress on itself (the handler's own transaction can't
    # proceed until report_progress's INSERT returns, and that INSERT can't
    # proceed until the handler's transaction commits). Nothing ever
    # deletes a Task, so there's no real integrity this constraint would
    # have protected anyway - just indexed instead.
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
