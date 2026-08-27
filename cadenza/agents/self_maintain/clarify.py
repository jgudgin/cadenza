"""Turns a rough, possibly one-line request into a concrete brief for
`plan_maintenance` to split into subtasks - by asking the terminal a
bounded number of clarifying questions first, the way a person would
before splitting work across a team.

Lives outside the orchestrator on purpose: cadenza's task queue has no
"pause mid-run for a human" primitive, and a real-time back-and-forth
doesn't want one. This entire exchange happens before `start_run` is ever
called - by the time a task exists, it's already unambiguous.
"""

from __future__ import annotations

from typing import Callable

from ...llm import ask_claude_json

MAX_ROUNDS = 3

_CLARIFY_TOOL = {
    "name": "clarify_or_proceed",
    "description": (
        "Decide whether the request is concrete enough to split into independent "
        "subtasks yet, or whether it needs clarifying questions first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Up to 3 short clarifying questions. Empty once the request is "
                    "concrete enough to decompose - do not ask more than necessary."
                ),
            },
            "refined_brief": {
                "type": "string",
                "description": (
                    "Only set when questions is empty: a clear, self-contained "
                    "description of the work, informed by every answer so far."
                ),
            },
        },
        "required": ["questions"],
    },
}

_SYSTEM = """\
You are the intake step for an automated multi-agent coding system: a rough, \
possibly one-line request comes in, and your job is to turn it into a \
concrete brief that a planner will split into independent subtasks, each \
handed to its own small bounded coding agent working in its own git branch.

Ask clarifying questions ONLY when genuinely necessary to avoid the \
downstream split being wrong or wasteful - vague scope, an ambiguous target, \
a missing constraint that would change what gets built. Do not ask a \
question whose answer wouldn't change the outcome. Prefer proceeding with a \
reasonable default over asking. Never ask more than 3 questions total \
across the whole conversation.
"""


async def clarify_interactively(rough_idea: str, *, ask: Callable[[str], str]) -> str:
    """Runs the clarify-then-decide loop against the real API, calling the
    synchronous `ask(question)` for each question the model raises, until
    it returns a refined brief or MAX_ROUNDS is exhausted."""
    transcript = f"Request: {rough_idea}"
    for _ in range(MAX_ROUNDS):
        decision = await ask_claude_json(system=_SYSTEM, prompt=transcript, tool=_CLARIFY_TOOL)
        questions = decision.get("questions") or []
        if not questions:
            return decision.get("refined_brief") or rough_idea
        for question in questions:
            answer = ask(question)
            transcript += f"\nQ: {question}\nA: {answer}"
    # Out of rounds - proceed with what we've got rather than loop forever;
    # the transcript itself is still a perfectly usable brief.
    return transcript
