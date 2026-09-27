"""Provider-neutral request/response types.

The generation pipeline speaks only in these types. A provider receives a
system prompt, a user prompt, an optional JSON schema for the reply and a
``task``/``context`` pair. LLM-backed providers ignore ``context`` (everything
they need is rendered into the prompts); deterministic providers such as the
offline provider use it instead of parsing prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

TaskName = Literal["plan", "commit", "repair"]


@dataclass(frozen=True)
class LLMRequest:
    task: TaskName
    system: str
    prompt: str
    json_schema: dict[str, Any] | None = None
    max_output_tokens: int = 32_000
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: TokenUsage
    model: str
    stop_reason: str | None = None


class ProviderError(RuntimeError):
    """A provider failed in a way the caller cannot fix by retrying the same request.

    ``usage`` carries tokens that were billed even though the call failed
    (e.g. a truncated or refused reply) so budgets stay accurate.
    """

    def __init__(self, message: str, usage: TokenUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class ProviderRefusalError(ProviderError):
    """The model declined the request."""


class ProviderTruncatedError(ProviderError):
    """The reply hit the output-token limit and is incomplete."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
