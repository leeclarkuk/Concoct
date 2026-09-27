"""Command-line interface."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from concoct import __version__
from concoct.budget import Budget, MeteredProvider
from concoct.config import ENV_TEMPLATE, Settings, load_settings
from concoct.gitops.repository import GitRepository
from concoct.logging import configure_logging
from concoct.manifest import find_local_repositories, read_manifest
from concoct.models import Manifest
from concoct.orchestrator import BatchRunner, assign_repositories
from concoct.providers.pricing import price_for
from concoct.providers.registry import ProviderConfigError, create_provider
from concoct.secrets import redact
from concoct.ui import (
    RichReporter,
    commit_line,
    format_tokens,
    format_usage,
    plan_table,
    settings_panel,
    validation_table,
)

console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

RESPONSIBLE_USE = (
    "Concoct generates synthetic repositories for research, testing and demos. Do not "
    "present generated activity as genuine professional experience."
)


def _fail(message: str, code: int = EXIT_USAGE) -> None:
    err_console.print(f"[bold red]error:[/] {redact(message)}")
    sys.exit(code)


def _settings(ctx: click.Context, **overrides: Any) -> Settings:
    env_file = ctx.obj.get("env_file") if ctx.obj else None
    if env_file is not None:
        overrides["_env_file"] = env_file
    try:
        settings = load_settings(**overrides)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg']}" for e in exc.errors()
        )
        _fail(f"invalid configuration: {problems}")
        raise  # unreachable
    configure_logging(settings.log_level, settings.log_file)
    return settings


def _github(settings: Settings):  # -> GitHubService
    if settings.github_token is None:
        _fail("CONCOCT_GITHUB_TOKEN is not set (needed for GitHub operations)")
    from concoct.github.client import GitHubService

    assert settings.github_token is not None
    return GitHubService(settings.github_token.get_secret_value(), settings.github_owner)


# --------------------------------------------------------------------------- root
@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.version_option(__version__, prog_name="concoct")
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Read settings from this .env file instead of ./.env.",
)
@click.pass_context
def cli(ctx: click.Context, env_file: Path | None) -> None:
    """Concoct: generate realistic synthetic repositories with coherent Git histories.

    Local generation is the default; GitHub is only touched with explicit flags.
    """
    ctx.ensure_object(dict)
    ctx.obj["env_file"] = env_file


# ----------------------------------------------------------------------- generate
def _generate_options(func: Callable[..., Any]) -> Callable[..., Any]:
    options = [
        click.option(
            "-l",
            "--language",
            "languages",
            multiple=True,
            help="Language(s) to use; repeat for several. [default: python]",
        ),
        click.option(
            "-t",
            "--tech",
            "technologies",
            multiple=True,
            help="Framework/technology every project must use; repeatable.",
        ),
        click.option(
            "-c",
            "--category",
            "categories",
            multiple=True,
            help='Project category, e.g. "developer tool"; repeatable.',
        ),
        click.option("-r", "--repos", type=click.IntRange(1, 100), help="Number of repositories."),
        click.option(
            "--complexity",
            type=click.Choice(["low", "medium", "high", "random"]),
            help="Project size.",
        ),
        click.option(
            "-d",
            "--history-days",
            type=click.IntRange(1, 3650),
            help="How far back history may start.",
        ),
        click.option(
            "--end-date", type=click.DateTime(), help="Latest commit date (default: now)."
        ),
        click.option(
            "--min-commits", type=click.IntRange(2, 200), help="Minimum commits per repo."
        ),
        click.option(
            "--max-commits", type=click.IntRange(2, 200), help="Maximum commits per repo."
        ),
        click.option("--timezone", help="IANA timezone for commit times, e.g. Europe/London."),
        click.option("--author-name", help="Commit author name (default: a synthetic identity)."),
        click.option("--author-email", help="Commit author email."),
        click.option("--license", "license_", help="Add a licence, e.g. MIT or Apache-2.0."),
        click.option(
            "--visibility",
            type=click.Choice(["private", "public"]),
            help="GitHub visibility when pushing. [default: private]",
        ),
        click.option(
            "-o",
            "--output-dir",
            type=click.Path(file_okay=False, path_type=Path),
            help="Where repositories are written. [default: concoct-output]",
        ),
        click.option(
            "--provider",
            type=click.Choice(["anthropic", "offline"]),
            help="LLM provider. 'offline' uses a built-in demo template.",
        ),
        click.option("-m", "--model", help="Model identifier. [default: claude-opus-5]"),
        click.option(
            "--effort",
            type=click.Choice(["low", "medium", "high", "xhigh", "max"]),
            help="Reasoning effort for Claude.",
        ),
        click.option(
            "--max-cost",
            "max_cost_usd",
            type=click.FloatRange(0),
            help="Stop when estimated spend reaches this many USD. [default: 10]",
        ),
        click.option(
            "--max-tokens",
            "max_total_tokens",
            type=click.IntRange(1),
            help="Stop when this many tokens have been used.",
        ),
        click.option("--seed", type=int, help="Seed for reproducible choices and timestamps."),
        click.option(
            "--max-repairs",
            "max_repair_attempts",
            type=click.IntRange(0, 5),
            help="Repair attempts after failed validation. [default: 2]",
        ),
        click.option(
            "--run-commands/--no-run-commands",
            default=None,
            help="Install dependencies and run tests/lint during validation.",
        ),
        click.option(
            "--strict-lint",
            is_flag=True,
            default=None,
            help="Treat lint findings as validation failures.",
        ),
        click.option(
            "--disclosure/--no-disclosure",
            "synthetic_disclosure",
            default=None,
            help="Note in README that the repository is synthetic. [default: on]",
        ),
        click.option(
            "--push/--no-push",
            default=None,
            help="Create GitHub repositories and push. [default: --no-push]",
        ),
        click.option(
            "--cleanup",
            is_flag=True,
            default=None,
            help="Delete local copies after a successful push.",
        ),
        click.option(
            "--dry-run",
            is_flag=True,
            help="Show what would happen without calling the model or writing files.",
        ),
        click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation."),
        click.option(
            "-v", "--verbose", is_flag=True, help="Show architecture and failure details."
        ),
        click.option(
            "--log-level",
            type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
        ),
        click.option("--log-file", type=click.Path(dir_okay=False, path_type=Path)),
    ]
    for option in reversed(options):
        func = option(func)
    return func


@cli.command()
@_generate_options
@click.pass_context
def generate(
    ctx: click.Context,
    dry_run: bool,
    yes: bool,
    verbose: bool,
    license_: str | None,
    **options: Any,
) -> None:
    """Generate synthetic repositories with incremental, validated Git history.

    \b
    Examples:
      concoct generate -l python -t fastapi -c "developer tool" --no-push
      concoct generate -l go -l rust -r 3 --min-commits 10 --max-commits 25 --seed 7
      concoct generate --provider offline --repos 1          # no API key needed
    """
    settings = _settings(ctx, license=license_, **options)

    if settings.cleanup and not settings.push:
        err_console.print(
            "[yellow]warning:[/] --cleanup only applies after a successful push; "
            "local repositories will be kept."
        )
        settings.cleanup = False

    if dry_run:
        _dry_run(settings)
        return

    try:
        provider = create_provider(settings)
    except ProviderConfigError as exc:
        _fail(str(exc))
        return
    publisher = None
    if settings.push:
        publisher = _github(settings)
        try:
            owner = publisher.owner
        except Exception as exc:
            _fail(f"GitHub authentication failed: {exc}")
            return
        if not yes:
            console.print(
                Panel(
                    f"About to create [bold]{settings.repos}[/] new "
                    f"[bold]{settings.visibility}[/] "
                    f"repositor{'y' if settings.repos == 1 else 'ies'} "
                    f"under [bold]{owner}[/] and push generated history.\n"
                    "Existing repositories are never modified. Created repositories are tagged "
                    "'concoct-generated'.\n\n" + RESPONSIBLE_USE,
                    title="GitHub changes",
                    border_style="yellow",
                    expand=False,
                )
            )
            if not click.confirm("Continue?", default=False):
                _fail("aborted by user", EXIT_FAILED)

    budget = Budget(settings.max_cost_usd, settings.max_total_tokens)
    reporter = RichReporter(console, verbose=verbose)
    metered = MeteredProvider(provider, budget)
    if price_for(provider.model) is None:
        reporter.cost_known = False
        err_console.print(
            f"[yellow]warning:[/] no price data for model {provider.model!r}; "
            "only the token budget can be enforced."
        )
    runner = BatchRunner(settings, metered, reporter, publisher=publisher)
    try:
        batch = runner.run()
    except KeyboardInterrupt:
        reporter.progress.stop()
        _fail("interrupted; partial repositories keep their manifest for inspection", EXIT_FAILED)
        return
    sys.exit(EXIT_OK if batch.succeeded else EXIT_FAILED)


def _dry_run(settings: Settings) -> None:
    console.print(settings_panel(settings.public_dict(), title="Concoct — dry run"))
    table = Table(title="Planned repositories", title_justify="left")
    for column in ("#", "Language", "Category", "Complexity", "Commits"):
        table.add_column(column)
    for a in assign_repositories(settings):
        table.add_row(
            str(a.index + 1), a.language, a.category, a.complexity.value, str(a.commit_count)
        )
    console.print(table)
    key = "set" if settings.anthropic_api_key else "[red]missing[/]"
    notes = [
        "No model calls, files or GitHub changes were made.",
        f"Provider: {settings.provider} "
        f"(API key {key if settings.provider == 'anthropic' else 'n/a'})",
    ]
    if settings.push:
        notes.append(
            f"Would create {settings.repos} {settings.visibility} GitHub repositor"
            f"{'y' if settings.repos == 1 else 'ies'} and push history"
            + (", then delete local copies." if settings.cleanup else ".")
        )
    else:
        notes.append(f"Would write repositories to {settings.output_dir}/ (no push).")
    console.print("\n".join(notes))


# ------------------------------------------------------------------------- status
@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show configuration and connectivity (read-only)."""
    settings = _settings(ctx)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Version", __version__)
    table.add_row("Provider", f"{settings.provider} · {settings.model}")
    table.add_row(
        "Anthropic key",
        "[green]configured[/]" if settings.anthropic_api_key else "[yellow]not set[/]",
    )
    table.add_row(
        "GitHub token",
        "[green]configured[/]"
        if settings.github_token
        else "[bright_black]not set (local only)[/]",
    )
    table.add_row("Output dir", str(settings.output_dir))
    local = find_local_repositories(settings.output_dir)
    table.add_row("Local repos", str(len(local)))
    table.add_row("git", shutil.which("git") or "[red]not found[/]")
    table.add_row("uv", shutil.which("uv") or "[yellow]not found (validation uses venv/pip)[/]")
    if settings.github_token:
        try:
            info = _github(settings).status()
            table.add_row("GitHub user", f"{info['login']} (owner: {info['owner']})")
            table.add_row("Token scopes", ", ".join(info["scopes"]) or "fine-grained / n/a")
            table.add_row("Rate limit", str(info["rate_remaining"]))
        except Exception as exc:
            table.add_row("GitHub", f"[red]{redact(str(exc))}[/]")
    console.print(Panel(table, title="[bold]Concoct status", border_style="blue", expand=False))


