"""clarify_interactively drives a real multi-round loop, but the LLM call
itself is mocked (canned-for-CI, same as everywhere else) and `ask` is a
plain fake instead of a real terminal - this tests the loop's own logic
(when to stop, what it returns, the round cap), not the model's judgement.
"""

from __future__ import annotations

from cadenza.agents.self_maintain import clarify as clarify_mod
from cadenza.agents.self_maintain.clarify import MAX_ROUNDS, clarify_interactively


async def test_proceeds_immediately_when_no_questions_are_needed(monkeypatch):
    async def fake_ask_claude_json(*, system, prompt, tool):
        return {"questions": [], "refined_brief": "add a missing test for the Drop exception path"}

    monkeypatch.setattr(clarify_mod, "ask_claude_json", fake_ask_claude_json)

    def ask(question: str) -> str:
        raise AssertionError("should never ask a question here")

    brief = await clarify_interactively("add a missing test", ask=ask)

    assert brief == "add a missing test for the Drop exception path"


async def test_asks_questions_then_proceeds_with_the_refined_brief(monkeypatch):
    calls = {"n": 0}

    async def fake_ask_claude_json(*, system, prompt, tool):
        calls["n"] += 1
        if calls["n"] == 1:
            assert "Request: fix the bug" in prompt
            return {"questions": ["Which bug specifically?"]}
        assert "Q: Which bug specifically?" in prompt
        assert "A: the retry-collision one" in prompt
        return {"questions": [], "refined_brief": "fix the retry-collision bug in create_worktree"}

    monkeypatch.setattr(clarify_mod, "ask_claude_json", fake_ask_claude_json)

    answers = iter(["the retry-collision one"])
    brief = await clarify_interactively("fix the bug", ask=lambda q: next(answers))

    assert brief == "fix the retry-collision bug in create_worktree"
    assert calls["n"] == 2


async def test_gives_up_asking_after_max_rounds_instead_of_looping_forever(monkeypatch):
    async def fake_ask_claude_json(*, system, prompt, tool):
        return {"questions": ["another question?"]}  # never satisfied

    monkeypatch.setattr(clarify_mod, "ask_claude_json", fake_ask_claude_json)

    asked = []

    def ask(question: str) -> str:
        asked.append(question)
        return "some answer"

    brief = await clarify_interactively("a vague idea", ask=ask)

    assert len(asked) == MAX_ROUNDS
    assert "a vague idea" in brief  # falls back to the transcript, not empty-handed
