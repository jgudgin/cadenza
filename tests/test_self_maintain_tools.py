"""The coding loop's tool-dispatch logic (turn handling, `finish`,
tracking which files changed, trusting its own test run over whatever the
model last said) tested against a scripted fake model - same
canned-for-CI-friendliness principle as test_finance_workflow.py, just at
the level of one tool-use turn instead of one `ask_claude_json` call.
"""

from __future__ import annotations

from types import SimpleNamespace

from cadenza.agents.self_maintain import tools, workspace


def _block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


async def test_coding_loop_writes_a_file_and_trusts_its_own_test_run(tmp_path, monkeypatch):
    responses = [
        SimpleNamespace(
            content=[
                _block(
                    "tool_use",
                    id="1",
                    name="write_file",
                    input={"path": "answer.py", "content": "ANSWER = 42\n"},
                )
            ]
        ),
        SimpleNamespace(content=[_block("tool_use", id="2", name="run_tests", input={})]),
        SimpleNamespace(content=[_block("tool_use", id="3", name="finish", input={"summary": "added ANSWER"})]),
    ]
    fake_client = _FakeClient(responses)
    monkeypatch.setattr(tools, "_client", lambda: fake_client)
    monkeypatch.setattr(workspace, "run_tests", lambda *a, **k: (True, "1 passed"))

    result = await tools.run_coding_loop(worktree_path=str(tmp_path), task="add a constant", previous_failure=None)

    assert result["changed_files"] == ["answer.py"]
    assert result["tests_passed"] is True
    assert result["summary"] == "added ANSWER"
    assert (tmp_path / "answer.py").read_text() == "ANSWER = 42\n"


async def test_coding_loop_reports_the_real_test_result_even_if_model_never_finishes(tmp_path, monkeypatch):
    """The authoritative pass/fail always comes from this module actually
    running the tests, per tools.py's own docstring - not from whatever
    the model last claimed, and not blocked on the model calling finish."""
    responses = [SimpleNamespace(content=[])]  # model stops calling tools without finishing
    fake_client = _FakeClient(responses)
    monkeypatch.setattr(tools, "_client", lambda: fake_client)
    monkeypatch.setattr(workspace, "run_tests", lambda *a, **k: (False, "1 failed"))

    result = await tools.run_coding_loop(worktree_path=str(tmp_path), task="add a constant", previous_failure=None)

    assert result["tests_passed"] is False
    assert result["summary"] == "(model stopped without calling a tool or explaining why)"


async def test_coding_loop_keeps_the_models_own_words_when_it_bails_without_a_tool_call(tmp_path, monkeypatch):
    """A run that stops after one text-only turn (e.g. the model explains
    why it isn't attempting the task) must surface that explanation, not
    silently discard it as if nothing happened - this is what made an
    early real run of this loop (asking it to build a lease/heartbeat
    system) unreadable: it reported '(agent did not call finish)' with no
    way to tell whether the model tried and failed or never engaged."""
    responses = [
        SimpleNamespace(content=[_block("text", text="This task is too large for a single bounded attempt.")])
    ]
    fake_client = _FakeClient(responses)
    monkeypatch.setattr(tools, "_client", lambda: fake_client)
    monkeypatch.setattr(workspace, "run_tests", lambda *a, **k: (True, "no tests ran"))

    result = await tools.run_coding_loop(worktree_path=str(tmp_path), task="add a constant", previous_failure=None)

    assert result["summary"] == "This task is too large for a single bounded attempt."
    assert result["changed_files"] == []
    assert result["changed_files"] == []
