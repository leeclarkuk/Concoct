"""Rich terminal presentation. Implements :class:`concoct.events.Reporter`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from concoct.events import Reporter
from concoct.models import (
    CheckStatus,
    CommitKind,
    CommitRecord,
    DevelopmentPlan,
    RepositoryResult,
    RepositoryStatus,
    UsageSummary,
    ValidationReport,
)

KIND_STYLES: dict[CommitKind, str] = {
    CommitKind.SCAFFOLD: "bold magenta",
    CommitKind.FEATURE: "green",
    CommitKind.TEST: "cyan",
    CommitKind.REFACTOR: "yellow",
    CommitKind.FIX: "red",
    CommitKind.DOCS: "blue",
    CommitKind.CONFIG: "bright_black",
    CommitKind.DEPS: "bright_black",
    CommitKind.CI: "bright_blue",
    CommitKind.PERF: "bright_green",
    CommitKind.CHORE: "bright_black",
}
CHECK_ICONS: dict[CheckStatus, str] = {
    CheckStatus.PASSED: "[green]✔[/]",
    CheckStatus.FAILED: "[red]✘[/]",
    CheckStatus.WARNING: "[yellow]▲[/]",
    CheckStatus.SKIPPED: "[bright_black]–[/]",
}
STATUS_STYLES: dict[RepositoryStatus, str] = {
    RepositoryStatus.SUCCEEDED: "[green]succeeded[/]",
    RepositoryStatus.VALIDATION_FAILED: "[yellow]validation failed[/]",
    RepositoryStatus.FAILED: "[red]failed[/]",
    RepositoryStatus.BUDGET_EXCEEDED: "[red]budget exceeded[/]",
}


def format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def format_usage(usage: UsageSummary, cost_known: bool = True) -> str:
    cost = f"${usage.cost_usd:.2f}" if cost_known else "cost n/a"
    return (
        f"{usage.calls} calls · {format_tokens(usage.input_tokens)} in · "
        f"{format_tokens(usage.output_tokens)} out"
        + (f" · {format_tokens(usage.cache_read_tokens)} cached" if usage.cache_read_tokens else "")
        + f" · {cost}"
    )


def format_when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M %z")


def settings_panel(summary: dict[str, Any], title: str = "Concoct") -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    def join(values: Any) -> str:
        return ", ".join(values) if values else "[bright_black]any[/]"

    budget = []
    if summary.get("max_cost_usd") is not None:
        budget.append(f"${summary['max_cost_usd']:.2f}")
    if summary.get("max_total_tokens"):
        budget.append(f"{format_tokens(summary['max_total_tokens'])} tokens")
    rows = [
        ("Repositories", str(summary["repos"])),
        ("Languages", join(summary["languages"])),
        ("Technologies", join(summary["technologies"])),
        ("Categories", join(summary["categories"])),
        ("Complexity", str(summary["complexity"])),
        ("Commits", f"{summary['min_commits']}–{summary['max_commits']} per repository"),
        ("History", f"{summary['history_days']} days, {summary['timezone']}"),
        (
            "Provider",
            "offline (built-in template)"
            if summary["provider"] == "offline"
            else f"{summary['provider']} · {summary['model']} · effort {summary['effort']}",
        ),
        ("Budget", " / ".join(budget) or "unlimited"),
        ("Repairs", f"up to {summary['max_repair_attempts']} per repository"),
        ("Output", str(summary["output_dir"])),
        (
            "Publish",
            f"[bold yellow]push to GitHub ({summary['visibility']})[/]"
            if summary["push"]
            else "local only",
        ),
        ("Seed", str(summary["seed"])),
    ]
    for key, value in rows:
        table.add_row(key, value)
    return Panel(table, title=f"[bold]{title}[/]", border_style="blue", expand=False)


def plan_table(plan: DevelopmentPlan, schedule: list[datetime] | None = None) -> Table:
    table = Table(title="Development plan", title_justify="left", show_lines=False)
    table.add_column("#", justify="right", style="bright_black")
    if schedule:
        table.add_column("When", style="bright_black", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Commit message")
    for i, step in enumerate(plan.commits):
        row = [str(step.index + 1)]
        if schedule:
            row.append(format_when(schedule[i]))
        row += [f"[{KIND_STYLES.get(step.kind, '')}]{step.kind.value}[/]", step.subject]
        table.add_row(*row)
    return table


def validation_table(report: ValidationReport) -> Table:
    title = "Validation" + (f" (after repair {report.attempt})" if report.attempt else "")
    table = Table(title=title, title_justify="left")
    table.add_column("", width=1)
    table.add_column("Check", style="bold")
    table.add_column("Result")
    for check in report.checks:
        table.add_row(CHECK_ICONS[check.status], check.name, check.summary)
    return table


def commit_line(record: CommitRecord) -> Text:
    style = KIND_STYLES.get(record.kind, "")
    text = Text("  ")
    text.append("✔ " if not record.repair else "⚒ ", style="green" if not record.repair else "red")
    text.append(record.sha[:8], style="bold yellow")
    text.append(f"  {format_when(record.authored_at)}  ", style="bright_black")
    text.append(record.subject, style=style)
    text.append(
        f"  (+{record.insertions} −{record.deletions}, {len(record.files_changed)} files)",
        style="bright_black",
    )
    return text


def summary_table(results: list[RepositoryResult]) -> Table:
    table = Table(title="Summary", title_justify="left")
    table.add_column("Repository", style="bold cyan")
    table.add_column("Status")
    table.add_column("Commits", justify="right")
    table.add_column("Validation")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Location")
    for r in results:
        if r.validation is None:
            validation = "–"
        else:
            passed = sum(1 for c in r.validation.checks if c.status == CheckStatus.PASSED)
            validation = f"{passed}/{len(r.validation.checks)} passed"
            if r.repair_attempts:
                validation += f", {r.repair_attempts} repair(s)"
        location = r.remote.html_url if r.remote else (r.path or "–")
        table.add_row(
            r.name,
            STATUS_STYLES[r.status],
            str(len(r.commits)),
            validation,
            format_tokens(r.usage.total_tokens),
            f"${r.usage.cost_usd:.2f}",
            location,
        )
    return table


class RichReporter(Reporter):
    """Live progress for ``concoct generate``."""

    def __init__(self, console: Console | None = None, *, verbose: bool = False) -> None:
        self.console = console or Console()
        self.verbose = verbose
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=24),
            MofNCompleteColumn(),
            TextColumn("[bright_black]{task.fields[usage]}"),
            TimeElapsedColumn(),
            console=self.console,
            transient=True,
        )
        self._task: TaskID | None = None
        self._usage = ""
        self.cost_known = True

    # ------------------------------------------------------------------
    def run_started(self, total_repos: int, summary: dict[str, object]) -> None:
        self.console.print(settings_panel(summary))
        self.progress.start()

    def repo_started(self, index: int, total: int, description: str) -> None:
        self.console.rule(f"[bold]Repository {index + 1}/{total}[/] [bright_black]{description}")
        if self._task is not None:
            self.progress.remove_task(self._task)
        self._task = self.progress.add_task("planning", total=None, usage=self._usage)

    def phase(self, index: int, phase: str, detail: str = "") -> None:
        if self._task is not None:
            label = phase if not detail else f"{phase} [bright_black]{detail[:60]}[/]"
            self.progress.update(self._task, description=label)

    def plan_ready(self, index: int, plan: DevelopmentPlan, schedule: list[datetime]) -> None:
        spec = plan.spec
        header = Text.assemble(
            (spec.name, "bold cyan"),
            "  ",
            (spec.description, ""),
            "\n",
            (
                f"{spec.language} · {', '.join(spec.technologies) or 'default stack'} · "
                f"{spec.category} · {spec.complexity.value}",
                "bright_black",
            ),
        )
        body: list[Any] = [header]
        if spec.architecture and self.verbose:
            body.append(Text(spec.architecture, style="italic"))
        self.console.print(Panel(Group(*body), border_style="cyan", expand=False))
        self.console.print(plan_table(plan, schedule))
        if self._task is not None:
            self.progress.update(self._task, total=len(plan.commits), completed=0)

    def commit_created(self, index: int, record: CommitRecord, planned_total: int) -> None:
        self.console.print(commit_line(record), no_wrap=True, overflow="ellipsis")
        if self._task is not None and not record.repair:
            self.progress.advance(self._task)

    def validation_finished(self, index: int, report: ValidationReport) -> None:
        self.console.print(validation_table(report))
        if self.verbose:
            for check in report.failures:
                if check.details:
                    self.console.print(Panel(check.details, title=check.name, border_style="red"))

    def repair_started(self, index: int, attempt: int, max_attempts: int) -> None:
        self.console.print(
            f"  [yellow]Validation failed — requesting repair {attempt}/{max_attempts}[/]"
        )

    def usage_updated(self, total: UsageSummary) -> None:
        self._usage = format_usage(total, self.cost_known)
        if self._task is not None:
            self.progress.update(self._task, usage=self._usage)

    def repo_finished(self, result: RepositoryResult) -> None:
        if self._task is not None:
            self.progress.remove_task(self._task)
            self._task = None
        lines = [f"{STATUS_STYLES[result.status]}  [bold]{result.name}[/]"]
        if result.commits:
            first, last = result.commits[0].authored_at, result.commits[-1].authored_at
            lines.append(f"{len(result.commits)} commits from {first:%Y-%m-%d} to {last:%Y-%m-%d}")
        lines.append(f"usage: {format_usage(result.usage, self.cost_known)}")
        if result.path:
            lines.append(f"local: {result.path}")
        if result.remote:
            lines.append(f"remote: {result.remote.html_url} ({result.remote.visibility})")
        if result.error:
            lines.append(f"[red]{result.error}[/]")
        style = "green" if result.status == RepositoryStatus.SUCCEEDED else "red"
        self.console.print(Panel("\n".join(lines), border_style=style, expand=False))

    def warning(self, message: str) -> None:
        self.console.print(f"[yellow]warning:[/] {message}")

    def run_finished(self, results: list[RepositoryResult], total: UsageSummary) -> None:
        self.progress.stop()
        self.console.print(summary_table(results))
        ok = sum(1 for r in results if r.status == RepositoryStatus.SUCCEEDED)
        self.console.print(
            f"[bold]{ok}/{len(results)}[/] repositories succeeded · "
            f"total usage: {format_usage(total, self.cost_known)}"
        )
