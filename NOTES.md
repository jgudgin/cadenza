# NOTES

## Design decisions

- 2026-09-15: Prioritize hardening `cadenza` (the engine) over further
  building out `cadenza-self-maintain` or `cadenza-modeler`.
  Reason: recent work here was already engine-level (ORM relationship for
  task dependencies, `GET /runs/{id}/tasks`, `AgentContext.report_progress`).
  Both downstream apps call straight into `orchestrator.py` and
  `registry.py`. A gap in the engine blocks both apps equally.
  Consequence: near-term commits concentrate in `cadenza/`.
  `cadenza-self-maintain` and `cadenza-modeler` stay as they are until the
  engine work settles.
- 2026-09-15: A failure during planning counts as a task attempt.
  Decision: `_settle_task` in `cadenza/orchestrator.py` runs the planner and
  plan application inside a savepoint within the task's transaction. When
  either raises, only the savepoint rolls back. The claim and its attempts
  increment stay. The task settles through `_settle_failure`, the same path
  handler failures use: backoff, `last_error`, an event, and failure with
  blocked dependents at `max_attempts`. Planning failures share the agent's
  `max_attempts`. A planner may raise `Retry`, `Permanent` or `Drop`, and the
  engine honors it. An unregistered task type settles as a permanent failure
  in `process_one` and `process_one_with_lease`. The retry or failure event
  records `phase` and `discarded_output`.
  Reason: a planning failure used to roll back the attempts increment. The
  task then reran with no limit, no backoff and no record.
  Consequences: the handler output is discarded, so the handler reruns on
  each planning retry and repeats any external side effects. The next entry
  covers handlers with side effects. `tests/test_orchestrator.py`,
  `test_crash_between_claim_and_commit_rolls_back_and_is_resumable` now
  cancels a task mid-handler to prove the crash rollback. The README section
  "The core guarantee" still describes the old behavior. Committed on
  2026-09-15 in 0114c27.
  Confirmed by `tools/probe_savepoint.py` on 2026-09-15 (SQLAlchemy 2.0.52,
  asyncpg 0.31.0): a savepoint rollback discards the context update, the new
  tasks and the events added inside the savepoint. The outer transaction
  still commits. The claim's attempts increment and its row lock survive the
  rollback. A database error raised when the savepoint flushes on exit also
  rolls back only the savepoint. The rollback expires every attribute of the
  task object. Reading one of them then raises `MissingGreenlet`. The settle
  code must call `session.refresh` on the task before it reads
  `task.attempts`. Cancelling `process_one` mid-handler rolls back the claim
  and releases the row lock.
  Confirmed by `tests/test_planning_failures.py` on 2026-09-15: planner
  retry with backoff, exhaustion at `max_attempts`, planner-raised
  `Permanent` and `Drop`, an unregistered task type, a bad dependency key,
  and a planner failure on the lease path.
  Unverified: an unregistered task type on the lease path, and a run object
  modified inside the savepoint.
- 2026-09-15: The engine gets no per-agent switch for planning failures.
  Decision: planning failures always go through the shared retry path.
  `cadenza-self-maintain`'s `self_maintain` handler will later become safe
  to rerun. Before it starts work, it will look for an open PR on its
  branch and return that PR. It must also handle a pushed branch that has
  no PR.
  Reason: once the handler can resume, an automatic retry does the right
  thing for it. A per-agent switch would turn temporary planning errors
  into permanent failures. The handler change also covers a process killed
  after the push, which already reruns the handler today.
  Consequences: until the handler change lands, a planning failure after
  `self_maintain` pushes can rerun the coding loop and fail the push as
  non-fast-forward. The handler change waits for the engine work, per the
  prioritization decision above.

## Known bugs

Entries marked "confirmed" were reproduced by `tools/probe_error_handling.py`
on 2026-09-15. Entries marked "unverified" come from reading the code only.

- `cadenza/orchestrator.py`, `_apply_plan`: a new task that depends on a task
  already `failed`, `blocked` or `dropped` stays `pending` forever.
  `_block_dependents` runs only when the upstream task settles.
  `run_to_completion` never returns. Confirmed: still running after 10s.
  Related (unverified): a dependency cycle inside one batch hangs the same
  way.
- `cadenza/orchestrator.py`, `run_to_completion`: `concurrency=0` starts no
  workers, and `_finalize_if_stuck` then marks the run `completed` while the
  seed task is still `pending`. `cadenza/api.py`, `create_run` and
  `cadenza/cli.py`, `start` pass the value through unchecked. Confirmed.
- `cadenza/orchestrator.py`, `process_one_with_lease` and
  `sweep_expired_leases`: the settle transaction never checks that this
  worker still owns the task. After a lease expires and a sweep resets the
  task, a second worker can run it while the first worker is still running.
  Both workers then settle, and the planner applies twice. Confirmed: one
  task ran its handler 2 times and created 2 child tasks. No function
  extends a lease, although the module calls this the lease/heartbeat model.
- `cadenza/orchestrator.py`, `_NEXT_TASK_CTE` and `_apply_plan`: neither
  checks run status. Workers keep claiming tasks of a finished run, and a
  later planner overwrites a terminal status. Confirmed: a run went from
  `completed` to `needs_review`. When one `PlanOutcome` sets both
  `run_complete` and `escalate`, `_apply_plan` silently records `completed`
  (unverified).
- `cadenza/api.py`, `list_runs`: the docstring says each write bumps
  `updated_at`. Context updates in `_apply_plan` use raw SQL, and task writes
  touch `cadenza_tasks` only. A run's `updated_at` changes only when its
  status changes. Confirmed.
- `cadenza/api.py`, `create_run`: an unregistered `seed_task_type` still
  returns 202. The seed task then fails permanently in the background, and
  `_finalize_if_stuck` should mark the run `needs_review` (unverified).
  `list_runs` does not validate `limit`. `get_tasks` and `get_trace` return
  an empty list for an unknown run, while `get_run` returns 404
  (unverified).
- `cadenza/orchestrator.py`, `_progress_reporter`: the docstring calls it
  best-effort, but a database error inside `report` propagates into the
  handler and counts as a retry (unverified).
- Dead code, confirmed by grep: `cadenza/models.py`, `Task.max_attempts` is
  never read (the orchestrator uses `AgentSpec.max_attempts`).
  `Task.reasoning` is never written. `run_to_completion` increments
  `idle_polls` and never reads it.
- `tests/test_orchestrator.py`, `test_sweep_expired_leases_recovers_stuck_task`:
  its comment says it checks that an unexpired lease is left alone, but it
  never creates a second task. `test_retry_then_succeeds` cites a NOTES.md
  quote that exists in cadence, not here.

## Open questions

- Should `_apply_plan` reject a dependency cycle, or a dependency on a task
  that already failed, before it inserts the batch? On such a plan, should
  it escalate the run or fail the task? Unregistered types and unknown keys
  already fail or retry at run time.
- Should a terminal run status be final, so that later planners and claims
  cannot change it?
- Should the lease model get an ownership check at settle (for example,
  compare `attempts` read at claim time) and a heartbeat that extends a
  lease?
- Should `run_to_completion` have a stall timeout?
- `cadenza/db.py`, `create_tables` uses `create_all`, which never alters an
  existing table. Does the engine need migrations? A migration tool would be
  a new dependency.