# --------------------------------------------------------------------- list-repos
@cli.command("list-repos")
@click.option("--remote", is_flag=True, help="List Concoct-created repositories on GitHub.")
@click.option("-o", "--output-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def list_repos(ctx: click.Context, remote: bool, output_dir: Path | None, as_json: bool) -> None:
    """List repositories generated by Concoct (local by default)."""
    settings = _settings(ctx, output_dir=output_dir)
    if remote:
        service = _github(settings)
        try:
            repos = service.list_generated()
        except Exception as exc:
            _fail(str(exc), EXIT_FAILED)
            return
        if as_json:
            click.echo(json.dumps([r.__dict__ for r in repos], default=str, indent=2))
            return
        table = Table(
            title=f"Concoct repositories on GitHub ({service.owner})", title_justify="left"
        )
        for column in ("Repository", "Visibility", "Created", "Description"):
            table.add_column(column)
        for r in repos:
            table.add_row(
                r.full_name,
                "private" if r.private else "public",
                f"{r.created_at:%Y-%m-%d}" if r.created_at else "–",
                r.description,
            )
        console.print(table if repos else "No Concoct-created repositories found on GitHub.")
        return

    local = find_local_repositories(settings.output_dir)
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "path": str(p),
                        "name": m.result.name,
                        "status": m.result.status,
                        "commits": len(m.result.commits),
                        "remote": m.result.remote.html_url if m.result.remote else None,
                    }
                    for p, m in local
                ],
                indent=2,
            )
        )
        return
    if not local:
        console.print(f"No Concoct repositories in {settings.output_dir}/")
        return
    table = Table(title=f"Local repositories in {settings.output_dir}/", title_justify="left")
    for column in ("Repository", "Status", "Language", "Commits", "Created", "Remote"):
        table.add_column(column)
    for path, m in local:
        r = m.result
        table.add_row(
            path.name,
            str(r.status.value),
            r.spec.language if r.spec else "–",
            str(len(r.commits)),
            f"{m.created_at:%Y-%m-%d %H:%M}",
            r.remote.html_url if r.remote else "–",
        )
    console.print(table)


