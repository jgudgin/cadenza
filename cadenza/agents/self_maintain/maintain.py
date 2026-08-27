"""Wires the bounded coding loop (`tools.run_coding_loop`) and the git/
GitHub guardrail layer (`workspace`) into a real cadenza agent: given a
task describing something that needs fixing, spin up an isolated
worktree, let the coding loop attempt it, and - only if it leaves the
real test suite passing - push a branch and open a PR against `main`.
Nothing here ever merges a PR; a human reviews and merges every one it
opens.

The retry policy lives in the planner, not in exception handling: a
failed test run is a normal *outcome* to reason about (worth one retry
with the failure output as feedback), not an infrastructure failure like
a worktree that couldn't be created. `Retry` stays reserved for the
latter, matching how `validate_model` in the finance workflow used
`Permanent` only for its own bugs, never for a bad-but-legible result.
"""

from __future__ import annotations

import asyncio
import os
import pathlib

from .. import registry
from ...exceptions import Retry
from ...registry import AgentContext, PlanOutcome, TaskSpec
from . import workspace
from .tools import run_coding_loop

REPO_PATH = os.environ.get("CADENZA_SELF_MAINTAIN_REPO", str(pathlib.Path.cwd()))
MAX_OPEN_PRS = int(os.environ.get("CADENZA_SELF_MAINTAIN_MAX_OPEN_PRS", "5"))
# What every agent's worktree is cut from - almost always "main", the only
# thing open_pull_request ever targets as a base too. Override when the
# code being worked on only exists on a feature branch that hasn't reached
# main yet (as cadenza's own self-maintenance agent code does right now,
# on "self-maintainer") - otherwise every worktree silently won't have it.
BASE_BRANCH = os.environ.get("CADENZA_SELF_MAINTAIN_BASE_BRANCH", "main")
LABEL = "self-maintain"


async def self_maintain(ctx: AgentContext) -> dict:
    task = ctx.input["task"]
    previous_failure = ctx.input.get("previous_failure")

    open_prs = await asyncio.to_thread(workspace.count_open_prs, REPO_PATH, label=LABEL)
    if open_prs >= MAX_OPEN_PRS:
        return {"skipped": True, "reason": f"{open_prs} self-maintenance PRs already open (cap {MAX_OPEN_PRS})"}

    branch_name = f"self-maintain/{workspace.slugify(task)}-{ctx.task_id}"
    try:
        worktree_path = await asyncio.to_thread(
            workspace.create_worktree, REPO_PATH, branch_name, base=BASE_BRANCH
        )
    except Exception as exc:
        raise Retry(f"could not create worktree: {exc}") from exc

    result = await run_coding_loop(worktree_path=worktree_path, task=task, previous_failure=previous_failure)
    if not result["tests_passed"] or not result["changed_files"]:
        return result

    # Independent of each other - one labels the repo, the other pushes the
    # branch - only open_pull_request below needs both done.
    await asyncio.gather(
        asyncio.to_thread(workspace.ensure_label, REPO_PATH, LABEL, description="Opened by the self-maintenance agent"),
        asyncio.to_thread(
            workspace.commit_and_push, worktree_path, branch_name, f"self-maintain: {result['summary'][:72]}"
        ),
    )
    pr_url = await asyncio.to_thread(
        workspace.open_pull_request,
        worktree_path,
        branch_name,
        title=f"self-maintain: {task[:60]}",
        body=result["summary"],
        base=BASE_BRANCH,
        labels=[LABEL],
    )
    return {**result, "pr_url": pr_url}


async def plan_self_maintain(input) -> PlanOutcome:  # noqa: ANN001
    output = input.output

    if output.get("skipped"):
        return PlanOutcome(escalate=True, reasoning=output["reason"])

    if output.get("pr_url"):
        return PlanOutcome(run_complete=True, reasoning=f"opened {output['pr_url']}")

    if not output.get("tests_passed", False):
        already_retried = bool(input.completed_task_input.get("previous_failure"))
        if already_retried:
            return PlanOutcome(
                escalate=True,
                reasoning="tests still failing after a retry with the failure output as feedback",
            )
        return PlanOutcome(
            tasks=[
                TaskSpec(
                    type="self_maintain",
                    input={**input.completed_task_input, "previous_failure": output.get("test_output", "")},
                )
            ],
            reasoning="tests failed; retrying once, this time with the failure output as feedback",
        )

    return PlanOutcome(escalate=True, reasoning="coding agent made no changes and reported no failing tests")


registry.agent("self_maintain", plan_next=plan_self_maintain)(self_maintain)
