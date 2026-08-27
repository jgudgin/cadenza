"""plan_next_split is a rule planner - tested as a pure function, same as
test_self_maintain_planner.py. plan_maintenance itself makes one LLM call
(mocked here, same canned-for-CI principle used throughout this repo).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cadenza.agents.self_maintain import plan_maintenance as plan_maintenance_mod
from cadenza.agents.self_maintain.plan_maintenance import plan_maintenance, plan_next_split
from cadenza.registry import AgentContext


@dataclass
class FakePlannerInput:
    goal: str = "fix a bunch of stuff"
    run_context: dict[str, Any] = field(default_factory=dict)
    completed_task_type: str = "plan_maintenance"
    completed_task_input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    task_id: int = 1


async def test_splits_into_independent_self_maintain_tasks():
    outcome = await plan_next_split(
        FakePlannerInput(output={"subtasks": ["add test A", "fix docstring B"], "reasoning": "disjoint files"})
    )
    assert len(outcome.tasks) == 2
    assert all(t.type == "self_maintain" for t in outcome.tasks)
    assert outcome.tasks[0].input == {"task": "add test A"}
    assert outcome.tasks[1].input == {"task": "fix docstring B"}
    assert not outcome.run_complete
    assert not outcome.escalate


async def test_empty_decomposition_escalates_instead_of_silently_doing_nothing():
    outcome = await plan_next_split(FakePlannerInput(output={"subtasks": []}))
    assert outcome.escalate
    assert outcome.tasks == []


async def test_plan_maintenance_handler_falls_back_to_the_brief_if_the_model_returns_no_subtasks(monkeypatch):
    async def fake_ask_claude_json(*, system, prompt, tool):
        return {"subtasks": [], "reasoning": "nothing to split"}

    monkeypatch.setattr(plan_maintenance_mod, "ask_claude_json", fake_ask_claude_json)

    output = await plan_maintenance(AgentContext(run_id=1, task_id=1, input={"brief": "fix the thing"}, run_context={}))

    assert output["subtasks"] == ["fix the thing"]
