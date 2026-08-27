"""A small, tightly bounded coding agent: given a task and a worktree,
lets Claude read files, write files, list files, and run the real test
suite - confined entirely to that worktree via `workspace.safe_path`, no
shell access, nothing that can touch `.git` or `.github`. Terminates when
the model calls `finish`, or after a fixed number of tool-call turns,
whichever comes first - and the *authoritative* pass/fail comes from this
module running the tests itself afterward, not from trusting whatever the
model last reported.
"""

from __future__ import annotations

import asyncio
import pathlib
from functools import lru_cache
from typing import Any

import anthropic

from . import workspace

MODEL = "claude-sonnet-5"
MAX_TURNS = 20
MAX_FILE_BYTES = 200_000
# claude-sonnet-5 emits extended-thinking content by default, and it counts
# against max_tokens like everything else: on a hard turn the model can burn
# the whole budget on thinking and hit stop_reason="max_tokens" before
# producing a single tool_use or word of text - a silent, totally opaque
# failure (see the "model stopped without calling a tool" case below).
# Generous headroom, not 4096.
MAX_TOKENS = 16_000

_TOOLS = [
    {
        "name": "list_files",
        "description": "List files in the worktree matching a glob pattern, e.g. 'cadenza/**/*.py'.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file's full contents. Path is relative to the worktree root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Overwrite (or create) a file with the given full contents. Path is relative to the worktree root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run the real pytest suite (optionally scoped to a path) against the worktree's current state.",
        "input_schema": {
            "type": "object",
            "properties": {"path_filter": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "finish",
        "description": "Call this when the task is complete and you want to stop.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


@lru_cache(maxsize=1)
def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic()


def _execute_tool_sync(worktree_path: str, name: str, tool_input: dict[str, Any]) -> str:
    if name == "list_files":
        base = pathlib.Path(worktree_path)
        matches = sorted(
            str(p.relative_to(base)) for p in base.glob(tool_input["pattern"]) if ".git" not in p.parts
        )
        return "\n".join(matches) or "(no matches)"
    if name == "read_file":
        path = workspace.safe_path(worktree_path, tool_input["path"])
        if not path.exists():
            return f"ERROR: no such file: {tool_input['path']}"
        data = path.read_bytes()[:MAX_FILE_BYTES]
        return data.decode("utf-8", errors="replace")
    if name == "write_file":
        path = workspace.safe_path(worktree_path, tool_input["path"])
        content = tool_input["content"]
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return "ERROR: file too large"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"wrote {len(content)} bytes to {tool_input['path']}"
    if name == "run_tests":
        passed, output = workspace.run_tests(worktree_path, tool_input.get("path_filter", ""))
        return f"{'PASSED' if passed else 'FAILED'}\n{output}"
    raise ValueError(f"unknown tool: {name}")


async def run_coding_loop(*, worktree_path: str, task: str, previous_failure: str | None) -> dict:
    system = (
        "You are editing a real codebase in an isolated git worktree, on its own "
        "branch. You have list_files, read_file, write_file, and run_tests - no "
        "shell access, nothing outside this worktree, and .git/.github are "
        "off-limits (attempts to touch them are rejected). Make the smallest "
        "change that accomplishes the task. Run the tests yourself at least once "
        "before finishing, and call `finish` with a short summary when done. "
        f"You have at most {MAX_TURNS} tool calls total."
    )
    user_msg = f"Task: {task}"
    if previous_failure:
        user_msg += f"\n\nA previous attempt's tests failed with this output:\n{previous_failure}"

    messages: list[dict] = [{"role": "user", "content": user_msg}]
    changed_files: set[str] = set()
    summary = ""

    for _ in range(MAX_TURNS):
        response = await _client().messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system, tools=_TOOLS, messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            # Treat as done, but keep whatever the model said instead of
            # calling a tool - silently discarding it left a run that
            # bailed after one text-only turn indistinguishable from one
            # that made real progress and then stopped cleanly.
            text = " ".join(b.text for b in response.content if b.type == "text").strip()
            summary = text or "(model stopped without calling a tool or explaining why)"
            break

        tool_results = []
        finished = False
        for block in tool_uses:
            if block.name == "finish":
                summary = block.input.get("summary", "")
                finished = True
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "ok"})
                continue
            if block.name == "write_file":
                changed_files.add(block.input["path"])
            try:
                result_text = await asyncio.to_thread(_execute_tool_sync, worktree_path, block.name, block.input)
            except Exception as exc:
                result_text = f"ERROR: {exc}"
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})

        messages.append({"role": "user", "content": tool_results})
        if finished:
            break

    passed, test_output = await asyncio.to_thread(workspace.run_tests, worktree_path)
    return {
        "changed_files": sorted(changed_files),
        "summary": summary or f"(used all {MAX_TURNS} tool-call turns without calling finish)",
        "tests_passed": passed,
        "test_output": test_output,
    }
