"""cadenza/api.py is a real, if thin, HTTP surface over the same
orchestrator/session_factory everything else in this suite exercises -
driven through httpx's ASGI transport against real Postgres (the
`engine` fixture already creates the schema, so the app's own lifespan
doesn't need to run for these), not mocked.
"""

from __future__ import annotations

import httpx
import pytest

from cadenza.api import create_app
from cadenza.orchestrator import run_to_completion, start_run
from cadenza.registry import PlanOutcome, Registry, TaskSpec


@pytest.fixture
def registry():
    reg = Registry()

    async def do_thing(ctx):
        return {"pr_url": "https://example.com/pr/1", "summary": "did the thing"}

    async def plan_next(input):  # noqa: ANN001
        return PlanOutcome(run_complete=True)

    reg.agent("do_thing", plan_next=plan_next)(do_thing)
    return reg


@pytest.fixture
async def client(engine, registry):
    app = create_app(registry, engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_list_runs_is_most_recently_updated_first(session_factory, registry, client):
    first_started = await start_run(session_factory, "first goal", TaskSpec(type="do_thing"))
    second_started = await start_run(session_factory, "second goal", TaskSpec(type="do_thing"))

    # Complete the second-started run first, then the first-started one -
    # so despite starting earlier, first_started ends up more recently
    # updated. Proves the ordering is really updated_at, not created_at/id.
    await run_to_completion(session_factory, registry, second_started, concurrency=1)
    await run_to_completion(session_factory, registry, first_started, concurrency=1)

    resp = await client.get("/runs")
    assert resp.status_code == 200
    body = resp.json()
    ids = [r["id"] for r in body if r["id"] in (first_started, second_started)]
    assert ids.index(first_started) < ids.index(second_started)


async def test_get_run_tasks_includes_output_for_a_completed_task(session_factory, registry, client):
    run_id = await start_run(session_factory, "a goal", TaskSpec(type="do_thing"))
    await run_to_completion(session_factory, registry, run_id, concurrency=1)

    resp = await client.get(f"/runs/{run_id}/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["status"] == "completed"
    assert task["output"] == {"pr_url": "https://example.com/pr/1", "summary": "did the thing"}
    assert task["created_at"]
    assert task["updated_at"]
    assert task["lease_expires_at"] is None


async def test_get_run_returns_404_for_an_unknown_run(client):
    resp = await client.get("/runs/999999")
    assert resp.status_code == 404
