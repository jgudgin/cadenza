"""Direct unit coverage for `print_subtask_outcomes`: a pure reporting
helper for the self-maintenance agent's CLI, so it doesn't need Postgres
or any of the orchestrator machinery - just fake outcome dicts and
`capsys`, the same way we'd unit test any other formatting helper."""

from __future__ import annotations

from cadenza.agents.self_maintain.cli import print_subtask_outcomes


def test_empty_outcomes_prints_placeholder_message(capsys):
    print_subtask_outcomes([])

    out = capsys.readouterr().out
    assert "No subtasks were run." in out


def test_succeeded_outcome_is_rendered(capsys):
    print_subtask_outcomes(
        [
            {"task_id": 1, "type": "run_tests", "status": "succeeded"},
        ]
    )

    out = capsys.readouterr().out
    assert "task 1" in out
    assert "run_tests" in out
    assert "OK" in out
    assert "FAILED" not in out


def test_failed_outcome_includes_error_message(capsys):
    print_subtask_outcomes(
        [
            {"task_id": 2, "type": "apply_fix", "status": "failed", "error": "patch did not apply"},
        ]
    )

    out = capsys.readouterr().out
    assert "task 2" in out
    assert "apply_fix" in out
    assert "FAILED" in out
    assert "patch did not apply" in out


def test_mixed_outcomes_each_get_their_own_line(capsys):
    outcomes = [
        {"task_id": 1, "type": "lint", "status": "succeeded"},
        {"task_id": 2, "type": "run_tests", "status": "failed", "error": "3 tests failed"},
        {"task_id": 3, "type": "cleanup", "status": "dropped"},
        {"task_id": 4, "type": "notify", "status": "blocked"},
    ]

    print_subtask_outcomes(outcomes)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 4
    assert "OK" in lines[0] and "task 1" in lines[0]
    assert "FAILED" in lines[1] and "3 tests failed" in lines[1]
    assert "SKIPPED" in lines[2] and "task 3" in lines[2]
    assert "BLOCKED" in lines[3] and "task 4" in lines[3]


def test_unknown_status_falls_back_to_uppercased_status(capsys):
    print_subtask_outcomes(
        [
            {"task_id": 5, "type": "mystery", "status": "needs_review"},
        ]
    )

    out = capsys.readouterr().out
    assert "NEEDS_REVIEW" in out


def test_missing_fields_degrade_gracefully_instead_of_raising(capsys):
    # No task_id, no type, no status at all - should still print
    # something sensible rather than blow up mid-report.
    print_subtask_outcomes([{}])

    out = capsys.readouterr().out
    assert "task ?" in out
    assert "unknown" in out


def test_succeeded_outcome_with_stray_error_field_still_shown(capsys):
    # Even a "succeeded" outcome might carry a warning-ish message; it
    # should still be surfaced rather than silently dropped.
    print_subtask_outcomes(
        [
            {"task_id": 6, "type": "run_tests", "status": "succeeded", "error": "1 test skipped"},
        ]
    )

    out = capsys.readouterr().out
    assert "OK" in out
    assert "1 test skipped" in out
