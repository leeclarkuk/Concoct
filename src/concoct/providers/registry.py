"""Construct the configured provider."""

from __future__ import annotations

from concoct.config import Settings
from concoct.providers.base import LLMProvider


class ProviderConfigError(ValueError):
    pass


def create_provider(settings: Settings) -> LLMProvider:
    if settings.provider == "offline":
        from concoct.providers.offline import OfflineProvider

        return OfflineProvider()
    if settings.provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise ProviderConfigError(
                "No Anthropic API key configured. Set CONCOCT_ANTHROPIC_API_KEY (or "
                "ANTHROPIC_API_KEY) in the environment or .env, or use --provider offline "
                "for the built-in demo template."
            )
        from concoct.providers.anthropic import ClaudeProvider

        return ClaudeProvider(
            settings.anthropic_api_key.get_secret_value(),
            settings.model,
            effort=settings.effort,
            base_url=settings.anthropic_base_url,
            timeout=settings.request_timeout,
        )
    raise ProviderConfigError(f"unknown provider {settings.provider!r}")
