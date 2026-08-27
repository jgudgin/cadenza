"""Everything that touches git/the filesystem/GitHub for real. No LLM
code lives here - this module is the guardrail layer the coding agent's
tools are built on top of, and it is deliberately boring and restrictive:

- every run gets its own git worktree, on its own branch, checked out from
  `main` - never a direct edit of the working directory this session (or
  any other) actually has open, and never a commit on main itself.
- `safe_path` confines every read/write to inside that worktree, and
  denylists `.git` and `.github` specifically so an agent editing source
  can't rewrite its own guardrails or tamper with CI config.
- `run_tests` is the only way code in the worktree gets executed, and it
  always runs the same fixed pytest invocation - no arbitrary shell.
- `open_pull_request` only ever opens a PR against `main`; nothing in this
  module merges one.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

WORKTREE_ROOT = pathlib.Path(os.environ.get("CADENZA_AGENT_WORKTREE_ROOT", "/tmp/cadenza-agent-worktrees"))
DENYLISTED_PREFIXES = (".git", ".github")
PROTECTED_BRANCHES = ("main", "master")


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "task"


def create_worktree(repo_path: str, branch_name: str) -> str:
    if branch_name in PROTECTED_BRANCHES:
        raise ValueError(f"refusing to use {branch_name!r} as the agent's working branch")
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    worktree_path = WORKTREE_ROOT / branch_name.replace("/", "-")
    result = _run(
        ["git", "-C", repo_path, "worktree", "add", "-b", branch_name, str(worktree_path), "main"],
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")
    return str(worktree_path)


def safe_path(worktree_path: str, rel_path: str) -> pathlib.Path:
    """Resolve rel_path against the worktree and refuse anything that
    escapes it (via ../, symlinks, absolute paths) or touches a
    denylisted directory."""
    base = pathlib.Path(worktree_path).resolve()
    target = (base / rel_path).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes the worktree: {rel_path}")
    normalized = str(target.relative_to(base)) if target != base else ""
    if any(normalized == d or normalized.startswith(d + os.sep) for d in DENYLISTED_PREFIXES):
        raise ValueError(f"path is off-limits: {rel_path}")
    return target


def run_tests(worktree_path: str, path_filter: str = "", timeout: int = 120) -> tuple[bool, str]:
    cmd = ["python", "-m", "pytest", "-q"]
    if path_filter:
        cmd.append(path_filter)
    # PYTHONPATH first so the worktree's own copy of cadenza/ shadows
    # whatever editable install this process happens to be running under -
    # otherwise a test run here would silently exercise the wrong code.
    env = {**os.environ, "PYTHONPATH": worktree_path}
    try:
        result = subprocess.run(cmd, cwd=worktree_path, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return False, f"tests timed out after {timeout}s\n{stdout}\n{stderr}"[-4000:]
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output[-4000:]


def commit_and_push(worktree_path: str, branch_name: str, message: str) -> None:
    if branch_name in PROTECTED_BRANCHES:
        raise ValueError(f"refusing to push to {branch_name!r}")
    _run(["git", "-C", worktree_path, "add", "-A"], timeout=15)
    result = _run(["git", "-C", worktree_path, "commit", "-m", message], timeout=15)
    if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr):
        raise RuntimeError(f"git commit failed: {result.stderr}")
    result = _run(["git", "-C", worktree_path, "push", "-u", "origin", branch_name], timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git push failed: {result.stderr}")


def open_pull_request(
    worktree_path: str,
    branch_name: str,
    title: str,
    body: str,
    *,
    base: str = "main",
    labels: list[str] | None = None,
) -> str:
    if branch_name in PROTECTED_BRANCHES:
        raise ValueError(f"refusing to open a PR from {branch_name!r}")
    cmd = ["gh", "pr", "create", "--head", branch_name, "--base", base, "--title", title, "--body", body]
    for label in labels or []:
        cmd += ["--label", label]
    result = _run(cmd, cwd=worktree_path, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr}")
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return lines[-1] if lines else result.stdout.strip()


def ensure_label(repo_path: str, name: str, *, color: str = "0e8a16", description: str = "") -> None:
    """Create the label if it's missing; `--force` makes this idempotent
    (safe to call once per run) rather than needing a check-then-create."""
    _run(
        ["gh", "label", "create", name, "--color", color, "--description", description, "--force"],
        cwd=repo_path,
        timeout=15,
    )


def count_open_prs(repo_path: str, *, label: str | None = None) -> int:
    """How many PRs are currently open (optionally narrowed to `label`) -
    the mechanism a caller uses to cap how many self-maintenance PRs can
    pile up unreviewed before the agent stops opening new ones."""
    cmd = ["gh", "pr", "list", "--state", "open", "--json", "number"]
    if label:
        cmd += ["--label", label]
    result = _run(cmd, cwd=repo_path, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {result.stderr}")
    return len(json.loads(result.stdout or "[]"))
