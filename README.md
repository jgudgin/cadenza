# cadenza

A dynamic, dependency-aware multi-agent orchestration engine, demonstrated
on a financial-modelling workflow: given a company name, it drafts
assumptions, builds a 3-statement model, validates it, runs a bull/base/bear
sensitivity analysis, and exports the result to Excel with a narrative
summary - deciding what to do next after every step, rather than following
a fixed pipeline.

```
gather_assumptions ──┬─ (if incomplete) ──> gather_assumptions (retry, ≤3 rounds)
                      └─ (if complete)   ──> build_income_statement
                                               └──> build_cash_flow
                                                      └──> build_balance_sheet
                                                             └──> validate_model
                                                                    ├─ (imbalance)     ──> escalate
                                                                    ├─ (bad data)      ──> diagnose_and_fix ──> build_income_statement (replan)
                                                                    └─ (healthy) ──┬──> sensitivity_analysis (bull)   ─┐
                                                                                   ├──> sensitivity_analysis (base)   ├──> write_excel ──> summarize_for_user ──> done
                                                                                   └──> sensitivity_analysis (bear)  ─┘
```

Every arrow in that diagram is a decision made *at runtime*, by an
orchestrator inspecting what the previous step actually returned - not a
pre-declared graph. The three `sensitivity_analysis` branches run
concurrently; `write_excel` is created in the same planning step as all
three, with an explicit dependency on all of them, so it starts the moment
the last one finishes.

## Why this exists

This is a demonstration project for an AI orchestration engineer role. The
brief asked for exactly one loop, over and over:

> Agent completes task → results are assessed → next requirement is
> identified → appropriate agent/tool is deployed → process continues

That loop is the entire engine. `cadenza/orchestrator.py` is under 300
lines and *is* that loop - everything else (the finance agents, the CLI,
the API) is a workflow plugged into it.

It reuses a principle proven in a companion project,
[`cadence`](../cadence) - a scheduler for linear, crash-safe pipelines
built on one rule: **the database holds all the state, the process holds
none.** cadenza keeps that rule and removes cadence's one deliberate
restriction (steps run in a fixed sequence, never inspecting what came
before) - because dynamic orchestration is precisely the ability to look
at a result and decide what happens next.

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
avoiding the lock on one. A system with much longer-running steps would
want to split "claim" and "commit" with a lease/heartbeat instead, closer
to what Temporal does.

## How "what happens next" gets decided

Two kinds of planner, used deliberately for different situations
(`cadenza/registry.py`):

- **Rule planners** are plain functions. Most transitions in a real
  workflow aren't actually ambiguous - after the income statement, you
  always build the balance sheet next - so they cost nothing, are
  instant, and are fully unit-testable with no mocking
  (`tests/test_finance_planners.py`).
- **LLM planners** call Claude with a forced tool-use schema
  (`cadenza/llm.py::decide_next_steps`), so the decision comes back as
  structured data, not text to parse. Reserved for genuine judgement
  calls: `validate_model`'s planner is the only LLM-backed decision in
  this workflow, because "is this validation failure fixable, and what
  should happen about it" is the one place a fixed rule can't cover the
  space of real outcomes.

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

The one genuine hazard this introduces: several tasks running
concurrently (the three sensitivity scenarios) each want to write into
the run's shared context. Context updates are applied as a single atomic
`UPDATE ... SET context = context || :patch`, not a read-modify-write in
application code - so two concurrent commits can never lose one another's
write. Because Postgres's `||` on `jsonb` is a *shallow* merge, agents
that may run concurrently write disjoint top-level keys
(`sensitivity_bull`, `sensitivity_base`, `sensitivity_bear`, not a shared
`sensitivity` dict) - documented in `cadenza/agents/finance/sensitivity.py`.
A version needing a deep merge under concurrency would use `jsonb_set`
with an explicit path instead.

## Error handling

