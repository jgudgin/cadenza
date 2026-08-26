"""POST /runs {"company": "..."} kicks a run off in the background and
returns immediately - the caller polls GET /runs/{id} or /runs/{id}/trace.
The orchestrator itself doesn't know or care whether it was started from
here or from the CLI; both just call start_run + run_to_completion against
the same database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from . import db
from .agents import registry
from .models import Event, Task, WorkflowRun
from .orchestrator import run_to_completion, start_run
from .registry import TaskSpec

engine = db.make_engine()
session_factory = db.make_session_factory(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.create_tables(engine)
    yield
    await engine.dispose()


app = FastAPI(title="cadenza", lifespan=lifespan)


class RunRequest(BaseModel):
    company: str
    concurrency: int = 3


class RunSummary(BaseModel):
    id: int
    goal: str
    status: str


@app.post("/runs", response_model=RunSummary, status_code=202)
async def create_run(req: RunRequest, background: BackgroundTasks) -> RunSummary:
    goal = (
        f"Build a 3-year, 3-statement financial model for {req.company}, including a "
        "bull/base/bear sensitivity table, exported to Excel with a narrative summary."
    )
    run_id = await start_run(
        session_factory, goal, TaskSpec(type="gather_assumptions", input={"company": req.company, "round": 1})
    )
    background.add_task(run_to_completion, session_factory, registry, run_id, concurrency=req.concurrency)
    return RunSummary(id=run_id, goal=goal, status="running")


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
