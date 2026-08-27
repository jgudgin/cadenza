"""Direct, isolated unit tests for `print_subtask_outcomes`: no Postgres,
no orchestrator, no LLM - just checking that a list of subtask outcomes
is rendered to stdout exactly as expected, for every shape of input a
real self-maintenance run could produce (mixed, all-success, all-failure,
empty).
"""

from __future__ import annotations

from cadenza.agents.self_maintain.cli import SubtaskOutcome, print_subtask_outcomes


def test_prints_mixed_success_and_failure(capsys):
    outcomes = [
        SubtaskOutcome(name="lint", success=True),
        SubtaskOutcome(name="tests", success=False, detail="2 failures"),
        SubtaskOutcome(name="typecheck", success=True),
    ]

    print_subtask_outcomes(outcomes)

    captured = capsys.readouterr()
    assert captured.out == (
        "[OK] lint\n"
        "[FAILED] tests: 2 failures\n"
        "[OK] typecheck\n"
        "\n2/3 subtasks succeeded.\n"
    )


def test_prints_all_success(capsys):
    outcomes = [
        SubtaskOutcome(name="lint", success=True),
        SubtaskOutcome(name="tests", success=True),
    ]

    print_subtask_outcomes(outcomes)

    captured = capsys.readouterr()
    assert captured.out == (
        "[OK] lint\n"
        "[OK] tests\n"
        "\n2/2 subtasks succeeded.\n"
    )


def test_prints_all_failure(capsys):
    outcomes = [
        SubtaskOutcome(name="lint", success=False, detail="syntax error"),
        SubtaskOutcome(name="tests", success=False, detail="1 failure"),
    ]

    print_subtask_outcomes(outcomes)

    captured = capsys.readouterr()
    assert captured.out == (
        "[FAILED] lint: syntax error\n"
        "[FAILED] tests: 1 failure\n"
        "\n0/2 subtasks succeeded.\n"
    )


def test_prints_empty_list(capsys):
    print_subtask_outcomes([])

    captured = capsys.readouterr()
    assert captured.out == "No subtasks were run.\n"


def test_failure_without_detail_omits_colon(capsys):
    print_subtask_outcomes([SubtaskOutcome(name="deploy", success=False)])

    captured = capsys.readouterr()
    assert captured.out == "[FAILED] deploy\n\n0/1 subtasks succeeded.\n"
