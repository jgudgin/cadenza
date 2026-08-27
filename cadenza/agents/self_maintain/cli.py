"""CLI helpers for the self-maintenance agent.

`print_subtask_outcomes` renders the result of a batch of subtasks (each
one either succeeded or failed, optionally with a detail message) as
plain text on stdout, plus a one-line summary count - the smallest useful
report a human skimming a terminal needs to tell "everything's fine" from
"go look at task 3".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubtaskOutcome:
    name: str
    success: bool
    detail: str | None = None


def format_subtask_outcomes(outcomes: list[SubtaskOutcome]) -> str:
    """Pure formatting, no I/O - kept separate so it's trivial to test the
    exact text without touching stdout."""
    if not outcomes:
        return "No subtasks were run."

    lines = []
    for outcome in outcomes:
        marker = "OK" if outcome.success else "FAILED"
        line = f"[{marker}] {outcome.name}"
        if outcome.detail:
            line += f": {outcome.detail}"
        lines.append(line)

    succeeded = sum(1 for o in outcomes if o.success)
    total = len(outcomes)
    lines.append(f"\n{succeeded}/{total} subtasks succeeded.")
    return "\n".join(lines)


def print_subtask_outcomes(outcomes: list[SubtaskOutcome]) -> None:
    """Print the formatted outcome report to stdout."""
    print(format_subtask_outcomes(outcomes))
