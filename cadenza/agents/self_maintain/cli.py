"""`cadenza-self-maintain run "some task"` - the friendly entry point on
top of the generic engine CLI, the same pattern cadenza-modeler's `run`
command uses on top of `build_cli` (see README's "Building a project on
this"): seed one `self_maintain` task, drive it to completion, print
what happened.

`plan "a rough idea"` is the multi-agent version: clarify the idea
interactively, split it into independent subtasks, and fan out to one
`self_maintain` task per subtask - each becomes its own PR, from its own
coding agent, in its own branch, running concurrently.
"""

from __future__ import annotations

import asyncio

import typer
from sqlalchemy import select

from .. import registry  # importing this loads .env - see cadenza/agents/__init__.py
from ...cli import build_cli, console, print_status
from ...db import create_tables, make_engine, make_session_factory
from ...models import Task
from ...orchestrator import run_to_completion, start_run
from ...registry import TaskSpec
from . import maintain
from .clarify import clarify_interactively

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


@app.command()
def plan(
    rough_idea: str,
    concurrency: int = 3,
    repo: str = "",
    max_open_prs: int = 0,
) -> None:
    """Clarify a rough idea interactively, split it into independent
    subtasks, and fan out to one self-maintenance agent per subtask,
    running concurrently - each ends in its own PR (or escalation)."""
    if repo:
        maintain.REPO_PATH = repo
    if max_open_prs:
        maintain.MAX_OPEN_PRS = max_open_prs
    asyncio.run(_plan(rough_idea, concurrency))


async def _plan(rough_idea: str, concurrency: int) -> None:
    def ask(question: str) -> str:
        return typer.prompt(question)

    brief = await clarify_interactively(rough_idea, ask=ask)
    if brief != rough_idea:
        console.print(f"\n[dim]Refined brief:[/dim] {brief}\n")

    engine = make_engine()
    session_factory = make_session_factory(engine)
    await create_tables(engine)

    run_id = await start_run(session_factory, rough_idea, TaskSpec(type="plan_maintenance", input={"brief": brief}))
    console.print(f"[bold]Run {run_id}[/bold] started.\nRepo: {maintain.REPO_PATH}\n")

    await run_to_completion(session_factory, registry, run_id, concurrency=concurrency)
    await print_subtask_outcomes(session_factory, run_id)
    await engine.dispose()


async def print_subtask_outcomes(session_factory, run_id: int) -> None:  # noqa: ANN001
    """Each fanned-out self_maintain sibling decides run_complete/escalate
    for itself, with no join task to wait for all of them - so run.status
    reflects whichever one finished last, not the aggregate. Report the
    real per-subtask outcomes instead of trusting it."""
    async with session_factory() as session:
        result = await session.execute(
            select(Task).where(Task.run_id == run_id, Task.type == "self_maintain").order_by(Task.id)
        )
        subtasks = result.scalars().all()

    if not subtasks:
        console.print("[yellow]No subtasks were created (the decomposition step may have escalated).[/yellow]")
        return

    console.print(f"[bold]{len(subtasks)} subtask(s):[/bold]")
    for t in subtasks:
        output = t.output or {}
        if output.get("pr_url"):
            console.print(f"  [green]done[/green]   task {t.id}: {output['pr_url']}")
        elif t.status == "failed":
            console.print(f"  [red]failed[/red] task {t.id}: {(t.last_error or '')[:100]}")
        else:
            reason = output.get("reason") or output.get("summary") or t.status
            console.print(f"  [yellow]{t.status:<8}[/yellow] task {t.id}: {reason[:100]}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
