from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from concoct.config import Settings, load_settings
from concoct.providers.base import LLMRequest, LLMResponse, TokenUsage
from concoct.secrets import registry


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep tests independent of the developer's environment, .env and git config."""
    for key in list(os.environ):
        if key.startswith("CONCOCT_") or key in {"ANTHROPIC_API_KEY"}:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        defaults: dict[str, Any] = {
            "output_dir": tmp_path / "out",
            "provider": "offline",
            "seed": 1234,
            "run_commands": False,
            "min_commits": 8,
            "max_commits": 15,
        }
        defaults.update(overrides)
        return load_settings(**defaults)

    return factory


class ScriptedProvider:
    """Provider returning queued replies (str, dict or exception) in order."""

    name = "scripted"
    model = "claude-sonnet-5"

    def __init__(self, replies: list[Any] | None = None, usage: TokenUsage | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[LLMRequest] = []
        self.usage = usage or TokenUsage(input_tokens=100, output_tokens=50)

    def queue(self, *replies: Any) -> None:
        self.replies.extend(replies)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError(f"unexpected {request.task} request")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(request)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text=text, usage=self.usage, model=self.model, stop_reason="end_turn")


@pytest.fixture
def scripted() -> ScriptedProvider:
    return ScriptedProvider()


def plan_payload(count: int = 8, name: str = "tiny-tool") -> dict[str, Any]:
    kinds = ["scaffold"] + ["feature", "test", "refactor", "fix", "docs", "config"] * 10
    return {
        "project": {
            "name": name,
            "title": "Tiny Tool",
            "description": "A tiny tool.",
            "architecture": "One module.",
            "features": ["a", "b"],
            "topics": ["Tools", "tiny tool"],
        },
        "commits": [
            {
                "kind": kinds[i],
                "message": f"{kinds[i]}: step {i + 1}",
                "intent": f"do step {i + 1}",
                "files": [f"src/tiny/step{i + 1}.py"],
            }
            for i in range(count)
        ],
    }
