"""Rule planners are plain functions - test them as plain functions, no
database or LLM required. This is the pay-off of keeping "was the result
good enough" as a deterministic rule wherever the decision genuinely isn't
ambiguous: it's as cheap to test as any other pure function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cadenza.agents.finance.assumptions import MAX_ROUNDS, plan_next_assumptions
from cadenza.agents.finance.model_math import REQUIRED_ASSUMPTIONS

GOOD_ASSUMPTIONS = {
    "starting_revenue": 50_000_000,
    "revenue_growth": 0.15,
    "gross_margin": 0.6,
    "opex_pct_revenue": 0.35,
    "da_pct_revenue": 0.04,
    "capex_pct_revenue": 0.05,
    "nwc_pct_revenue": 0.1,
    "tax_rate": 0.21,
    "starting_cash": 10_000_000,
    "starting_debt": 5_000_000,
    "rationale": "example",
}


@dataclass
class FakePlannerInput:
    goal: str = "test"
    run_context: dict[str, Any] = field(default_factory=dict)
    completed_task_type: str = "gather_assumptions"
    completed_task_input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    task_id: int = 1


async def test_valid_assumptions_proceed_to_build():
    outcome = await plan_next_assumptions(
        FakePlannerInput(completed_task_input={"company": "Acme"}, output=GOOD_ASSUMPTIONS)
    )
    assert outcome.tasks[0].type == "build_income_statement"
    assert outcome.context_updates["assumptions"] == {k: GOOD_ASSUMPTIONS[k] for k in REQUIRED_ASSUMPTIONS}
    assert not outcome.escalate


async def test_invalid_assumptions_ask_again_with_specific_feedback():
    bad = {**GOOD_ASSUMPTIONS, "gross_margin": 4.5}  # nonsense: >100% margin
    outcome = await plan_next_assumptions(
        FakePlannerInput(completed_task_input={"company": "Acme", "round": 1}, output=bad)
    )
    assert outcome.tasks[0].type == "gather_assumptions"
    assert outcome.tasks[0].input["round"] == 2
    assert "gross_margin" in outcome.tasks[0].input["notes"]
    assert not outcome.escalate


async def test_invalid_assumptions_escalate_after_max_rounds():
    bad = {**GOOD_ASSUMPTIONS, "gross_margin": 4.5}
    outcome = await plan_next_assumptions(
        FakePlannerInput(completed_task_input={"company": "Acme", "round": MAX_ROUNDS}, output=bad)
    )
    assert outcome.escalate
    assert outcome.tasks == []
