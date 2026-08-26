"""The one decision in this workflow genuinely handed to an LLM planner
rather than a rule: what to do about a validation result.

Two checks come back from `validate`, and they call for different
responses. An arithmetic imbalance is a bug in this codebase's own maths -
no amount of re-drafting assumptions fixes that, so it escalates straight
away. An assumption-quality problem is exactly the kind of judgement call
worth spending a model call on: is this fixable, and if so, how.

This is also where the workflow fans out (three independent sensitivity
scenarios, dispatched to run concurrently) and fans back in (the Excel
export waits on all three) - both decided in a single planning call, using
the same TaskSpec key/depends_on mechanism a rule planner would use.
"""

from __future__ import annotations

import json

from .. import registry
from ...exceptions import Permanent
from ...llm import decide_next_steps
from ...registry import AgentContext, PlanOutcome
from . import model_math

_SYSTEM = """\
You are the planning brain for an automated financial-modelling pipeline. \
A step called 'validate_model' just checked whether the model's arithmetic \
balances and whether its assumptions are plausible. Decide what happens next.

Available next task types:

- "diagnose_and_fix": an LLM agent that looks at the flagged issues and the \
current assumptions, and proposes corrected assumptions. Use this ONLY when \
there are assumption_issues that look fixable (e.g. a missing or \
out-of-range value), AND fix_rounds_so_far is less than 2. Give it input \
{"issues": [...], "current_assumptions": {...}}.

- "sensitivity_analysis": a deterministic recalculation of the model under \
a shifted growth assumption. Use this ONLY when balances is true and there \
are no assumption_issues. When you use it, create exactly THREE tasks of \
this type in this same batch - scenario "bull" (growth_delta +0.03), \
"base" (growth_delta 0.0), "bear" (growth_delta -0.03) - each with a \
distinct `key` matching its scenario name. ALSO create one "write_excel" \
task in the same batch, input {}, with depends_on set to all three scenario \
keys, so it only starts once every scenario has finished.

Rules:
- If balances is false, that is a bug in this system's own arithmetic, not \
something diagnose_and_fix can address (it only edits assumptions). Set \
escalate=true and explain why.
- If assumption_issues exist and fix_rounds_so_far >= 2, this has already \
been tried and hasn't converged - set escalate=true rather than trying again.
- If everything is healthy, do not set run_complete=true here - there is \
still sensitivity analysis, the Excel export, and a summary left. Only \
set run_complete=true when nothing at all remains.
"""


async def validate_model(ctx: AgentContext) -> dict:
    try:
        assumptions = ctx.run_context["assumptions"]
        balance_sheet = ctx.run_context["balance_sheet"]
    except KeyError as exc:
        raise Permanent(f"missing required context: {exc}") from exc
    return model_math.validate(assumptions, balance_sheet)


async def plan_next_validate(input) -> PlanOutcome:  # noqa: ANN001
    prompt = (
        f"Goal: {input.goal}\n"
        f"fix_rounds_so_far: {input.run_context.get('fix_rounds', 0)}\n"
        f"Validation result: {json.dumps(input.output)}\n"
        f"Current assumptions: {json.dumps(input.run_context.get('assumptions', {}))}"
    )
    return await decide_next_steps(system=_SYSTEM, prompt=prompt)


registry.agent("validate_model", plan_next=plan_next_validate)(validate_model)
