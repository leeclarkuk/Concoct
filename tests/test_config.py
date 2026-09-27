from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from concoct.config import Settings, load_settings
from concoct.models import Complexity


def test_defaults_are_local_and_safe() -> None:
    settings = Settings()
    assert settings.push is False
    assert settings.cleanup is False
    assert settings.visibility == "private"
    assert settings.provider == "anthropic"
    assert settings.languages == ["python"]
    assert settings.max_cost_usd == 10.0
    assert isinstance(settings.seed, int)


def test_environment_uses_concoct_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCOCT_REPOS", "3")
    monkeypatch.setenv("CONCOCT_LANGUAGES", "python, Go")
    monkeypatch.setenv("CONCOCT_TECHNOLOGIES", "fastapi,postgres")
    monkeypatch.setenv("CONCOCT_COMPLEXITY", "high")
    monkeypatch.setenv("CONCOCT_PUSH", "true")
    settings = Settings()
    assert settings.repos == 3
    assert settings.languages == ["python", "go"]
    assert settings.technologies == ["fastapi", "postgres"]
    assert settings.complexity == Complexity.HIGH
    assert settings.push is True


def test_cli_overrides_beat_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCOCT_REPOS", "3")
    monkeypatch.setenv("CONCOCT_LANGUAGES", "rust")
    settings = load_settings(repos=5, languages=("python",), technologies=(), seed=None)
    assert settings.repos == 5
    assert settings.languages == ["python"]
    # empty multi-option means "not supplied", so defaults/env still apply
    assert settings.technologies == []


def test_dotenv_file_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("CONCOCT_MODEL=claude-sonnet-5\nCONCOCT_MIN_COMMITS=3\n")
    settings = Settings()
    assert settings.model == "claude-sonnet-5"
    assert settings.min_commits == 3


def test_explicit_env_file(tmp_path: Path) -> None:
    env = tmp_path / "custom.env"
    env.write_text("CONCOCT_REPOS=7\n")
    assert load_settings(_env_file=env).repos == 7


def test_anthropic_key_fallback_and_secret_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0123456789abcdef")
    settings = Settings()
    assert settings.anthropic_api_key is not None
    assert "sk-ant" not in repr(settings)
    public = settings.public_dict()
    assert "anthropic_api_key" not in public
    assert public["anthropic_api_key_set"] is True
    assert "sk-ant" not in str(public)


def test_min_greater_than_max_rejected() -> None:
    with pytest.raises(ValidationError, match="min_commits"):
        load_settings(min_commits=20, max_commits=10)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timezone", "Mars/Olympus"),
        ("repos", 0),
        ("author_email", "not-an-email"),
        ("visibility", "internal"),
        ("languages", ""),
    ],
)
def test_invalid_values_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_naive_end_date_becomes_utc() -> None:
    from datetime import datetime

    settings = load_settings(end_date=datetime(2024, 1, 1, 12, 0))
    assert settings.history_end().tzinfo is not None
