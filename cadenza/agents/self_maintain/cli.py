"""`cadenza-self-maintain run "some task"` - the friendly entry point on
top of the generic engine CLI, the same pattern cadenza-modeler's `run`
command uses on top of `build_cli` (see README's "Building a project on
this"): seed one `self_maintain` task, drive it to completion, print
what happened.
"""

from __future__ import annotations

import asyncio

from .. import registry  # importing this loads .env - see cadenza/agents/__init__.py
from ...cli import build_cli, console, print_status
from ...db import create_tables, make_engine, make_session_factory
from ...orchestrator import run_to_completion, start_run
from ...registry import TaskSpec
from . import maintain

app = build_cli(
    registry,
    help="Point cadenza at its own codebase: give it a task, it opens a PR (or escalates).",
)


@app.command()
def run(
    task: str,
    concurrency: int = 1,
    repo: str = "",
    max_open_prs: int = 0,
) -> None:
    """Start a self-maintenance run for one task description and drive it
    to completion (retrying once with test-failure feedback, then
    escalating rather than looping forever - see maintain.plan_self_maintain)."""
    if repo:
        maintain.REPO_PATH = repo
    if max_open_prs:
        maintain.MAX_OPEN_PRS = max_open_prs
    asyncio.run(_run(task, concurrency))


async def _run(task: str, concurrency: int) -> None:
    engine = make_engine()
    session_factory = make_session_factory(engine)
    await create_tables(engine)

    run_id = await start_run(session_factory, task, TaskSpec(type="self_maintain", input={"task": task}))
    console.print(f"[bold]Run {run_id}[/bold] started.\nTask: {task}\nRepo: {maintain.REPO_PATH}\n")

    await run_to_completion(session_factory, registry, run_id, concurrency=concurrency)
    await print_status(session_factory, run_id)
    await engine.dispose()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
