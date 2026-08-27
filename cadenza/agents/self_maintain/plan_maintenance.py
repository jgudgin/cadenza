"""Fans a refined maintenance brief out into independent `self_maintain`
tasks - one LLM decomposition call, then the same TaskSpec fan-out
mechanism the finance workflow used for its three concurrent sensitivity
scenarios. No fan-in here: each subtask becomes its own PR from its own
coding agent in its own branch, entirely independent of its siblings -
there is nothing to join.

Deliberately a rule planner, not an LLM one: "did this subtask produce a
PR or does it need a retry" is exactly `plan_self_maintain`'s job, already
solved per-subtask - splitting the brief is the only judgement call here.
"""

from __future__ import annotations

from .. import registry
from ...llm import ask_claude_json
from ...registry import AgentContext, PlanOutcome, TaskSpec

_DECOMPOSE_TOOL = {
    "name": "split_into_subtasks",
    "description": (
        "Split a maintenance brief into independent subtasks, each small enough for "
        "one bounded coding agent and touching disjoint files/concerns where "
        "possible, to avoid merge conflicts between them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "1 to 6 concrete, self-contained task descriptions. Each becomes "
                    "its own PR from its own coding agent - keep them disjoint "
                    "(different files/functions) wherever the brief allows it."
                ),
            },
            "reasoning": {"type": "string"},
        },
        "required": ["subtasks", "reasoning"],
    },
}

_SYSTEM = """\
You are the planning step of an automated multi-agent coding system. A \
brief describing some maintenance work comes in; split it into 1-6 \
independent subtasks, each concrete enough for a small bounded coding agent \
(about 20 tool-call turns) to complete alone, in its own git branch, ending \
in its own pull request.

Prefer subtasks that touch different files or clearly separate concerns - \
two subtasks that touch the same code will conflict with each other when a \
human reviews and merges them later. If the brief is already a single \
small piece of work, return exactly one subtask; do not split for the sake \
of splitting.
"""


async def plan_maintenance(ctx: AgentContext) -> dict:
    brief = ctx.input["brief"]
    decision = await ask_claude_json(system=_SYSTEM, prompt=brief, tool=_DECOMPOSE_TOOL)
    subtasks = decision.get("subtasks") or [brief]
    return {"subtasks": subtasks, "reasoning": decision.get("reasoning", "")}


async def plan_next_split(input) -> PlanOutcome:  # noqa: ANN001
    subtasks = input.output.get("subtasks") or []
    if not subtasks:
        return PlanOutcome(escalate=True, reasoning="decomposition produced no subtasks")
    return PlanOutcome(
        tasks=[TaskSpec(type="self_maintain", input={"task": t}) for t in subtasks],
        reasoning=input.output.get("reasoning", ""),
    )


registry.agent("plan_maintenance", plan_next=plan_next_split)(plan_maintenance)
