"""Real git throughout, no mocking - same philosophy as the orchestrator
tests running against real Postgres: worktree creation, path confinement,
and commit/push are exactly the kind of guardrail code worth catching
real bugs in, not the kind to fake out. `gh`-backed pieces (PR creation,
labels, counting) need real GitHub auth and aren't covered here.

`run_tests` is the exception: most of its coverage below mocks
`subprocess.run` on purpose, because what actually needs proving is its
own pass/fail parsing, truncation, and timeout handling - not that
`python -m pytest` itself works. One real end-to-end invocation (against
a tiny throwaway pytest suite) backs that unit-level coverage up.
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
    worktree silently lacks the very code it's meant to work on. Pushed to
    origin, not just committed locally: create_worktree fetches and
    branches off origin/{base}, precisely so a local branch pointer that's
    behind (or, as here, a branch that only exists remotely) can't produce
    a stale worktree - see create_worktree's own docstring for why."""
    _run(["git", "checkout", "-b", "feature"], cwd=repo)
    (pathlib.Path(repo) / "feature_only.txt").write_text("only on feature\n")
    _run(["git", "add", "feature_only.txt"], cwd=repo)
    _run(["git", "commit", "-m", "feature-only file"], cwd=repo)
    _run(["git", "push", "-u", "origin", "feature"], cwd=repo)
    _run(["git", "checkout", "main"], cwd=repo)

    worktree_path = workspace.create_worktree(repo, "self-maintain/from-feature", base="feature")

    assert (pathlib.Path(worktree_path) / "feature_only.txt").exists()


def test_create_worktree_uses_the_remote_branch_even_if_local_is_stale(repo):
    """The actual bug this guards against: a long-lived local clone whose
    `main` branch pointer never advances just because origin/main did.
    `git fetch` alone never fast-forwards it - only checking it out (or an
    explicit merge/reset) does - so a worktree cut from the local pointer
    silently misses everything merged since. Simulated here without a
    second clone: advance origin's main past what the local branch knows
    about, then prove create_worktree still sees the new commit."""
    (pathlib.Path(repo) / "merged_on_origin.txt").write_text("landed after this clone went stale\n")
    _run(["git", "add", "merged_on_origin.txt"], cwd=repo)
    _run(["git", "commit", "-m", "simulates a PR merged elsewhere"], cwd=repo)
    _run(["git", "push", "origin", "main"], cwd=repo)
    _run(["git", "reset", "--hard", "HEAD~1"], cwd=repo)  # local main is now stale on purpose

    worktree_path = workspace.create_worktree(repo, "self-maintain/from-stale-local", base="main")

    assert (pathlib.Path(worktree_path) / "merged_on_origin.txt").exists()


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


# --- run_tests -------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_tests_reports_success_and_captures_output(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        captured["timeout"] = timeout
        return _FakeCompletedProcess(0, stdout="3 passed in 0.01s\n")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    ok, output = workspace.run_tests("/some/worktree")

    assert ok is True
    assert "3 passed" in output
    assert captured["cmd"] == ["python", "-m", "pytest", "-q"]
    assert captured["cwd"] == "/some/worktree"
    assert captured["env"]["PYTHONPATH"] == "/some/worktree"


def test_run_tests_reports_failure_with_combined_stdout_and_stderr(monkeypatch):
    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        return _FakeCompletedProcess(1, stdout="1 failed, 2 passed\n", stderr="a warning on stderr\n")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    ok, output = workspace.run_tests("/some/worktree")

    assert ok is False
    assert "1 failed" in output
    assert "a warning on stderr" in output


@pytest.mark.parametrize("returncode", [2, 5, 130])
def test_run_tests_treats_any_nonzero_exit_code_as_failure(monkeypatch, returncode):
    """Not just returncode == 1 - pytest also exits 2 (internal error), 5
    (no tests collected, i.e. an empty test dir), or gets killed by a
    signal; all of those are still "not passing", not a crash."""

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        return _FakeCompletedProcess(returncode, stdout="no tests ran\n")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    ok, output = workspace.run_tests("/some/worktree")

    assert ok is False
    assert "no tests ran" in output


def test_run_tests_passes_through_a_path_filter(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(0, stdout="ok")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    workspace.run_tests("/some/worktree", path_filter="tests/test_thing.py")

    assert captured["cmd"] == ["python", "-m", "pytest", "-q", "tests/test_thing.py"]


def test_run_tests_truncates_very_long_output_to_the_last_4000_chars(monkeypatch):
    huge = "x" * 10_000

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        return _FakeCompletedProcess(0, stdout=huge)

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    _, output = workspace.run_tests("/some/worktree")

    assert len(output) == 4000
    assert output == huge[-4000:]


def test_run_tests_reports_failure_and_captures_partial_output_on_timeout(monkeypatch):
    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, output="ran for a while\n", stderr="")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    ok, output = workspace.run_tests("/some/worktree", timeout=5)

    assert ok is False
    assert "timed out after 5s" in output
    assert "ran for a while" in output


def test_run_tests_missing_worktree_propagates_a_real_error():
    """No mocking here on purpose: an actually-nonexistent cwd is exactly
    the kind of misuse (a worktree that was never created, or was already
    torn down) worth confirming really surfaces as an error instead of
    silently reporting a pass."""
    with pytest.raises((FileNotFoundError, NotADirectoryError)):
        workspace.run_tests("/definitely/does/not/exist", timeout=5)


def test_run_tests_end_to_end_against_a_real_pytest_suite(tmp_path):
    """One real, fast, minimal invocation to back up the mocked coverage
    above: a two-line passing test and a two-line failing test, run for
    real through `python -m pytest`."""
    (tmp_path / "test_pass.py").write_text("def test_it():\n    assert 1 + 1 == 2\n")
    (tmp_path / "test_fail.py").write_text("def test_it():\n    assert 1 + 1 == 3\n")

    ok, output = workspace.run_tests(str(tmp_path), path_filter="test_pass.py")
    assert ok is True
    assert "1 passed" in output

    ok, output = workspace.run_tests(str(tmp_path), path_filter="test_fail.py")
    assert ok is False
    assert "1 failed" in output
