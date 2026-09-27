from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from concoct.cli import cli

OFFLINE = ["--provider", "offline", "--no-run-commands", "--seed", "7", "-o", "out"]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def generate(runner: CliRunner, *extra: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli, ["generate", *OFFLINE, *extra], catch_exceptions=False)


def test_help_lists_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for command in ("generate", "status", "list-repos", "inspect", "delete", "config"):
        assert command in result.output


def test_generate_without_key_fails_clearly(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["generate", "--language", "python"])
    assert result.exit_code == 2
    assert "CONCOCT_ANTHROPIC_API_KEY" in result.output
    assert not Path("concoct-output").exists()


def test_dry_run_makes_no_changes(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["generate", "--dry-run", "-r", "2", "-l", "python", "-l", "go", "--seed", "3"]
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "No model calls" in result.output
    assert not Path("concoct-output").exists()


def test_invalid_options_are_reported(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["generate", "--min-commits", "20", "--max-commits", "10", "--dry-run"]
    )
    assert result.exit_code == 2
    assert "min_commits" in result.output


def test_acceptance_command_offline(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance command, with the provider selected through the environment."""
    monkeypatch.setenv("CONCOCT_PROVIDER", "offline")
    monkeypatch.setenv("CONCOCT_RUN_COMMANDS", "false")
    result = runner.invoke(
        cli,
        [
            "generate",
            "--language",
            "python",
            "--tech",
            "fastapi",
            "--category",
            "developer tool",
            "--repos",
            "1",
            "--min-commits",
            "8",
            "--max-commits",
            "15",
            "--no-push",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "1/1 repositories succeeded" in result.output
    assert "Development plan" in result.output
    assert "Validation" in result.output


def test_generate_list_inspect_delete_cycle(runner: CliRunner) -> None:
    result = generate(runner)
    assert result.exit_code == 0, result.output

    listed = runner.invoke(cli, ["list-repos", "-o", "out", "--json"])
    repos = json.loads(listed.output)
    assert len(repos) == 1 and repos[0]["status"] == "succeeded"
    name = repos[0]["name"]

    inspected = runner.invoke(cli, ["inspect", name, "-o", "out"])
    assert inspected.exit_code == 0, inspected.output
    assert "History" in inspected.output and "Initial project scaffold" in inspected.output

    as_json = json.loads(runner.invoke(cli, ["inspect", f"out/{name}", "--json"]).output)
    assert as_json["tool"] == "concoct"

    aborted = runner.invoke(cli, ["delete", name, "-o", "out"], input="n\n")
    assert aborted.exit_code == 1
    assert Path("out", name).exists()

    deleted = runner.invoke(cli, ["delete", name, "-o", "out", "--yes"])
    assert deleted.exit_code == 0, deleted.output
    assert not Path("out", name).exists()


def test_delete_refuses_directories_without_manifest(runner: CliRunner) -> None:
    Path("out/precious").mkdir(parents=True)
    result = runner.invoke(cli, ["delete", "precious", "-o", "out", "--yes"])
    assert result.exit_code == 1
    assert "no Concoct manifest" in result.output
    assert Path("out/precious").exists()


def test_push_requires_token(runner: CliRunner) -> None:
    result = generate(runner, "--push", "--yes")
    assert result.exit_code == 2
    assert "CONCOCT_GITHUB_TOKEN" in result.output


def test_cleanup_without_push_is_ignored(runner: CliRunner) -> None:
    result = generate(runner, "--cleanup")
    assert result.exit_code == 0
    assert "--cleanup only applies" in result.output
    assert any(Path("out").iterdir())


def test_config_show_never_prints_secrets(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-ant-api03-" + "q" * 40
    monkeypatch.setenv("CONCOCT_ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("CONCOCT_GITHUB_TOKEN", "ghp_" + "w" * 36)
    for args in (["config", "show"], ["config", "show", "--json"], ["status"]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert "qqqq" not in result.output and "wwww" not in result.output


def test_config_init_writes_private_template(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["config", "init"])
    assert result.exit_code == 0
    env = Path(".env")
    assert "CONCOCT_ANTHROPIC_API_KEY=" in env.read_text()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    again = runner.invoke(cli, ["config", "init"])
    assert again.exit_code == 2


def test_env_file_option(runner: CliRunner, tmp_path: Path) -> None:
    env = tmp_path / "alt.env"
    env.write_text("CONCOCT_REPOS=4\n")
    result = runner.invoke(cli, ["--env-file", str(env), "config", "show", "--json"])
    assert json.loads(result.output)["repos"] == 4