Three outcomes an agent can raise, borrowed directly from cadence because
it's the right vocabulary for any at-least-once task runner:
`Retry` (transient - back off and try again), `Permanent` (never going to
work, e.g. a missing required input), `Drop` (not a failure, just no
longer needed). Anything else raised is treated as `Retry`, because an
unexpected failure is usually transient - and a genuine dead end still
stops once `max_attempts` is exhausted.

A task that permanently fails (or exhausts its retries) doesn't just stop
- it cascades. `_block_dependents` walks the dependency graph outward
(breadth-first, so a failure several layers deep doesn't leave anything
downstream waiting forever) and marks everything that can now never
become ready as `blocked`. Nothing sits in `pending` indefinitely for a
dependency that will never satisfy.

## What's genuinely dynamic here (not staged)

The retry loop in `gather_assumptions` and the escalate/diagnose branch in
`validate_model` are driven by real model output variability, not a
scripted failure. Claude drafting a plausible-but-incomplete set of
assumptions, or one slightly out of a sane range, is a real and expected
outcome, not an injected bug - which is why `validate` re-checks
assumption bounds independently rather than only trusting the earlier
check passed (`cadenza/agents/finance/model_math.py::check_assumptions`,
shared by both agents that need it).

The three financial statements are deliberately *not* an LLM call - they
follow from the assumptions by arithmetic alone, built so the balance
sheet balances by construction (net working capital is carried as its own
line, not just netted into cash flow). `validate_model`'s balance check
should therefore always pass; it exists as a real regression guard,
checked every run, not a rehearsed failure mode. If it ever fails, that's
a bug in `model_math.py`, and the planner is instructed to say so and
escalate rather than hand it to `diagnose_and_fix` - which can only edit
assumptions, not this codebase's arithmetic.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d
export DATABASE_URL="postgresql+asyncpg://postgres:cadenza@localhost:5434/cadenza"
export ANTHROPIC_API_KEY=...        # only needed for `run`, not `status`/`trace`
cadenza create-tables
cadenza run "Some Company Inc"
cadenza status <run_id>             # task graph and outcome
cadenza trace <run_id>              # full decision log, with each planner's reasoning
```

`status` and `trace` never call an LLM - they only read what's already in
Postgres, the same reasoning as cadence's `report`/`check`: observability
should never depend on the thing it's observing being healthy.

An HTTP surface exists too (`cadenza/api.py`, `uvicorn cadenza.api:app`):
`POST /runs {"company": "..."}` kicks a run off in the background,
`GET /runs/{id}` and `GET /runs/{id}/trace` poll it - the same
`start_run`/`run_to_completion` calls the CLI uses, against the same
database.

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

`tests/test_orchestrator.py` proves the engine's guarantees with synthetic
agents (fan-out/fan-in, retry-then-succeed, exhausted-retry cascading
block, permanent-failure cascading block, crash-and-resume).
`tests/test_finance_planners.py` unit-tests the rule planners directly, no
database needed. `tests/test_finance_workflow.py` drives the *entire*
finance DAG through the real orchestrator against real Postgres, with the
two LLM call sites swapped for canned responses - so the whole thing,
including a real generated `.xlsx` file, is provable in CI with no API key
and no network call.

## What's deliberately out of scope

- **A checkpoint/lease system for very long-running tasks.** The
  one-transaction-per-task model is exactly right for LLM calls in the
  seconds range; a step that legitimately runs for hours would want
  Temporal-style leasing instead of a held row lock. Noted, not built -
  nothing in this codebase needed it.
- **A UI.** `status`/`trace` are the whole observability story, same as
  cadence's stance: scheduling and dashboards are somebody else's job to
  provide.
- **Vendor lock-in to Claude.** The only Anthropic-specific code is
  `cadenza/llm.py` (about 130 lines) - a planner is a function returning
  `PlanOutcome`, and nothing in `orchestrator.py` or `registry.py` knows
  or cares what produced one. Swapping models, or replacing the LLM
  planner with a different provider entirely, touches one file.
