# cadenza

A dynamic, dependency-aware multi-agent orchestration engine: an agent
completes a task, a planner assesses the result and decides what needs to
happen next, the appropriate agent or tool is dispatched, the process
continues - all crash-safe in Postgres, with no in-memory state that a
crash could lose.

This repo is the engine only. It has no idea what task it's orchestrating
- no fixed workflow, nothing domain-specific at all. Two complete real
examples built on it:

- [`cadenza-modeler`](https://github.com/jgudgin/cadenza-modeler) - a
  financial-modelling workflow with an LLM-drafted-assumptions loop, a
  validation step that dynamically fans out to concurrent scenarios and
  fans back in to an Excel export, and a narrative summary.
- [`cadenza-self-maintain`](https://github.com/jgudgin/cadenza-self-maintain) -
  a bounded coding agent that points at any git repo, edits code in an
  isolated worktree, and opens a PR only once the real test suite passes;
  a "boss agent" variant clarifies a rough request and fans it out to
  concurrent subtasks. (Originally built and proven directly in this
  repo, on itself, before being extracted for the same reason
  `cadenza-modeler` was.)

## Why this exists

The brief this was built to demonstrate asked for exactly one loop, over
and over:

> Agent completes task → results are assessed → next requirement is
> identified → appropriate agent/tool is deployed → process continues

That loop is the entire engine. `cadenza/orchestrator.py` is under 300
lines and *is* that loop - a project plugs in agents (`registry.py`) and
gets crash safety, retries, dependency resolution, concurrency, and
observability for free.

It reuses a principle proven in a companion project,
[`cadence`](https://github.com/jgudgin/cadence) - a scheduler for linear,
crash-safe pipelines built on one rule: **the database holds all the
state, the process holds none.** cadenza keeps that rule and removes
cadence's one deliberate restriction (steps run in a fixed sequence, never
inspecting what came before) - because dynamic orchestration is precisely
the ability to look at a result and decide what happens next.

## The core guarantee: one task, one transaction

Claim a task, run its handler, decide what happens next, write the new
tasks and the status change - all in a single database transaction, one
commit. If the process dies at any point before that commit (a crashed
container, a killed process, an unhandled bug in a planner), the *entire*
transaction rolls back: the task reverts to exactly the state it was in
before it was claimed, including its attempt counter. No status is ever
half-updated; no plan is ever half-applied. A fresh worker - possibly a
different process, remembering nothing - claims it again and it is as if
nothing happened.

This is proven, not asserted: see
[`test_crash_between_claim_and_commit_rolls_back_and_is_resumable`](tests/test_orchestrator.py).
It injects a genuine bug into a planner (an unhandled `RuntimeError`, not
one of the handled failure types), lets it crash mid-transaction, confirms
the task is back to `pending` with `attempts=0`, then re-runs it
successfully - proving recovery, not just rollback.

The trade-off is explicit, not hidden: holding a transaction open for the
duration of an LLM call means a row lock for however long that call takes
(seconds, typically). That's fine at this scale, because different tasks
are different rows and `SKIP LOCKED` means workers never contend over the
*same* row - concurrency comes from parallelism across rows, not from
avoiding the lock on one.

For the rare task type that legitimately runs for hours instead of
seconds, `cadenza/orchestrator.py` also offers an opt-in alternative that
splits "claim" and "commit" with a lease instead of one held transaction:
`claim_with_lease` claims the task and commits immediately, releasing the
row lock; the handler then runs with no transaction open at all; a second
short transaction records the result exactly like `process_one`'s tail
does. The cost is a real one - if the worker crashes mid-handler, nothing
rolls it back automatically, so `sweep_expired_leases` has to be run
periodically to notice a lease has passed and reset the task to `pending`
- but that's the correct trade for not pinning a lock for hours. This is
purely additive: `process_one` / `run_to_completion` are unchanged, and a
project picks whichever model fits, per task type. See
[`test_lease_claim_releases_row_lock_before_handler_finishes`](tests/test_orchestrator.py)
and
[`test_sweep_expired_leases_recovers_stuck_task`](tests/test_orchestrator.py).

## How "what happens next" gets decided

Two kinds of planner, used deliberately for different situations
(`cadenza/registry.py`):

- **Rule planners** are plain functions. Most transitions in a real
  workflow aren't actually ambiguous, so they cost nothing, are instant,
  and are fully unit-testable with no mocking.
- **LLM planners** call a model with a forced tool-use schema
  (`cadenza/llm.py::decide_next_steps`), so the decision comes back as
  structured data, not text to parse. Reserved for genuine judgement
  calls - the kind of decision a fixed rule can't cover the space of real
  outcomes for.

Both kinds return the same `PlanOutcome`: new tasks to create (each with
an optional `key` and `depends_on`, so a single planning call can fan out
to several siblings *and* create a join task that depends on all of them,
none of which have real database ids yet), facts to merge into the run's
shared context, and one of three terminal signals - keep going,
`run_complete`, or `escalate`. Completion and escalation are deliberately
distinct: "finished successfully" and "stuck, a human should look" must
never collapse into the same status.

## Concurrency and shared state

Multiple workers pull from the same ready queue via a single SQL query
(`cadenza/orchestrator.py::_CLAIM_SQL`):

```sql
UPDATE cadenza_tasks SET status = 'running', attempts = attempts + 1
FROM (
    SELECT id FROM cadenza_tasks
    WHERE status = 'pending' AND (backoff has elapsed)
      AND NOT EXISTS (any dependency not yet completed)
    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
) AS next_task
WHERE cadenza_tasks.id = next_task.id
RETURNING cadenza_tasks.*;
```

That single query *is* "understands dependencies and determines what
needs to happen next" - not a scheduler loop reasoning about a graph in
memory, a claim that is correct exactly because Postgres guarantees it
atomically.

Tasks running concurrently can write into a run's shared context at the
same time. Context updates are applied as a single atomic
`UPDATE ... SET context = context || :patch`, not a read-modify-write in
application code - so two concurrent commits can never lose one another's
write. Because Postgres's `||` on `jsonb` is a *shallow* merge, agents
that may run concurrently need to write disjoint top-level keys - a
version needing a deep merge under concurrency would use `jsonb_set` with
an explicit path instead.

## Error handling

Three outcomes an agent can raise (`cadenza/exceptions.py`), the right
vocabulary for any at-least-once task runner: `Retry` (transient - back
off and try again), `Permanent` (never going to work, e.g. a missing
required input), `Drop` (not a failure, just no longer needed). Anything
else raised is treated as `Retry`, because an unexpected failure is
usually transient - and a genuine dead end still stops once
`max_attempts` is exhausted.

A task that permanently fails (or exhausts its retries) doesn't just stop
- it cascades. `_block_dependents` walks the dependency graph outward
(breadth-first, so a failure several layers deep doesn't leave anything
downstream waiting forever) and marks everything that can now never
become ready as `blocked`. Nothing sits in `pending` indefinitely for a
dependency that will never satisfy.

## Building a project on this

A project is: agent handlers (`AgentContext -> dict`), a `plan_next`
function per agent type (`PlannerInput -> PlanOutcome`), registered
against a `Registry()` instance, plus a thin CLI/API layer built from the
reusable pieces this repo provides:

```python
# myproject/agents.py
from cadenza.registry import Registry

registry = Registry()

@registry.agent("do_thing", plan_next=my_planner)
async def do_thing(ctx):
    ...
```

```python
# myproject/cli.py
from cadenza.cli import build_cli
from .agents import registry

app = build_cli(registry, help="My project.")

@app.command()
def run(some_friendly_arg: str) -> None:
    """A project-specific entry point on top of the generic CLI - see
    cadenza-modeler's `run` command for a full example."""
```

`cadenza.api.create_app(registry)` does the equivalent for a FastAPI
surface. Neither `build_cli` nor `create_app` know or care what agents
are registered - see [`cadenza-modeler`](https://github.com/jgudgin/cadenza-modeler)
for both used for real.

```bash
pip install -e ".[anthropic]"       # only needed if any agent uses cadenza.llm
docker compose up -d
export DATABASE_URL="postgresql+asyncpg://postgres:cadenza@localhost:5434/cadenza"
export ANTHROPIC_API_KEY=...        # only if using cadenza.llm's LLM planner
```

## Tests

```bash
pip install -e ".[dev]"
docker compose up -d
export DATABASE_URL="postgresql+asyncpg://postgres:cadenza@localhost:5434/cadenza"
pytest -v
```

Each test gets its own throwaway Postgres schema (`tests/conftest.py`),
against real Postgres - no mocking the database, because `SKIP LOCKED`,
the dependency `NOT EXISTS` check, and the `jsonb` merge are exactly the
things worth catching only-against-the-real-thing bugs in.
`tests/test_orchestrator.py` proves the engine's guarantees with
synthetic agents: fan-out/fan-in, retry-then-succeed, exhausted-retry
cascading block, permanent-failure cascading block, the crash-and-resume
proof, and the opt-in lease model's row-lock-release and lease-recovery
proofs.

## What's deliberately out of scope

- **A UI.** `build_cli`'s `status`/`trace` commands are the whole
  observability story - scheduling and dashboards are a project's own
  job to provide on top, if it wants one.
- **Vendor lock-in to any LLM provider.** The only model-specific code is
  `cadenza/llm.py` (about 130 lines, currently Anthropic, gated behind an
  optional `anthropic` extra so nothing else in the engine depends on
  it) - a planner is a function returning `PlanOutcome`, and nothing in
  `orchestrator.py` or `registry.py` knows or cares what produced one.
