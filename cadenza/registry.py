"""The pluggable surface: what an agent/tool looks like, and what a planner
is allowed to decide once one finishes.

Two kinds of "what happens next" are deliberately kept separate:

- A **rule planner** is a plain function - cheap, instant, deterministic.
  Most transitions in a real workflow are not actually ambiguous (after the
  income statement, you always build the balance sheet next), so they
  shouldn't cost an LLM call or be a place a model can get creative.
- An **LLM planner** calls out to a model with a forced-schema tool so the
  decision is genuinely structured output, not free text. Reserved for the
  transitions that need judgement: is this data complete enough, did
  validation actually pass, is the goal now satisfied.

Framework-agnostic on purpose: nothing here depends on a specific
orchestration library. Swapping the LLM planner's model, or the whole
Registry's storage backend, does not touch this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


async def _default_report_progress(message: str) -> None:
    """The no-op AgentContext falls back to when nothing wires a real one
    in (constructing AgentContext directly, e.g. in a test) - so calling
    ctx.report_progress never requires a caller to stub it out."""


@dataclass
class AgentContext:
    """What an agent handler is handed to do its job."""

    run_id: int
    task_id: int
    input: dict[str, Any]
    run_context: dict[str, Any]  # the run's shared blackboard, read-only here
    # Best-effort, informational only - not part of the state machine. A
    # long-running handler (cadenza-self-maintain's coding loop is the
    # motivating case: up to 20 LLM tool-call turns inside one task) can
    # call this to narrate what it's doing right now. Each call is its own
    # short transaction, committed immediately and independently of
    # whatever transaction the handler itself is running inside - see
    # orchestrator.py's _progress_reporter for why that matters: it's what
    # makes a still-running task's progress visible to a live reader (a
    # dashboard) instead of waiting for the task to settle.
    report_progress: Callable[[str], Awaitable[None]] = _default_report_progress


AgentHandler = Callable[[AgentContext], Awaitable[dict[str, Any]]]


@dataclass
class TaskSpec:
    """A task a planner wants created.

    `depends_on` may reference either the numeric id of an already-existing
    task, or the string `key` of another TaskSpec in the same planning
    batch - this is what makes fan-out/fan-in possible in a single
    decision: N sibling tasks created together, plus a join task that
    depends on all N of them, none of which have real ids yet. The
    orchestrator resolves string keys to real ids after inserting the
    batch, in the same transaction.
    """

    type: str
    input: dict[str, Any] = field(default_factory=dict)
    depends_on: list[int | str] = field(default_factory=list)
    key: str | None = None


@dataclass
class PlanOutcome:
    """What a planner decided after one task finished.

    Exactly one of `run_complete` / `escalate` / (non-empty `tasks`) should
    normally be set - completion and escalation are both terminal, and are
    distinct from each other precisely because "successfully finished" and
    "stuck, a human should look" must never be reported as the same thing.
    """

    tasks: list[TaskSpec] = field(default_factory=list)
    reasoning: str = ""
    run_complete: bool = False
    escalate: bool = False
    context_updates: dict[str, Any] = field(default_factory=dict)


class PlannerInput(Protocol):
    goal: str
    run_context: dict[str, Any]
    completed_task_type: str
    completed_task_input: dict[str, Any]
    output: dict[str, Any]
    task_id: int


PlanFn = Callable[[PlannerInput], Awaitable[PlanOutcome]]


@dataclass
class AgentSpec:
    handler: AgentHandler
    plan_next: PlanFn
    max_attempts: int = 3


class Registry:
    """Name -> agent mapping. One instance per process; agents register
    themselves at import time via `@registry.agent(...)`."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {}

    def agent(
        self, name: str, *, plan_next: PlanFn, max_attempts: int = 3
    ) -> Callable[[AgentHandler], AgentHandler]:
        def decorator(fn: AgentHandler) -> AgentHandler:
            self._agents[name] = AgentSpec(handler=fn, plan_next=plan_next, max_attempts=max_attempts)
            return fn

        return decorator

    def get(self, name: str) -> AgentSpec:
        try:
            return self._agents[name]
        except KeyError:
            raise KeyError(
                f"no agent registered as {name!r} - registered types: {sorted(self._agents)}"
            ) from None

    def __contains__(self, name: str) -> bool:
        return name in self._agents


AgentResult = dict[str, Any]
