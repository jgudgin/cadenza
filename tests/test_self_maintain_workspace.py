"""Real git throughout, no mocking - same philosophy as the orchestrator
tests running against real Postgres: worktree creation, path confinement,
and commit/push are exactly the kind of guardrail code worth catching
real bugs in, not the kind to fake out. `gh`-backed pieces (PR creation,
labels, counting) need real GitHub auth and aren't covered here.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from cadenza.agents.self_maintain import workspace


def _run(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An 'origin' bare repo plus a real clone with one commit on main -
    everything commit_and_push needs to have somewhere real to push to."""
    monkeypatch.setattr(workspace, "WORKTREE_ROOT", tmp_path / "worktrees")

    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)], cwd=str(tmp_path))

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _run(["git", "init", "-b", "main", "."], cwd=str(repo_path))
    _run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path))
    _run(["git", "config", "user.name", "Test"], cwd=str(repo_path))
    (repo_path / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=str(repo_path))
    _run(["git", "commit", "-m", "initial"], cwd=str(repo_path))
    _run(["git", "remote", "add", "origin", str(origin)], cwd=str(repo_path))
    _run(["git", "push", "-u", "origin", "main"], cwd=str(repo_path))

    return str(repo_path)


def test_slugify_normalizes_and_truncates():
    assert workspace.slugify("Fix the Flaky Test!!!") == "fix-the-flaky-test"
    assert len(workspace.slugify("x" * 100, max_len=10)) == 10
    assert workspace.slugify("") == "task"


def test_create_worktree_refuses_protected_branches(repo):
    with pytest.raises(ValueError):
        workspace.create_worktree(repo, "main")


def test_create_worktree_base_overrides_main(repo):
    """A project developing this agent itself on a feature branch needs
    worktrees cut from that branch, not main - otherwise every agent's
    worktree silently lacks the very code it's meant to work on."""
    _run(["git", "checkout", "-b", "feature"], cwd=repo)
    (pathlib.Path(repo) / "feature_only.txt").write_text("only on feature\n")
    _run(["git", "add", "feature_only.txt"], cwd=repo)
    _run(["git", "commit", "-m", "feature-only file"], cwd=repo)
    _run(["git", "checkout", "main"], cwd=repo)

    worktree_path = workspace.create_worktree(repo, "self-maintain/from-feature", base="feature")

    assert (pathlib.Path(worktree_path) / "feature_only.txt").exists()


def test_safe_path_confines_to_the_worktree(tmp_path):
    base = tmp_path / "wt"
    base.mkdir()

    ok = workspace.safe_path(str(base), "src/thing.py")
    assert ok == base / "src" / "thing.py"

    with pytest.raises(ValueError):
        workspace.safe_path(str(base), "../outside.py")

    with pytest.raises(ValueError):
        workspace.safe_path(str(base), "/etc/passwd")


@pytest.mark.parametrize("bad_path", [".git/config", ".git/hooks/pre-commit", ".github/workflows/ci.yml"])
def test_safe_path_denylists_git_and_github(tmp_path, bad_path):
    base = tmp_path / "wt"
    base.mkdir()
    with pytest.raises(ValueError):
        workspace.safe_path(str(base), bad_path)


def test_create_worktree_and_commit_and_push_round_trip(repo):
    worktree_path = workspace.create_worktree(repo, "self-maintain/demo-1")

    target = workspace.safe_path(worktree_path, "new_file.txt")
    target.write_text("written by the agent\n")

    workspace.commit_and_push(worktree_path, "self-maintain/demo-1", "self-maintain: add new_file.txt")

    result = subprocess.run(
        ["git", "-C", repo, "ls-remote", "origin", "refs/heads/self-maintain/demo-1"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "refs/heads/self-maintain/demo-1" in result.stdout


def test_commit_and_push_refuses_protected_branches(repo):
    with pytest.raises(ValueError):
        workspace.commit_and_push(repo, "main", "sneaky")


def test_create_worktree_recovers_from_a_stale_earlier_attempt(repo):
    """A retried task reuses the same deterministic branch name, so a
    prior attempt that created the worktree/branch and then failed later
    on (e.g. mid coding-loop) must not permanently wedge every retry."""
    branch = "self-maintain/demo-retry"
    first_worktree = workspace.create_worktree(repo, branch)
    (workspace.safe_path(first_worktree, "abandoned.txt")).write_text("never committed\n")

    second_worktree = workspace.create_worktree(repo, branch)

    assert second_worktree == first_worktree
    assert not (pathlib.Path(second_worktree) / "abandoned.txt").exists()
    assert (pathlib.Path(second_worktree) / "README.md").exists()
