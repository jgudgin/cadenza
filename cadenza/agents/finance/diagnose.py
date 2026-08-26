"""Only reached when validate_model's planner decided the assumption
problems look fixable. Re-drafts the assumptions with the specific issues
in view, then sends the workflow back to build_income_statement - a real
replan, not just forward progress: the graph loops backward through the
same statement-building chain with corrected inputs.
"""

from __future__ import annotations

import json

from .. import registry
from ...llm import ask_claude_json
from ...registry import AgentContext, PlanOutcome, TaskSpec
from .assumptions import ASSUMPTIONS_TOOL
from .model_math import REQUIRED_ASSUMPTIONS

_SYSTEM = (
    "You previously drafted financial-model assumptions for a company. "
    "Validation found specific problems with them. Propose a corrected "
    "full set of assumptions that resolves exactly these problems while "
    "staying internally consistent and plausible."
)


async def diagnose_and_fix(ctx: AgentContext) -> dict:
    prompt = (
        f"Current assumptions: {json.dumps(ctx.input['current_assumptions'])}\n"
        f"Problems found: {'; '.join(ctx.input['issues'])}"
    )
    return await ask_claude_json(system=_SYSTEM, prompt=prompt, tool=ASSUMPTIONS_TOOL)


async def plan_next_diagnose(input) -> PlanOutcome:  # noqa: ANN001
    corrected = {k: input.output[k] for k in REQUIRED_ASSUMPTIONS}
    fix_rounds = input.run_context.get("fix_rounds", 0) + 1
    return PlanOutcome(
        reasoning=f"Applied corrected assumptions (fix round {fix_rounds}); rebuilding the model from the income statement forward.",
        context_updates={"assumptions": corrected, "fix_rounds": fix_rounds},
        tasks=[TaskSpec(type="build_income_statement", depends_on=[input.task_id])],
    )


registry.agent("diagnose_and_fix", plan_next=plan_next_diagnose)(diagnose_and_fix)
