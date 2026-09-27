from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from concoct.budget import Budget, BudgetExceededError, MeteredProvider
from concoct.generation.parsing import extract_json
from concoct.providers.anthropic import ClaudeProvider
from concoct.providers.base import (
    LLMProvider,
    LLMRequest,
    ProviderError,
    ProviderRefusalError,
    ProviderTruncatedError,
    TokenUsage,
)
from concoct.providers.offline import MAX_COMMITS, OfflineProvider, Project, select_stages
from concoct.providers.pricing import estimate_cost, price_for
from concoct.providers.registry import ProviderConfigError, create_provider
from concoct.validation.syntax import check_syntax
from tests.conftest import ScriptedProvider

REQUEST = LLMRequest(task="plan", system="sys", prompt="hello", json_schema={"type": "object"})


def message(text: str = '{"ok": true}', stop_reason: str = "end_turn", **extra: Any) -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=None,
        ),
        stop_reason=stop_reason,
        model="claude-opus-5",
        **extra,
    )


class FakeStream:
    def __init__(self, result: Any) -> None:
        self.result = result

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.result


class FakeClient:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return FakeStream(result)


def bad_request() -> anthropic.BadRequestError:
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com"))
    return anthropic.BadRequestError("unsupported parameter", response=response, body=None)


def test_claude_provider_request_shape() -> None:
    client = FakeClient(message())
    provider = ClaudeProvider("sk-ant-test", "claude-opus-5", effort="high", client=client)
    response = provider.complete(REQUEST)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == "high"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["messages"] == [{"role": "user", "content": "hello"}]
    assert "temperature" not in call
    assert call["extra_body"] == {"fallbacks": "default"}
    assert response.text == '{"ok": true}'
    assert response.usage == TokenUsage(10, 20, 5, 0)


def test_claude_provider_caches_prompt_prefix() -> None:
    client = FakeClient(message())
    provider = ClaudeProvider("k" * 10, "claude-opus-5", client=client)
    provider.complete(
        LLMRequest(task="commit", system="s", prompt="variable", prompt_prefix="stable")
    )
    content = client.calls[0]["messages"][0]["content"]
    assert content[0] == {
        "type": "text",
        "text": "stable",
        "cache_control": {"type": "ephemeral"},
    }
    assert content[1] == {"type": "text", "text": "variable"}


def test_claude_provider_haiku_has_no_adaptive_thinking() -> None:
    client = FakeClient(message())
    ClaudeProvider("k" * 10, "claude-haiku-4-5", client=client).complete(REQUEST)
    call = client.calls[0]
    assert "thinking" not in call
    assert "effort" not in call["output_config"]
    assert "extra_body" not in call


def test_claude_provider_drops_optional_features_after_bad_request() -> None:
    client = FakeClient(bad_request(), message(), message())
    provider = ClaudeProvider("k" * 10, "claude-opus-5", client=client)
    provider.complete(REQUEST)
    provider.complete(REQUEST)
    assert "format" in client.calls[0]["output_config"]
    assert "format" not in client.calls[1]["output_config"]
    assert "extra_body" not in client.calls[1]
    assert "format" not in client.calls[2]["output_config"]


def test_claude_provider_refusal_and_truncation_carry_usage() -> None:
    refusal = message(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    client = FakeClient(refusal, message(stop_reason="max_tokens"))
    provider = ClaudeProvider("k" * 10, "claude-opus-5", client=client)
    with pytest.raises(ProviderRefusalError, match="cyber") as refused:
        provider.complete(REQUEST)
    assert refused.value.usage == TokenUsage(10, 20, 5, 0)
    with pytest.raises(ProviderTruncatedError):
        provider.complete(REQUEST)


def test_claude_provider_auth_error_is_clear_and_redacted() -> None:
    response = httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com"))
    error = anthropic.AuthenticationError(
        "bad key sk-ant-" + "z" * 30, response=response, body=None
    )
    provider = ClaudeProvider("k" * 10, "claude-opus-5", client=FakeClient(error))
    with pytest.raises(ProviderError, match="CONCOCT_ANTHROPIC_API_KEY") as exc:
        provider.complete(REQUEST)
    assert "zzzz" not in str(exc.value)


def test_provider_protocol() -> None:
    assert isinstance(OfflineProvider(), LLMProvider)
    assert isinstance(ClaudeProvider("k" * 10, "claude-opus-5", client=FakeClient()), LLMProvider)


# ---------------------------------------------------------------- registry
def test_registry_requires_key_for_anthropic(make_settings) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ProviderConfigError, match="CONCOCT_ANTHROPIC_API_KEY"):
        create_provider(make_settings(provider="anthropic"))
    assert isinstance(create_provider(make_settings(provider="offline")), OfflineProvider)
    claude = create_provider(make_settings(provider="anthropic", anthropic_api_key="sk-ant-x" * 3))
    assert isinstance(claude, ClaudeProvider)


