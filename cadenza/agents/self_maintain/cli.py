"""CLI helpers for the self-maintenance agent.

`print_subtask_outcomes` renders the per-subtask results of a
self-maintenance run (e.g. "ran the linter", "ran the test suite",
"opened a fix PR") as one line per subtask, so a human watching the CLI
can see at a glance what succeeded, what failed and why, without having
to go dig through `cadenza status`/`cadenza trace`.

Each outcome is expected to look roughly like:

    {"task_id": 12, "type": "run_tests", "status": "succeeded"}
    {"task_id": 13, "type": "apply_fix", "status": "failed", "error": "patch did not apply"}

but every field is read defensively - missing/unexpected fields degrade
to a placeholder instead of raising, since this is a reporting helper,
not something that should ever crash a run that otherwise finished.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

_STATUS_LABELS = {
    "succeeded": "OK",
    "failed": "FAILED",
    "dropped": "SKIPPED",
    "blocked": "BLOCKED",
}


def print_subtask_outcomes(outcomes: Sequence[Mapping[str, Any]]) -> None:
    """Print a one-line summary for each subtask outcome.

    Prints a friendly placeholder message instead of nothing when
    `outcomes` is empty, so a run with no subtasks doesn't look like the
    reporting step silently did nothing.
    """
    if not outcomes:
        print("No subtasks were run.")
        return

    for outcome in outcomes:
        task_id = outcome.get("task_id", "?")
        task_type = outcome.get("type", "unknown")
        status = outcome.get("status", "unknown")
        label = _STATUS_LABELS.get(status, status.upper() if isinstance(status, str) else str(status))
        error = outcome.get("error")

        line = f"[{label}] task {task_id} ({task_type})"
        if error:
            line += f": {error}"
        print(line)
