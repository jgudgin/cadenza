"""The one place that talks to Claude for planning decisions.

Built lazily so importing this module - and therefore importing any agent
module - never requires ANTHROPIC_API_KEY. Only the first real decision
does. Same reasoning as rss_watch's `_client()` in the cadence examples:
`cadenza status`/`cadenza trace` should work with no key at all.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import anthropic

from .registry import PlanOutcome, TaskSpec

MODEL = "claude-sonnet-5"

_DECIDE_TOOL = {
    "name": "decide_next_steps",
    "description": (
        "Decide what should happen next in this workflow, given the goal, "
        "the shared context gathered so far, and the result of the task "
        "that just finished."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "One or two sentences: why this decision follows from the result.",
            },
            "run_complete": {
                "type": "boolean",
                "description": "True only if the overall goal is now fully satisfied.",
            },
            "escalate": {
                "type": "boolean",
                "description": (
                    "True if this cannot proceed automatically and a human should look "
                    "(e.g. a problem you cannot safely auto-correct). Mutually exclusive "
                    "with run_complete; leave next_tasks empty when this is true."
                ),
            },
            "context_updates": {
                "type": "object",
                "description": "Facts to merge into the run's shared context, if any.",
            },
            "next_tasks": {
                "type": "array",
                "description": "Tasks to dispatch next. Empty if run_complete is true.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Must be one of the available task types given in the prompt.",
                        },
                        "input": {
                            "type": "object",
                            "description": "Input payload the agent needs.",
                        },
                        "key": {
                            "type": "string",
                            "description": (
                                "Optional local label for this task, so another task in "
                                "this same batch can depend on it before it has a real id."
                            ),
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Local `key` values of sibling tasks in this same batch that "
                                "must complete first. Use this for fan-in: a task that should "
                                "only start once several others (also being created right now) "
                                "are all done."
                            ),
                        },
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["reasoning", "run_complete", "next_tasks"],
    },
}


@lru_cache(maxsize=1)
def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic()


async def decide_next_steps(
    *,
    system: str,
    prompt: str,
) -> PlanOutcome:
    """One structured-output call: given a system prompt describing the
    workflow and the available next task types, and a user prompt
    describing what just happened, get back a typed PlanOutcome.

    Raises on malformed model output rather than guessing - a planner that
    silently does the wrong thing is worse than one that fails loudly and
    lets the task retry. Async, like everything else in the orchestrator
    loop: a planning call is exactly the kind of slow I/O that would
    otherwise stall every other in-flight task on the same worker.
    """
    message = await _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        tools=[_DECIDE_TOOL],
        tool_choice={"type": "tool", "name": "decide_next_steps"},
        messages=[{"role": "user", "content": prompt}],
    )

    tool_use = next(
        (block for block in message.content if block.type == "tool_use"), None
    )
    if tool_use is None:
        raise ValueError(f"planner call returned no tool_use block: {message.content!r}")

    decision: dict[str, Any] = tool_use.input
    tasks = [
        TaskSpec(
            type=t["type"],
            input=t.get("input", {}),
            key=t.get("key"),
            depends_on=list(t.get("depends_on", [])),
        )
        for t in decision.get("next_tasks", [])
    ]
    return PlanOutcome(
        tasks=tasks,
        reasoning=decision.get("reasoning", ""),
        run_complete=bool(decision.get("run_complete", False)),
        escalate=bool(decision.get("escalate", False)),
        context_updates=decision.get("context_updates", {}) or {},
    )


async def ask_claude_json(*, system: str, prompt: str, tool: dict[str, Any]) -> dict[str, Any]:
    """Generic structured-output call for agent handlers that need the
    model to do real work (draft assumptions, diagnose an imbalance,
    write a summary) rather than route control flow. Takes a caller-defined
    tool schema so each agent controls exactly what shape it needs back.
    """
    message = await _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],
    )
    tool_use = next(
        (block for block in message.content if block.type == "tool_use"), None
    )
    if tool_use is None:
        raise ValueError(f"expected a tool_use block, got: {message.content!r}")
    return tool_use.input
