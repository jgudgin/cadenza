"""cadenza run "Some Company"      - build a model end-to-end
cadenza status <run_id>          - current task graph and outcome
cadenza trace <run_id>           - the full decision log, in order
cadenza create-tables            - one-time schema setup

status and trace never touch the LLM - they only read what's already in
Postgres, so they work with no ANTHROPIC_API_KEY set at all.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from . import db
from .agents import registry
from .models import Event, Task, WorkflowRun
from .orchestrator import run_to_completion, start_run
from .registry import TaskSpec

app = typer.Typer(add_completion=False, help="Dynamic multi-agent orchestration, demonstrated on financial modelling.")
console = Console()


@app.command()
def run(company: str, concurrency: int = 3) -> None:
    """Build a 3-statement model for COMPANY end-to-end."""
    asyncio.run(_run(company, concurrency))


async def _run(company: str, concurrency: int) -> None:
    engine = db.make_engine()
    session_factory = db.make_session_factory(engine)
    await db.create_tables(engine)

    goal = (
        f"Build a 3-year, 3-statement financial model for {company}, including a "
        "bull/base/bear sensitivity table, exported to Excel with a narrative summary."
    )
    run_id = await start_run(
        session_factory, goal, TaskSpec(type="gather_assumptions", input={"company": company, "round": 1})
    )
    console.print(f"[bold]Run {run_id}[/bold] started.\nGoal: {goal}\n")

    await run_to_completion(session_factory, registry, run_id, concurrency=concurrency)
    await _print_status(session_factory, run_id)
    await engine.dispose()


@app.command()
def status(run_id: int) -> None:
    """Show a run's tasks and current outcome."""
    asyncio.run(_status(run_id))


async def _status(run_id: int) -> None:
    engine = db.make_engine()
    session_factory = db.make_session_factory(engine)
    await _print_status(session_factory, run_id)
    await engine.dispose()


async def _print_status(session_factory, run_id: int) -> None:  # noqa: ANN001
    async with session_factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            console.print(f"[red]No such run: {run_id}[/red]")
            return
        result = await session.execute(select(Task).where(Task.run_id == run_id).order_by(Task.id))
        tasks = result.scalars().all()

    status_color = {"completed": "green", "failed": "red", "needs_review": "yellow"}.get(run.status, "cyan")
    console.print(f"\n[bold]Run {run.id}[/bold] - status: [{status_color}]{run.status}[/{status_color}]")
    console.print(f"Goal: {run.goal}\n")

    table = Table(show_header=True, header_style="bold")
    for col in ("id", "type", "status", "attempts", "depends via", "error"):
        table.add_column(col)
    for t in tasks:
        table.add_row(
            str(t.id),
            t.type,
            t.status,
            str(t.attempts),
            str(t.created_by_task_id or "-"),
            (t.last_error or "")[:60],
        )
    console.print(table)

    if run.context.get("excel_path"):
        console.print(f"\n[green]Workbook:[/green] {run.context['excel_path']}")
    if run.context.get("summary"):
        console.print(f"\n[bold]Summary:[/bold]\n{run.context['summary']}")


@app.command()
def trace(run_id: int) -> None:
    """Print the full decision log for a run, in order - the observability
    layer: every dispatch, every planner decision and its reasoning, every
    failure, one line each."""
    asyncio.run(_trace(run_id))


async def _trace(run_id: int) -> None:
    engine = db.make_engine()
    session_factory = db.make_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.id))
        events = result.scalars().all()
    for e in events:
        console.print(
            f"[dim]{e.created_at:%H:%M:%S}[/dim] [bold]{e.type:<24}[/bold] "
            f"task={e.task_id or '-':<5} {_format_payload(e.payload)}"
        )
    await engine.dispose()


def _format_payload(payload: dict) -> str:
    reasoning = payload.get("reasoning")
    if reasoning:
        return f"- {reasoning}"
    return str(payload)[:140]


@app.command(name="create-tables")
def create_tables_cmd() -> None:
    """One-time schema setup against DATABASE_URL."""
    asyncio.run(_create_tables())


async def _create_tables() -> None:
    engine = db.make_engine()
    await db.create_tables(engine)
    await engine.dispose()
    console.print("[green]Tables created.[/green]")


if __name__ == "__main__":
    app()
