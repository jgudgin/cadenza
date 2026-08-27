"""Direct unit coverage for `workspace.run_tests`.

Two real-pytest-subprocess cases (against a throwaway worktree directory,
the same "no mocking the thing being proven" philosophy as the rest of
the suite) prove the actual pass/fail detection works, plus one mocked
case that pins down exactly how pytest is invoked without spawning a
second real process for every assertion.
"""

from __future__ import annotations

from unittest.mock import patch

from cadenza.agents.self_maintain.workspace import TestResult, run_tests

PASSING_TEST = """
def test_ok():
    assert 1 + 1 == 2
"""

FAILING_TEST = """
def test_not_ok():
    assert 1 + 1 == 3
"""


def _make_worktree(tmp_path, contents: str):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "test_example.py").write_text(contents)
    return worktree


def test_run_tests_reports_pass_for_a_passing_suite(tmp_path):
    worktree = _make_worktree(tmp_path, PASSING_TEST)

    result = run_tests(worktree)

    assert isinstance(result, TestResult)
    assert result.passed is True
    assert result.returncode == 0
    assert "1 passed" in result.stdout


def test_run_tests_reports_failure_for_a_failing_suite(tmp_path):
    worktree = _make_worktree(tmp_path, FAILING_TEST)

    result = run_tests(worktree)

    assert result.passed is False
    assert result.returncode != 0
    assert "1 failed" in result.stdout


def test_run_tests_scopes_to_path_filter(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "test_good.py").write_text(PASSING_TEST)
    (worktree / "test_bad.py").write_text(FAILING_TEST)

    result = run_tests(worktree, path_filter="test_good.py")

    assert result.passed is True
    assert result.returncode == 0


def test_run_tests_invokes_pytest_as_a_subprocess_in_the_worktree(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    fake_completed = type(
        "FakeCompletedProcess",
        (),
        {"returncode": 0, "stdout": "1 passed", "stderr": ""},
    )()

    with patch("cadenza.agents.self_maintain.workspace.subprocess.run", return_value=fake_completed) as mock_run:
        result = run_tests(worktree, path_filter="tests/test_foo.py")

    mock_run.assert_called_once_with(
        ["pytest", "tests/test_foo.py"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    assert result == TestResult(passed=True, returncode=0, stdout="1 passed", stderr="")