# ------------------------------------------------------------------------ inspect
def _resolve_local(settings: Settings, target: str) -> tuple[Path, Manifest]:
    candidates = [Path(target), Path(settings.output_dir) / target]
    for candidate in candidates:
        manifest = read_manifest(candidate)
        if manifest is not None:
            return candidate, manifest
    _fail(f"{target!r} is not a Concoct repository (looked in {', '.join(map(str, candidates))})")
    raise AssertionError  # unreachable


@cli.command()
@click.argument("target")
@click.option("-o", "--output-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Print the manifest as JSON.")
@click.option("--plan/--no-plan", "show_plan", default=True, help="Show the development plan.")
@click.pass_context
def inspect(
    ctx: click.Context, target: str, output_dir: Path | None, as_json: bool, show_plan: bool
) -> None:
    """Show plan, history and validation of a generated repository (path or name)."""
    settings = _settings(ctx, output_dir=output_dir)
    path, manifest = _resolve_local(settings, target)
    if as_json:
        click.echo(manifest.model_dump_json(indent=2))
        return
    r = manifest.result
    spec = r.spec
    header = [
        f"[bold cyan]{r.name}[/]  {spec.description if spec else ''}",
        f"status: {r.status.value} · generated {manifest.created_at:%Y-%m-%d %H:%M} UTC "
        f"with {manifest.provider}/{manifest.model} · seed {manifest.seed}",
        f"path: {path}",
    ]
    if spec:
        header.append(
            f"{spec.language} · {', '.join(spec.technologies) or 'default stack'} · "
            f"{spec.category} · {spec.complexity.value}"
        )
    if r.remote:
        header.append(f"remote: {r.remote.html_url}")
    if r.error:
        header.append(f"[red]{r.error}[/]")
    console.print(Panel("\n".join(header), border_style="cyan", expand=False))
    if spec and spec.architecture:
        console.print(Panel(spec.architecture, title="Architecture", expand=False))
    if show_plan and r.plan:
        console.print(plan_table(r.plan))

    console.print("[bold]History[/] (from git)")
    try:
        history = GitRepository.open(path).history()
    except Exception as exc:
        history = []
        console.print(f"[red]could not read git history: {exc}[/]")
    recorded = {c.sha: c for c in r.commits}
    for entry in history:
        record = recorded.get(str(entry["sha"]))
        if record is not None:
            console.print(commit_line(record), no_wrap=True, overflow="ellipsis")
        else:
            console.print(f"  {str(entry['sha'])[:8]}  {entry['subject']}")
    if history:
        authors = sorted({str(e["author"]) for e in history})
        console.print(
            f"  [bright_black]{len(history)} commits · author(s): {', '.join(authors)}[/]"
        )
    if r.validation:
        console.print(validation_table(r.validation))
    console.print(f"usage: {format_usage(r.usage)} ({format_tokens(r.usage.total_tokens)} tokens)")


