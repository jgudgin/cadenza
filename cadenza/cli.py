"""Generic CLI commands, reusable by any project built on cadenza.

`build_cli(registry)` returns a Typer app with the domain-agnostic
commands every project needs (`start`, `status`, `trace`,
`create-tables`); a project adds its own commands on top of the returned
app for anything that needs to know its own task types (e.g. a `run`
command that seeds a specific first task with a friendly argument, the
way the finance demo's `cadenza-model run "Some Company"` does).

`status`/`trace` never touch an LLM - they only read what's already in
Postgres, so they work with no API key set at all, regardless of what a
project's agents need.
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from . import db
from .models import Event, Task, WorkflowRun
from .orchestrator import run_to_completion, start_run
from .registry import Registry, TaskSpec

console = Console()


def build_cli(registry: Registry, *, help: str = "Dynamic multi-agent orchestration.") -> typer.Typer:
    app = typer.Typer(add_completion=False, help=help)

    @app.command()
    def start(goal: str, seed_type: str, seed_input: str = "{}", concurrency: int = 3) -> None:
        """Start a run from a raw seed task type/input (JSON). Most
        projects will add their own friendlier `run` command on top of
        this app instead of asking users to hand-write JSON."""
        asyncio.run(_start(goal, seed_type, json.loads(seed_input), concurrency))

    async def _start(goal: str, seed_type: str, seed_input: dict, concurrency: int) -> None:
        engine = db.make_engine()
        session_factory = db.make_session_factory(engine)
        await db.create_tables(engine)

        run_id = await start_run(session_factory, goal, TaskSpec(type=seed_type, input=seed_input))
        console.print(f"[bold]Run {run_id}[/bold] started.\nGoal: {goal}\n")

        await run_to_completion(session_factory, registry, run_id, concurrency=concurrency)
        await print_status(session_factory, run_id)
        await engine.dispose()

    @app.command()
    def status(run_id: int) -> None:
        """Show a run's tasks and current outcome."""
        asyncio.run(_status(run_id))

    @app.command()
    def trace(run_id: int) -> None:
        """Print the full decision log for a run, in order - the
        observability layer: every dispatch, every planner decision and
        its reasoning, every failure, one line each."""
        asyncio.run(_trace(run_id))

    @app.command(name="create-tables")
    def create_tables_cmd() -> None:
        """One-time schema setup against DATABASE_URL."""
        asyncio.run(_create_tables())

    return app


async def _status(run_id: int) -> None:
    engine = db.make_engine()
    session_factory = db.make_session_factory(engine)
    await print_status(session_factory, run_id)
    await engine.dispose()


async def print_status(session_factory, run_id: int) -> None:  # noqa: ANN001
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
    for col in ("id", "type", "status", "attempts", "created by", "error"):
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

    if run.context:
        console.print(f"\n[dim]Context keys: {', '.join(sorted(run.context))}[/dim]")


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


async def _create_tables() -> None:
    engine = db.make_engine()
    await db.create_tables(engine)
    await engine.dispose()
    console.print("[green]Tables created.[/green]")
