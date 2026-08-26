"""Three deterministic tool agents - no LLM involved. They exist as
separate agent types, rather than one big "build_model" step, because that
is what makes the dependency graph mean something: cash flow genuinely
needs the income statement's net income, and the balance sheet genuinely
needs both. A missing upstream context key is a wiring bug, not a
transient failure, so it is raised as Permanent rather than retried.
"""

from __future__ import annotations

from .. import registry
from ...exceptions import Permanent
from ...registry import AgentContext, PlanOutcome, TaskSpec
from . import model_math


async def build_income_statement(ctx: AgentContext) -> dict:
    try:
        assumptions = ctx.run_context["assumptions"]
    except KeyError as exc:
        raise Permanent(f"missing required context: {exc}") from exc
    return {"income_statement": model_math.project_income_statement(assumptions)}


async def plan_next_income_statement(input) -> PlanOutcome:  # noqa: ANN001
    return PlanOutcome(
        reasoning="Income statement built; cash flow needs its net income figures next.",
        context_updates={"income_statement": input.output["income_statement"]},
        tasks=[TaskSpec(type="build_cash_flow", depends_on=[input.task_id])],
    )


async def build_cash_flow(ctx: AgentContext) -> dict:
    try:
        assumptions = ctx.run_context["assumptions"]
        income_statement = ctx.run_context["income_statement"]
    except KeyError as exc:
        raise Permanent(f"missing required context: {exc}") from exc
    return {"cash_flow_statement": model_math.project_cash_flow(assumptions, income_statement)}


async def plan_next_cash_flow(input) -> PlanOutcome:  # noqa: ANN001
    return PlanOutcome(
        reasoning="Cash flow built; balance sheet needs ending cash and capex from it.",
        context_updates={"cash_flow_statement": input.output["cash_flow_statement"]},
        tasks=[TaskSpec(type="build_balance_sheet", depends_on=[input.task_id])],
    )


async def build_balance_sheet(ctx: AgentContext) -> dict:
    try:
        assumptions = ctx.run_context["assumptions"]
        income_statement = ctx.run_context["income_statement"]
        cash_flow = ctx.run_context["cash_flow_statement"]
    except KeyError as exc:
        raise Permanent(f"missing required context: {exc}") from exc
    return {
        "balance_sheet": model_math.project_balance_sheet(assumptions, income_statement, cash_flow)
    }


async def plan_next_balance_sheet(input) -> PlanOutcome:  # noqa: ANN001
    return PlanOutcome(
        reasoning="Balance sheet built; time to validate the whole model before anything downstream uses it.",
        context_updates={"balance_sheet": input.output["balance_sheet"]},
        tasks=[TaskSpec(type="validate_model", depends_on=[input.task_id])],
    )


registry.agent("build_income_statement", plan_next=plan_next_income_statement)(build_income_statement)
registry.agent("build_cash_flow", plan_next=plan_next_cash_flow)(build_cash_flow)
registry.agent("build_balance_sheet", plan_next=plan_next_balance_sheet)(build_balance_sheet)
