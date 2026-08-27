"""Generic HTTP surface, reusable by any project built on cadenza.

`create_app(registry)` builds a FastAPI app with the domain-agnostic
routes every project needs. `POST /runs` takes a raw goal/seed task, the
same way `cadenza start` does on the CLI side - a project wanting a
friendlier endpoint (e.g. `POST /models {"company": "..."}`) mounts its
own route on the same app, alongside these.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from . import db
from .models import Event, Task, WorkflowRun
from .orchestrator import run_to_completion, start_run
from .registry import Registry, TaskSpec


class RunRequest(BaseModel):
    goal: str
    seed_task_type: str
    seed_task_input: dict = {}
    concurrency: int = 3


class RunSummary(BaseModel):
    id: int
    goal: str
    status: str


def create_app(registry: Registry, *, title: str = "cadenza", engine: AsyncEngine | None = None) -> FastAPI:
    """Pass `engine` when a project is adding its own routes that need the
    same session factory (e.g. a friendlier `POST /models`) - otherwise
    this builds and owns one itself."""
    engine = engine or db.make_engine()
    session_factory = db.make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.create_tables(engine)
        yield
        await engine.dispose()

    app = FastAPI(title=title, lifespan=lifespan)

    @app.post("/runs", response_model=RunSummary, status_code=202)
    async def create_run(req: RunRequest, background: BackgroundTasks) -> RunSummary:
        run_id = await start_run(
            session_factory, req.goal, TaskSpec(type=req.seed_task_type, input=req.seed_task_input)
        )
        background.add_task(run_to_completion, session_factory, registry, run_id, concurrency=req.concurrency)
        return RunSummary(id=run_id, goal=req.goal, status="running")

    @app.get("/runs/{run_id}", response_model=RunSummary)
    async def get_run(run_id: int) -> RunSummary:
        async with session_factory() as session:
            run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        return RunSummary(id=run.id, goal=run.goal, status=run.status)

    @app.get("/runs/{run_id}/tasks")
    async def get_tasks(run_id: int) -> list[dict]:
        async with session_factory() as session:
            result = await session.execute(select(Task).where(Task.run_id == run_id).order_by(Task.id))
            tasks = result.scalars().all()
        return [
            {
                "id": t.id,
                "type": t.type,
                "status": t.status,
                "attempts": t.attempts,
                "created_by_task_id": t.created_by_task_id,
                "last_error": t.last_error,
            }
            for t in tasks
        ]

    @app.get("/runs/{run_id}/trace")
    async def get_trace(run_id: int) -> list[dict]:
        async with session_factory() as session:
            result = await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.id))
            events = result.scalars().all()
        return [
            {
                "id": e.id,
                "task_id": e.task_id,
                "type": e.type,
                "payload": e.payload,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]

    return app
