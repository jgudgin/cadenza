"""The one genuinely uncertain input: what should the model assume about
this company. An LLM agent drafts a full set of assumptions; a plain rule
function checks whether they're usable before anything downstream is
allowed to build on them.

This is the workflow's main "loop until good enough" branch, and it is
driven by real LLM output variability - not a staged failure - because the
model can genuinely return an incomplete or implausible draft.
"""

from __future__ import annotations

from .. import registry
from ...llm import ask_claude_json
from ...registry import AgentContext, PlanOutcome, TaskSpec
from .model_math import REQUIRED_ASSUMPTIONS, check_assumptions

MAX_ROUNDS = 3

# Shared with diagnose.py - a correction is just another draft of the same
# schema, so it reuses this tool definition rather than duplicating it.
ASSUMPTIONS_TOOL = {
    "name": "propose_assumptions",
    "description": "Propose a full set of financial-model assumptions for the given company.",
    "input_schema": {
        "type": "object",
        "properties": {
            **{
                key: {"type": "number", "description": f"{key}, as a decimal fraction where applicable"}
                for key in REQUIRED_ASSUMPTIONS
            },
            "rationale": {"type": "string", "description": "One or two sentences on where these numbers come from."},
        },
        "required": [*REQUIRED_ASSUMPTIONS, "rationale"],
    },
}


async def gather_assumptions(ctx: AgentContext) -> dict:
    company = ctx.input["company"]
    notes = ctx.input.get("notes", "")
    system = (
        "You are drafting starting assumptions for a simplified 3-year, "
        "3-statement financial model (income statement, balance sheet, cash "
        "flow). Give plausible, defensible figures for a company like this "
        "one - it is fine to be approximate, this is a planning model, not "
        "audited output."
    )
    prompt = f"Company: {company}."
    if notes:
        prompt += f"\n\nThe previous draft had problems - fix these specifically: {notes}"

    result = await ask_claude_json(system=system, prompt=prompt, tool=ASSUMPTIONS_TOOL)
    return result


async def plan_next_assumptions(input) -> PlanOutcome:  # noqa: ANN001 - structural PlannerInput
    issues = check_assumptions(input.output)
    company = input.completed_task_input["company"]
    round_ = input.completed_task_input.get("round", 1)

    if not issues:
        assumptions = {k: input.output[k] for k in REQUIRED_ASSUMPTIONS}
        return PlanOutcome(
            reasoning=f"Assumptions complete and plausible after round {round_}.",
            context_updates={"assumptions": assumptions, "company": company},
            tasks=[TaskSpec(type="build_income_statement", depends_on=[input.task_id])],
        )

    if round_ >= MAX_ROUNDS:
        return PlanOutcome(
            reasoning=f"Assumptions still have issues after {MAX_ROUNDS} rounds: {'; '.join(issues)}",
            escalate=True,
        )

    return PlanOutcome(
        reasoning=f"Round {round_} had problems, asking again: {'; '.join(issues)}",
        tasks=[
            TaskSpec(
                type="gather_assumptions",
                input={"company": company, "round": round_ + 1, "notes": "; ".join(issues)},
            )
        ],
    )


registry.agent("gather_assumptions", plan_next=plan_next_assumptions, max_attempts=3)(
    gather_assumptions
)
