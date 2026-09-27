"""Progress reporting interface.

The orchestrator reports progress through this interface only, so pipeline
code never depends on Rich (or any UI) and tests can capture events.
"""

from __future__ import annotations

from datetime import datetime

from concoct.models import (
    CommitRecord,
    DevelopmentPlan,
    RepositoryResult,
    UsageSummary,
    ValidationReport,
)


class Reporter:
    """Base reporter: every hook is a no-op. Subclass and override what you need."""

    def run_started(self, total_repos: int, summary: dict[str, object]) -> None: ...

    def repo_started(self, index: int, total: int, description: str) -> None: ...

    def phase(self, index: int, phase: str, detail: str = "") -> None: ...

    def plan_ready(self, index: int, plan: DevelopmentPlan, schedule: list[datetime]) -> None: ...

    def commit_created(self, index: int, record: CommitRecord, planned_total: int) -> None: ...

    def validation_finished(self, index: int, report: ValidationReport) -> None: ...

    def repair_started(self, index: int, attempt: int, max_attempts: int) -> None: ...

    def usage_updated(self, total: UsageSummary) -> None: ...

    def repo_finished(self, result: RepositoryResult) -> None: ...

    def warning(self, message: str) -> None: ...

    def run_finished(self, results: list[RepositoryResult], total: UsageSummary) -> None: ...


class RecordingReporter(Reporter):
    """Collects events as ``(name, payload)`` tuples; handy for tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def phase(self, index: int, phase: str, detail: str = "") -> None:
        self.events.append(("phase", (index, phase, detail)))

    def plan_ready(self, index: int, plan: DevelopmentPlan, schedule: list[datetime]) -> None:
        self.events.append(("plan", plan))

    def commit_created(self, index: int, record: CommitRecord, planned_total: int) -> None:
        self.events.append(("commit", record))

    def validation_finished(self, index: int, report: ValidationReport) -> None:
        self.events.append(("validation", report))

    def repair_started(self, index: int, attempt: int, max_attempts: int) -> None:
        self.events.append(("repair", attempt))

    def repo_finished(self, result: RepositoryResult) -> None:
        self.events.append(("repo_finished", result))

    def warning(self, message: str) -> None:
        self.events.append(("warning", message))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]
