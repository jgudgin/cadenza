"""Three independent instances of this agent type run concurrently -
bull/base/bear each recompute the whole model with a shifted growth
assumption, and none of them need to know the other two exist. That's the
point: they were created together, in one planning batch, but nothing here
coordinates between them at runtime - the dependency graph already encodes
that write_excel waits for all three, so each one just does its job and
writes to its own context key.

Each instance writes a distinct top-level context key (`sensitivity_bull`,
not a shared `sensitivity` dict with a `bull` sub-key) precisely because
concurrent context updates are merged with a shallow SQL-level `||`: two
writes to the same top-level key can race, but disjoint keys never can.
"""

from __future__ import annotations

from .. import registry
from ...exceptions import Permanent
from ...registry import AgentContext, PlanOutcome
from . import model_math


async def sensitivity_analysis(ctx: AgentContext) -> dict:
    scenario = ctx.input["scenario"]
    growth_delta = ctx.input["growth_delta"]
    try:
        assumptions = dict(ctx.run_context["assumptions"])
    except KeyError as exc:
        raise Permanent(f"missing required context: {exc}") from exc

    assumptions["revenue_growth"] = assumptions["revenue_growth"] + growth_delta
    income_statement = model_math.project_income_statement(assumptions)
    cash_flow = model_math.project_cash_flow(assumptions, income_statement)
    final_year = f"year_{model_math.PROJECTION_YEARS}"

    return {
        "scenario": scenario,
        "growth_delta": growth_delta,
        "final_year_revenue": income_statement[final_year]["revenue"],
        "final_year_net_income": income_statement[final_year]["net_income"],
        "final_year_cash": cash_flow[final_year]["ending_cash"],
    }


async def plan_next_sensitivity(input) -> PlanOutcome:  # noqa: ANN001
    scenario = input.output["scenario"]
    return PlanOutcome(
        reasoning=f"{scenario} scenario computed; write_excel is already queued to run once all three are done.",
        context_updates={f"sensitivity_{scenario}": input.output},
    )


registry.agent("sensitivity_analysis", plan_next=plan_next_sensitivity)(sensitivity_analysis)
