"""Claude provider. This is the only module that imports the Anthropic SDK."""

from __future__ import annotations

from typing import Any

import anthropic

from concoct.logging import get_logger
from concoct.providers.base import (
    LLMRequest,
    LLMResponse,
    ProviderError,
    ProviderRefusalError,
    ProviderTruncatedError,
    TokenUsage,
)
from concoct.secrets import redact

log = get_logger("providers.anthropic")

# Models that accept adaptive thinking and the ``effort`` control.
_NO_ADAPTIVE_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5")
# Models that support the server-side refusal fallback (routes a declined
# request to a suitable model inside the same API call).
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1", "claude-fable-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    """Calls the Anthropic Messages API with streaming and structured output."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        effort: str = "high",
        base_url: str | None = None,
        timeout: float = 600.0,
        max_retries: int = 3,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self._client = client or anthropic.Anthropic(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        # Optional request features are dropped (once, for the rest of the run)
        # if the API rejects them for this model/account.
        self._optional_features = True

    # ------------------------------------------------------------------
    def _build_kwargs(self, request: LLMRequest, optional: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            # Stable system prompt first, marked cacheable: it is identical for
            # every commit of a run, so later calls read it from cache.
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": self._user_content(request)}],
        }
        adaptive = not self.model.startswith(_NO_ADAPTIVE_PREFIXES)
        output_config: dict[str, Any] = {}
        if adaptive:
            kwargs["thinking"] = {"type": "adaptive"}
            output_config["effort"] = self.effort
        if optional and request.json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
        if output_config:
            kwargs["output_config"] = output_config
        if optional and self.model.startswith(_FALLBACK_MODELS):
            kwargs["extra_headers"] = {"anthropic-beta": _FALLBACK_BETA}
            kwargs["extra_body"] = {"fallbacks": "default"}
        return kwargs

    @staticmethod
    def _user_content(request: LLMRequest) -> str | list[dict[str, Any]]:
        if not request.prompt_prefix:
            return request.prompt
        return [
            {
                "type": "text",
                "text": request.prompt_prefix,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": request.prompt},
        ]

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            message = self._stream(request, self._optional_features)
        except anthropic.BadRequestError as exc:
            if not self._optional_features:
                raise ProviderError(f"Claude rejected the request: {redact(str(exc))}") from exc
            log.warning(
                "Claude rejected optional request features (%s); retrying without "
                "structured output / fallbacks for the rest of this run.",
                redact(str(exc)),
            )
            self._optional_features = False
            try:
                message = self._stream(request, False)
            except anthropic.BadRequestError as retry_exc:
                raise ProviderError(
                    f"Claude rejected the request: {redact(str(retry_exc))}"
                ) from retry_exc
        return self._to_response(message)

    def _stream(self, request: LLMRequest, optional: bool) -> Any:
        kwargs = self._build_kwargs(request, optional)
        try:
            with self._client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except anthropic.BadRequestError:
            raise
        except anthropic.AuthenticationError as exc:
            raise ProviderError(
                "Anthropic authentication failed: check CONCOCT_ANTHROPIC_API_KEY"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError(f"Anthropic permission denied: {redact(str(exc))}") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"Unknown model {self.model!r}: {redact(str(exc))}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"Rate limited after retries: {redact(str(exc))}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic API error {exc.status_code}: {redact(str(exc))}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Could not reach the Anthropic API: {redact(str(exc))}") from exc

    def _to_response(self, message: Any) -> LLMResponse:
        usage = message.usage
        token_usage = TokenUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderRefusalError(
                f"Claude declined the request (category: {category})", usage=token_usage
            )
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        if stop_reason == "max_tokens":
            raise ProviderTruncatedError(
                "Claude's reply hit the output-token limit before completing", usage=token_usage
            )
        return LLMResponse(
            text=text,
            usage=token_usage,
            model=getattr(message, "model", self.model),
            stop_reason=stop_reason,
        )