# ------------------------------------------------------------------ budget
def test_pricing() -> None:
    assert price_for("claude-opus-5") is not None
    assert price_for("claude-sonnet-4-6-20260101") == price_for("claude-sonnet-4-6")
    assert price_for("mystery-model") is None
    cost = estimate_cost("claude-opus-5", TokenUsage(input_tokens=1_000_000, output_tokens=0))
    assert cost == pytest.approx(5.0)


def test_metered_provider_enforces_cost_budget() -> None:
    inner = ScriptedProvider(["{}", "{}", "{}"], usage=TokenUsage(input_tokens=500_000))
    budget = Budget(max_cost_usd=1.5)
    metered = MeteredProvider(inner, budget)  # sonnet-5: $2/MTok input -> $1 per call
    seen = []
    metered.add_listener(seen.append)
    metered.complete(REQUEST)
    metered.complete(REQUEST)
    with pytest.raises(BudgetExceededError, match="cost budget"):
        metered.complete(REQUEST)
    assert len(inner.requests) == 2
    assert budget.total.calls == 2
    assert budget.total.cost_usd == pytest.approx(2.0)
    assert len(seen) == 2


def test_metered_provider_counts_failed_calls_and_token_budget() -> None:
    inner = ScriptedProvider([ProviderTruncatedError("cut", usage=TokenUsage(output_tokens=900))])
    metered = MeteredProvider(inner, Budget(max_cost_usd=None, max_tokens=500))
    with pytest.raises(ProviderTruncatedError):
        metered.complete(REQUEST)
    with pytest.raises(BudgetExceededError, match="token budget"):
        metered.complete(REQUEST)
    assert metered.budget.total.output_tokens == 900


# ------------------------------------------------------------------ offline
def test_offline_plan_honours_commit_count_and_existing_names() -> None:
    provider = OfflineProvider()
    req = LLMRequest(
        task="plan",
        system="",
        prompt="",
        context={
            "request": {"language": "python", "commit_count": 9, "existing_names": ["hooklens"]}
        },
    )
    plan = json.loads(provider.complete(req).text)
    assert len(plan["commits"]) == 9
    assert plan["commits"][0]["kind"] == "scaffold"
    assert plan["project"]["name"] != "hooklens"


def test_offline_rejects_unsupported_language() -> None:
    req = LLMRequest(task="plan", system="", prompt="", context={"request": {"language": "go"}})
    with pytest.raises(ProviderError, match="only has a Python template"):
        OfflineProvider().complete(req)


@pytest.mark.parametrize("count", range(2, MAX_COMMITS + 1))
def test_offline_every_prefix_of_every_stage_subset_parses(count: int) -> None:
    stages = select_stages(count)
    assert stages[0].key == "scaffold"
    for end in range(1, len(stages) + 1):
        project = Project({s.key for s in stages[:end]}, license="MIT")
        for path in [
            "pyproject.toml",
            "README.md",
            "src/hooklens/app.py",
            "src/hooklens/api.py",
            "src/hooklens/store.py",
            "tests/test_capture.py",
            "tests/test_extras.py",
            ".github/workflows/ci.yml",
        ]:
            content = project.render(path)
            if content is not None:
                assert check_syntax(path, content) is None, (count, end, path)


def test_offline_repair_proposes_nothing() -> None:
    req = LLMRequest(task="repair", system="", prompt="", context={})
    assert extract_json(OfflineProvider().complete(req).text)["changes"] == []
