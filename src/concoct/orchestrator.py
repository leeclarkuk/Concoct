"""The generation pipeline: plan → incremental commits → validate → repair → publish."""

from __future__ import annotations

import random
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from concoct import __version__
from concoct.budget import BudgetExceededError, MeteredProvider
from concoct.config import Settings
from concoct.events import Reporter
from concoct.generation.commits import CommitGenerator, Draft, GenerationError
from concoct.gitops.repository import (
    SYNTHETIC_IDENTITY_EMAIL,
    SYNTHETIC_IDENTITY_NAME,
    GitOperationError,
    GitRepository,
    Identity,
)
from concoct.logging import get_logger
from concoct.manifest import find_local_repositories, read_manifest, write_manifest
from concoct.models import (
    CommitKind,
    Complexity,
    DevelopmentPlan,
    FileChange,
    Manifest,
    ProjectSpec,
    RemoteInfo,
    RepositoryResult,
    RepositoryStatus,
    UsageSummary,
    ValidationReport,
)
from concoct.planning.planner import Planner, PlanningError
from concoct.planning.prompts import PlanRequest
from concoct.planning.schedule import commit_schedule, follow_up_timestamp
from concoct.providers.base import ProviderError
from concoct.rng import derive_rng
from concoct.secrets import redact
from concoct.validation.runner import Validator

log = get_logger("orchestrator")

DEFAULT_CATEGORIES = (
    "developer tool",
    "command-line utility",
    "web API",
    "library",
    "data processing pipeline",
    "automation script",
    "dashboard",
    "testing utility",
)

DISCLOSURE_MARKER = "synthetically generated"
DISCLOSURE = (
    "\n---\n\n"
    "_This repository was synthetically generated with Concoct for research, testing or "
    "demonstration purposes. Its commit history does not represent real development work._\n"
)


class Publisher(Protocol):
    """Anything that can publish a finished repository (e.g. GitHub)."""

    def existing_names(self) -> set[str]: ...

    def publish(self, repo: GitRepository, spec: ProjectSpec, visibility: str) -> RemoteInfo: ...


@dataclass
class RepositoryAssignment:
    """Everything decided up-front (deterministically) for one repository."""

    index: int
    language: str
    category: str
    technologies: list[str]
    complexity: Complexity
    commit_count: int
    rng: random.Random = field(repr=False)


@dataclass
class BatchResult:
    results: list[RepositoryResult]
    usage: UsageSummary

    @property
    def succeeded(self) -> bool:
        return bool(self.results) and all(
            r.status == RepositoryStatus.SUCCEEDED for r in self.results
        )


def assign_repositories(settings: Settings) -> list[RepositoryAssignment]:
    """Decide language, category, complexity and commit count for every repo."""
    seed = settings.resolved_seed
    reserve = min(settings.max_repair_attempts, settings.max_commits - settings.min_commits)
    planned_max = max(settings.min_commits, settings.max_commits - reserve)
    assignments = []
    for index in range(settings.repos):
        rng = derive_rng(seed, "repo", index)
        categories = settings.categories or list(DEFAULT_CATEGORIES)
        if settings.complexity == "random":
            complexity = rng.choices(
                [Complexity.LOW, Complexity.MEDIUM, Complexity.HIGH], weights=[3, 5, 2]
            )[0]
        else:
            complexity = Complexity(settings.complexity)
        assignments.append(
            RepositoryAssignment(
                index=index,
                language=settings.languages[index % len(settings.languages)],
                category=rng.choice(categories),
                technologies=list(settings.technologies),
                complexity=complexity,
                commit_count=rng.randint(settings.min_commits, planned_max),
                rng=rng,
            )
        )
    return assignments


def ensure_disclosure(changes: list[FileChange], files: dict[str, str], spec: ProjectSpec) -> None:
    """Keep the synthetic-origin notice in README.md (mutates ``changes``)."""
    for change in changes:
        if change.path.lower() == "readme.md" and change.action == "write":
            if DISCLOSURE_MARKER not in (change.content or "").lower():
                change.content = (change.content or "").rstrip("\n") + "\n" + DISCLOSURE
            return
    if not any(p.lower() == "readme.md" for p in files):
        changes.append(
            FileChange(
                path="README.md",
                content=f"# {spec.title or spec.name}\n\n{spec.description}\n{DISCLOSURE}",
            )
        )


