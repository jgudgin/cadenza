"""Operations against a worktree the self-maintenance agent is allowed to
modify - kept separate from the agent's planning/LLM logic so that
"did the change actually work" is answered by really running the test
suite, not by asking a model to guess.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestResult:
    """The outcome of running the test suite in a worktree."""

    # Not a pytest test class, just named after what it represents - tell
    # pytest's collector not to treat it as one when this module (or a test
    # importing it) is collected.
    __test__ = False

    passed: bool
    returncode: int
    stdout: str
    stderr: str


def run_tests(worktree: str | Path, *, path_filter: str | None = None) -> TestResult:
    """Run pytest inside `worktree` and report whether it passed.

    This shells out to a real `pytest` subprocess (rather than reimplementing
    pass/fail detection) so the result reflects exactly what a human running
    the suite by hand would see: a real return code and real captured
    output. `path_filter`, if given, is passed straight through to pytest to
    scope the run (e.g. to a single test file or directory).
    """
    cmd = ["pytest"]
    if path_filter:
        cmd.append(path_filter)

    proc = subprocess.run(
        cmd,
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    return TestResult(
        passed=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