# ------------------------------------------------------------------------- delete
@cli.command()
@click.argument("names", nargs=-1, required=True)
@click.option(
    "--local/--no-local", "delete_local", default=True, help="Delete local copies. [default: on]"
)
@click.option(
    "--remote",
    "delete_remote",
    is_flag=True,
    help="Also delete the GitHub repositories (Concoct-created ones only).",
)
@click.option("-o", "--output-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Show what would be deleted.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def delete(
    ctx: click.Context,
    names: tuple[str, ...],
    delete_local: bool,
    delete_remote: bool,
    output_dir: Path | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Delete Concoct-generated repositories.

    Only repositories with a Concoct manifest (local) or the 'concoct-generated'
    topic (GitHub) can be deleted. A preview is always shown first.
    """
    settings = _settings(ctx, output_dir=output_dir)
    local_targets: list[Path] = []
    remote_targets: list[str] = []
    problems: list[str] = []

    if delete_local:
        for name in names:
            path = Path(settings.output_dir) / name
            if read_manifest(path) is not None:
                local_targets.append(path)
            elif path.exists():
                problems.append(f"{path} has no Concoct manifest; refusing to delete it")
            elif not delete_remote:
                problems.append(f"{path} does not exist")

    service = None
    if delete_remote:
        service = _github(settings)
        for name in names:
            try:
                remote_targets.append(service.get_generated(name).full_name)
            except Exception as exc:
                problems.append(str(exc))

    table = Table(title="Deletion preview", title_justify="left")
    table.add_column("Where")
    table.add_column("Target")
    for path in local_targets:
        table.add_row("local", str(path))
    for full_name in remote_targets:
        table.add_row("[bold red]GitHub[/]", full_name)
    if local_targets or remote_targets:
        console.print(table)
    for problem in problems:
        err_console.print(f"[yellow]skipping:[/] {problem}")
    if not local_targets and not remote_targets:
        _fail("nothing to delete", EXIT_FAILED)
    if dry_run:
        console.print("Dry run: nothing was deleted.")
        return
    if not yes:
        warning = " GitHub deletions cannot be undone." if remote_targets else ""
        if not click.confirm(
            f"Delete {len(local_targets) + len(remote_targets)} target(s)?{warning}", default=False
        ):
            _fail("aborted by user", EXIT_FAILED)

    failed = False
    for full_name in remote_targets:
        assert service is not None
        try:
            service.delete(full_name.split("/", 1)[1])
            console.print(f"[red]deleted[/] {full_name}")
        except Exception as exc:
            failed = True
            err_console.print(f"[red]failed[/] {full_name}: {redact(str(exc))}")
    for path in local_targets:
        shutil.rmtree(path)
        console.print(f"[red]deleted[/] {path}")
    sys.exit(EXIT_FAILED if failed or problems else EXIT_OK)


# ------------------------------------------------------------------------- config
@cli.group()
def config() -> None:
    """Show or initialise configuration."""


@config.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def config_show(ctx: click.Context, as_json: bool) -> None:
    """Print the effective configuration (secrets are never shown)."""
    settings = _settings(ctx)
    data = settings.public_dict()
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return
    table = Table(title="Effective configuration", title_justify="left")
    table.add_column("Setting", style="bold")
    table.add_column("Environment variable", style="bright_black")
    table.add_column("Value")
    for key, value in data.items():
        env = "CONCOCT_" + key.upper().removesuffix("_SET")
        table.add_row(key, env, json.dumps(value, default=str))
    console.print(table)
    console.print("[bright_black]Precedence: CLI options > environment > .env > defaults.[/]")


@config.command("init")
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".env"),
    show_default=True,
)
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
def config_init(path: Path, force: bool) -> None:
    """Write a commented .env template."""
    if path.exists() and not force:
        _fail(f"{path} already exists (use --force to overwrite)")
    path.write_text(ENV_TEMPLATE, encoding="utf-8")
    path.chmod(0o600)
    console.print(f"Wrote {path}. Fill in your API key; never commit this file.")


def main() -> None:
    cli(prog_name="concoct")
