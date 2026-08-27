"""plan_self_maintain is a rule planner - a plain function, tested the
same way test_finance_planners.py tests plan_next_assumptions: no
database, no LLM, no gh/git required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cadenza.agents.self_maintain.maintain import LABEL, MAX_OPEN_PRS, plan_self_maintain


@dataclass
class FakePlannerInput:
    goal: str = "fix the thing"
    run_context: dict[str, Any] = field(default_factory=dict)
    completed_task_type: str = "self_maintain"
    completed_task_input: dict[str, Any] = field(default_factory=lambda: {"task": "fix the thing"})
    output: dict[str, Any] = field(default_factory=dict)
    task_id: int = 1


async def test_pr_opened_completes_the_run():
    outcome = await plan_self_maintain(
        FakePlannerInput(output={"tests_passed": True, "changed_files": ["a.py"], "pr_url": "https://x/1"})
    )
    assert outcome.run_complete
    assert not outcome.escalate
    assert outcome.tasks == []


async def test_first_test_failure_retries_once_with_feedback():
    outcome = await plan_self_maintain(
        FakePlannerInput(
            completed_task_input={"task": "fix the thing"},
            output={"tests_passed": False, "changed_files": ["a.py"], "test_output": "boom"},
        )
    )
    assert not outcome.escalate
    assert not outcome.run_complete
    assert len(outcome.tasks) == 1
    retry = outcome.tasks[0]
    assert retry.type == "self_maintain"
    assert retry.input["task"] == "fix the thing"
    assert retry.input["previous_failure"] == "boom"


async def test_second_test_failure_escalates_instead_of_retrying_forever():
    outcome = await plan_self_maintain(
        FakePlannerInput(
            completed_task_input={"task": "fix the thing", "previous_failure": "boom"},
            output={"tests_passed": False, "changed_files": ["a.py"], "test_output": "still boom"},
        )
    )
    assert outcome.escalate
    assert outcome.tasks == []


async def test_no_changes_escalates():
    outcome = await plan_self_maintain(
        FakePlannerInput(output={"tests_passed": True, "changed_files": [], "summary": "nothing to do"})
    )
    assert outcome.escalate
    assert outcome.tasks == []


async def test_pr_cap_hit_escalates_with_the_reason():
    outcome = await plan_self_maintain(
        FakePlannerInput(output={"skipped": True, "reason": f"{MAX_OPEN_PRS} {LABEL} PRs already open (cap {MAX_OPEN_PRS})"})
    )
    assert outcome.escalate
    assert outcome.tasks == []
    assert str(MAX_OPEN_PRS) in outcome.reasoning
