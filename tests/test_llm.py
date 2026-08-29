"""cadenza/llm.py is the one place that talks to Claude. These tests fake
the Anthropic client so they run with no API key and no network call, while
still exercising the real response-parsing logic - the part that breaks
silently if the SDK's response shape ever changes and nothing else in this
suite would catch it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cadenza import llm


class _FakeMessages:
    def __init__(self, content: list) -> None:
        self._content = content
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=self._content)


class _FakeClient:
    def __init__(self, content: list) -> None:
        self.messages = _FakeMessages(content)


@pytest.fixture(autouse=True)
def _clear_client_cache():
    llm._client.cache_clear()
    yield
    llm._client.cache_clear()


async def test_decide_next_steps_parses_a_fan_out_fan_in_decision(monkeypatch):
    decision = {
        "reasoning": "needs two scenarios then a join",
        "run_complete": False,
        "escalate": False,
        "context_updates": {"assumptions_drafted": True},
        "next_tasks": [
            {"type": "scenario", "input": {"case": "high"}, "key": "high"},
            {"type": "scenario", "input": {"case": "low"}, "key": "low"},
            {"type": "join", "depends_on": ["high", "low"]},
        ],
    }
    fake = _FakeClient([SimpleNamespace(type="tool_use", input=decision)])
    monkeypatch.setattr(llm, "_client", lambda: fake)

    outcome = await llm.decide_next_steps(system="sys", prompt="prompt")

    assert outcome.run_complete is False
    assert outcome.escalate is False
    assert outcome.context_updates == {"assumptions_drafted": True}
    assert [t.type for t in outcome.tasks] == ["scenario", "scenario", "join"]
    assert outcome.tasks[2].depends_on == ["high", "low"]
    assert fake.messages.kwargs["tool_choice"] == {"type": "tool", "name": "decide_next_steps"}


async def test_decide_next_steps_raises_when_no_tool_use_block(monkeypatch):
    fake = _FakeClient([SimpleNamespace(type="text", text="I'd rather just chat")])
    monkeypatch.setattr(llm, "_client", lambda: fake)

    with pytest.raises(ValueError, match="no tool_use block"):
        await llm.decide_next_steps(system="sys", prompt="prompt")


async def test_ask_claude_json_returns_the_tool_input(monkeypatch):
    fake = _FakeClient([SimpleNamespace(type="tool_use", input={"diagnosis": "assets != liabilities + equity"})])
    monkeypatch.setattr(llm, "_client", lambda: fake)

    result = await llm.ask_claude_json(
        system="sys",
        prompt="prompt",
        tool={"name": "diagnose", "input_schema": {"type": "object", "properties": {}}},
    )

    assert result == {"diagnosis": "assets != liabilities + equity"}
    assert fake.messages.kwargs["tool_choice"] == {"type": "tool", "name": "diagnose"}
