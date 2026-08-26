"""The last task in the graph. Plain rule planner: there is nothing left
after this, so it's the one place `run_complete=True` gets set."""

from __future__ import annotations

import json

from .. import registry
from ...llm import ask_claude_json
from ...registry import AgentContext, PlanOutcome

_SUMMARY_TOOL = {
    "name": "write_summary",
    "description": "Write a short narrative summary of a completed financial model.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 paragraphs: what the model shows, the key assumptions driving it, "
                    "and the spread between the bull/base/bear scenarios."
                ),
            }
        },
        "required": ["summary"],
    },
}

_SYSTEM = (
    "You write plain-English summaries of financial models for someone about "
    "to open the spreadsheet - point them at what matters, not a restatement "
    "of every number."
)


async def summarize_for_user(ctx: AgentContext) -> dict:
    rc = ctx.run_context
    prompt = (
        f"Company: {rc.get('company')}\n"
        f"Assumptions: {json.dumps(rc.get('assumptions', {}))}\n"
        f"Final-year balance sheet: {json.dumps(rc.get('balance_sheet', {}).get('year_3', {}))}\n"
        f"Sensitivity - bull: {json.dumps(rc.get('sensitivity_bull', {}))}\n"
        f"Sensitivity - base: {json.dumps(rc.get('sensitivity_base', {}))}\n"
        f"Sensitivity - bear: {json.dumps(rc.get('sensitivity_bear', {}))}\n"
        f"Workbook: {rc.get('excel_path')}"
    )
    return await ask_claude_json(system=_SYSTEM, prompt=prompt, tool=_SUMMARY_TOOL)


async def plan_next_summary(input) -> PlanOutcome:  # noqa: ANN001
    return PlanOutcome(
        reasoning="Summary written; nothing left to do.",
        context_updates={"summary": input.output["summary"]},
        run_complete=True,
    )


registry.agent("summarize_for_user", plan_next=plan_next_summary)(summarize_for_user)
