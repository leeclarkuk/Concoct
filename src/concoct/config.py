"""Configuration: CLI options > environment (CONCOCT_*) > .env file > defaults.

``Settings`` is the single source of truth for a run. The CLI builds it with
explicit keyword overrides (which pydantic-settings gives the highest
priority), so every option can equally be supplied through the environment.
"""

from __future__ import annotations

import secrets as _stdlib_secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from concoct.models import Complexity
from concoct.secrets import registry

DEFAULT_MODEL = "claude-opus-5"
ENV_PREFIX = "CONCOCT_"

CsvList = Annotated[list[str], NoDecode]


def _split_csv(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            items.extend(_split_csv(item))
        return items
    return value


class Settings(BaseSettings):
    """All Concoct configuration. Secret fields are never logged or persisted."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    # --- credentials -----------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "anthropic_api_key", "CONCOCT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        ),
    )
    github_token: SecretStr | None = None
    github_owner: str | None = Field(
        default=None, description="Organisation to create repos in (default: the token's user)."
    )

    # --- provider --------------------------------------------------------
    provider: Literal["anthropic", "offline"] = "anthropic"
    model: str = DEFAULT_MODEL
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    anthropic_base_url: str | None = None
    request_timeout: float = Field(default=600.0, gt=0)

    # --- what to generate ------------------------------------------------
    languages: CsvList = Field(default_factory=lambda: ["python"])
    technologies: CsvList = Field(default_factory=list)
    categories: CsvList = Field(default_factory=list)
    repos: int = Field(default=1, ge=1, le=100)
    complexity: Complexity | Literal["random"] = Complexity.MEDIUM
    license: str | None = None
    synthetic_disclosure: bool = True

    # --- history ---------------------------------------------------------
    history_days: int = Field(default=180, ge=1, le=3650)
    end_date: datetime | None = None
    min_commits: int = Field(default=8, ge=2, le=200)
    max_commits: int = Field(default=20, ge=2, le=200)
    timezone: str = "UTC"
    author_name: str | None = None
    author_email: str | None = None
    seed: int | None = None

    # --- budget ----------------------------------------------------------
    max_cost_usd: float | None = Field(default=10.0, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=1)

    # --- validation ------------------------------------------------------
    max_repair_attempts: int = Field(default=2, ge=0, le=5)
    run_commands: bool = True
    command_timeout: int = Field(default=600, ge=10)
    strict_lint: bool = False

    # --- output / publishing -------------------------------------------
    output_dir: Path = Path("concoct-output")
    visibility: Literal["private", "public"] = "private"
    push: bool = False
    cleanup: bool = False

    # --- diagnostics -----------------------------------------------------
    log_level: str = "WARNING"
    log_file: Path | None = None

    @field_validator("languages", "technologies", "categories", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("languages")
    @classmethod
    def _lower_languages(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip().lower() for v in value if v.strip()]
        if not cleaned:
            raise ValueError("at least one language is required")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("end_date")
    @classmethod
    def _aware_end_date(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    @field_validator("author_email")
    @classmethod
    def _valid_email(cls, value: str | None) -> str | None:
        if value is not None and ("@" not in value or " " in value.strip()):
            raise ValueError(f"invalid author email {value!r}")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.min_commits > self.max_commits:
            raise ValueError(
                f"min_commits ({self.min_commits}) must be <= max_commits ({self.max_commits})"
            )
        if self.seed is None:
            self.seed = _stdlib_secrets.randbelow(2**31)
        registry.register(
            self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else None
        )
        registry.register(self.github_token.get_secret_value() if self.github_token else None)
        return self

    # ------------------------------------------------------------------
    @property
    def resolved_seed(self) -> int:
        assert self.seed is not None
        return self.seed

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def history_end(self) -> datetime:
        return self.end_date or datetime.now(UTC)

    def public_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot with every secret removed (for manifests/UI)."""
        data = self.model_dump(mode="json", exclude={"anthropic_api_key", "github_token"})
        data["anthropic_api_key_set"] = self.anthropic_api_key is not None
        data["github_token_set"] = self.github_token is not None
        return data


def load_settings(**overrides: Any) -> Settings:
    """Build settings, treating ``None`` overrides as "not supplied"."""
    explicit = {key: value for key, value in overrides.items() if value is not None}
    for key in ("languages", "technologies", "categories"):
        if key in explicit and not explicit[key]:
            del explicit[key]  # empty multi-option means "not supplied"
    return Settings(**explicit)


ENV_TEMPLATE = f"""\
# Concoct configuration. Every setting can also be passed as a CLI option.
# Never commit this file: it contains credentials.
CONCOCT_ANTHROPIC_API_KEY=
# Only needed for --push / list-repos --remote / delete --remote
CONCOCT_GITHUB_TOKEN=
# CONCOCT_GITHUB_OWNER=my-org

CONCOCT_PROVIDER=anthropic
CONCOCT_MODEL={DEFAULT_MODEL}
CONCOCT_EFFORT=high
CONCOCT_OUTPUT_DIR=concoct-output
CONCOCT_VISIBILITY=private
CONCOCT_MAX_COST_USD=10
CONCOCT_MAX_REPAIR_ATTEMPTS=2
# CONCOCT_LANGUAGES=python,typescript
# CONCOCT_TECHNOLOGIES=fastapi
# CONCOCT_CATEGORIES=developer tool
# CONCOCT_SEED=1234
# CONCOCT_AUTHOR_NAME=Synthetic Author
# CONCOCT_AUTHOR_EMAIL=synthetic@example.invalid
"""