class RepositoryBuilder:
    """Builds one repository end to end."""

    def __init__(
        self,
        settings: Settings,
        provider: MeteredProvider,
        reporter: Reporter,
        validator: Validator,
        *,
        author: Identity,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.reporter = reporter
        self.validator = validator
        self.author = author
        self.planner = Planner(provider)
        self.generator = CommitGenerator(provider)

    def build(
        self, assignment: RepositoryAssignment, existing_names: set[str]
    ) -> tuple[RepositoryResult, GitRepository | None]:
        result = RepositoryResult(index=assignment.index, status=RepositoryStatus.FAILED)

        def track(entry: UsageSummary) -> None:
            result.usage.add(entry)
            self.reporter.usage_updated(self.provider.budget.total)

        self.provider.add_listener(track)
        repo: GitRepository | None = None
        try:
            plan = self._plan(assignment, existing_names)
            result.spec, result.plan = plan.spec, plan
            schedule = commit_schedule(
                assignment.rng,
                len(plan.commits),
                end=self.settings.history_end(),
                history_days=self.settings.history_days,
                zone=self.settings.zone,
            )
            self.reporter.plan_ready(assignment.index, plan, schedule)

            path = self._allocate_path(plan.spec.name)
            if path.name != plan.spec.name:
                plan.spec.name = path.name
            repo = GitRepository.create(path)
            result.path = str(path)

            self._build_history(assignment, plan, schedule, repo, result)
            report = self._validate_and_repair(assignment, plan, repo, result)
            result.validation = report
            result.status = (
                RepositoryStatus.SUCCEEDED if report.passed else RepositoryStatus.VALIDATION_FAILED
            )
            if not report.passed:
                result.error = "validation failed: " + "; ".join(
                    f"{c.name}: {c.summary}" for c in report.failures
                )
        except BudgetExceededError as exc:
            result.status = RepositoryStatus.BUDGET_EXCEEDED
            result.error = str(exc)
        except (PlanningError, GenerationError, ProviderError, GitOperationError) as exc:
            result.status = RepositoryStatus.FAILED
            result.error = redact(str(exc))
        finally:
            self.provider.remove_listener(track)

        if repo is not None:
            self._write_manifest(repo, result, assignment)
        return result, repo

    # ------------------------------------------------------------------ steps
    def _plan(self, a: RepositoryAssignment, existing_names: set[str]) -> DevelopmentPlan:
        self.reporter.phase(a.index, "planning", f"{a.language} · {a.category}")
        reserve = min(
            self.settings.max_repair_attempts, self.settings.max_commits - self.settings.min_commits
        )
        request = PlanRequest(
            language=a.language,
            technologies=a.technologies,
            category=a.category,
            complexity=a.complexity,
            commit_count=a.commit_count,
            min_commits=self.settings.min_commits,
            max_commits=max(self.settings.min_commits, self.settings.max_commits - reserve),
            license=self.settings.license,
            existing_names=tuple(sorted(existing_names)),
            synthetic_disclosure=self.settings.synthetic_disclosure,
        )
        return self.planner.plan(request)

    def _allocate_path(self, name: str) -> Path:
        base = Path(self.settings.output_dir)
        candidate, suffix = base / name, 2
        while candidate.exists():
            candidate = base / f"{name}-{suffix}"
            suffix += 1
        return candidate

    def _build_history(
        self,
        a: RepositoryAssignment,
        plan: DevelopmentPlan,
        schedule: list[datetime],
        repo: GitRepository,
        result: RepositoryResult,
    ) -> None:
        total = len(plan.commits)
        for step, when in zip(plan.commits, schedule, strict=True):
            self.reporter.phase(a.index, f"commit {step.index + 1}/{total}", step.subject)
            files = repo.snapshot()
            draft = self.generator.implement(plan, step, files)
            record = self._commit(
                repo, draft, files, plan.spec, when, step.kind, index=len(result.commits)
            )
            result.commits.append(record)
            self.reporter.commit_created(a.index, record, total)

    def _commit(
        self,
        repo: GitRepository,
        draft: Draft,
        files: dict[str, str],
        spec: ProjectSpec,
        when: datetime,
        kind: CommitKind,
        *,
        index: int,
        repair: bool = False,
    ):
        if self.settings.synthetic_disclosure:
            ensure_disclosure(draft.changes, files, spec)
        changed = repo.apply(draft.changes)
        if not changed:
            raise GenerationError(f"commit {index + 1} produced no effective changes")
        return repo.commit(
            draft.message, author=self.author, when=when, kind=kind, index=index, repair=repair
        )

    def _validate_and_repair(
        self,
        a: RepositoryAssignment,
        plan: DevelopmentPlan,
        repo: GitRepository,
        result: RepositoryResult,
    ) -> ValidationReport:
        self.reporter.phase(a.index, "validating", "")
        report = self.validator.validate(repo, plan.spec, attempt=0)
        self.reporter.validation_finished(a.index, report)
        attempt = 0
        while not report.passed and attempt < self.settings.max_repair_attempts:
            attempt += 1
            result.repair_attempts = attempt
            self.reporter.repair_started(a.index, attempt, self.settings.max_repair_attempts)
            self.reporter.phase(
                a.index,
                f"repair {attempt}/{self.settings.max_repair_attempts}",
                report.failures[0].name if report.failures else "",
            )
            files = repo.snapshot()
            try:
                draft = self.generator.repair(plan, files, report.diagnostics())
            except GenerationError as exc:
                self.reporter.warning(f"repair attempt {attempt} produced no usable fix: {exc}")
                continue
            when = follow_up_timestamp(
                a.rng, result.commits[-1].authored_at, self.settings.history_end()
            )
            record = self._commit(
                repo,
                draft,
                files,
                plan.spec,
                when,
                CommitKind.FIX,
                index=len(result.commits),
                repair=True,
            )
            result.commits.append(record)
            self.reporter.commit_created(a.index, record, len(plan.commits))
            self.reporter.phase(a.index, "validating", f"after repair {attempt}")
            report = self.validator.validate(repo, plan.spec, attempt=attempt)
            self.reporter.validation_finished(a.index, report)
        return report

    def _write_manifest(
        self, repo: GitRepository, result: RepositoryResult, a: RepositoryAssignment
    ) -> None:
        manifest = Manifest(
            concoct_version=__version__,
            created_at=datetime.now(UTC),
            seed=self.settings.resolved_seed,
            repo_index=a.index,
            provider=self.provider.name,
            model=self.provider.model,
            options=self.settings.public_dict(),
            result=result,
        )
        write_manifest(repo.root, manifest)


class BatchRunner:
    """Generates every requested repository, then optionally publishes them."""

    def __init__(
        self,
        settings: Settings,
        provider: MeteredProvider,
        reporter: Reporter | None = None,
        *,
        validator: Validator | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.reporter = reporter or Reporter()
        self.validator = validator or Validator(
            run_commands=settings.run_commands,
            command_timeout=settings.command_timeout,
            strict_lint=settings.strict_lint,
        )
        self.publisher = publisher
        if settings.push and publisher is None:
            raise ValueError("push requested but no publisher configured")

    def author(self) -> Identity:
        return Identity(
            self.settings.author_name or SYNTHETIC_IDENTITY_NAME,
            self.settings.author_email or SYNTHETIC_IDENTITY_EMAIL,
        )

    def run(self) -> BatchResult:
        settings = self.settings
        assignments = assign_repositories(settings)
        self.reporter.run_started(len(assignments), settings.public_dict())
        Path(settings.output_dir).mkdir(parents=True, exist_ok=True)

        existing = {path.name for path, _ in find_local_repositories(settings.output_dir)}
        existing |= {p.name for p in Path(settings.output_dir).iterdir() if p.is_dir()}
        if self.publisher is not None:
            existing |= self.publisher.existing_names()

        builder = RepositoryBuilder(
            settings, self.provider, self.reporter, self.validator, author=self.author()
        )
        results: list[RepositoryResult] = []
        for assignment in assignments:
            self.reporter.repo_started(
                assignment.index,
                len(assignments),
                f"{assignment.language} · {assignment.category} · "
                f"{assignment.complexity.value} · {assignment.commit_count} planned commits",
            )
            result, repo = builder.build(assignment, existing)
            if result.spec is not None:
                existing.add(result.spec.name)
            if repo is not None and result.status == RepositoryStatus.SUCCEEDED:
                self._publish(result, repo, assignment)
            self.reporter.repo_finished(result)
            results.append(result)
            if result.status == RepositoryStatus.BUDGET_EXCEEDED:
                self.reporter.warning("budget exhausted; remaining repositories skipped")
                break

        batch = BatchResult(results=results, usage=self.provider.budget.total)
        self.reporter.run_finished(results, batch.usage)
        return batch

    def _publish(
        self, result: RepositoryResult, repo: GitRepository, a: RepositoryAssignment
    ) -> None:
        if not self.settings.push or self.publisher is None or result.spec is None:
            return
        self.reporter.phase(a.index, "publishing", result.spec.name)
        try:
            result.remote = self.publisher.publish(repo, result.spec, self.settings.visibility)
        except Exception as exc:  # publishing failure must not lose the local repo
            result.status = RepositoryStatus.FAILED
            result.error = f"publish failed: {redact(str(exc))}"
            write_manifest(repo.root, _refresh(repo, result))
            return
        manifest = _refresh(repo, result)
        write_manifest(repo.root, manifest)
        if self.settings.cleanup:
            shutil.rmtree(repo.root)
            result.path = None


def _refresh(repo: GitRepository, result: RepositoryResult) -> Manifest:
    manifest = read_manifest(repo.root)
    assert manifest is not None
    manifest.result = result
    return manifest
