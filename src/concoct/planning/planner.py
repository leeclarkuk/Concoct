"""Produce a validated :class:`DevelopmentPlan` for one repository."""

from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field

from concoct.generation.parsing import ResponseParseError, parse_model
from concoct.logging import get_logger
from concoct.models import CommitKind, DevelopmentPlan, PlannedCommit, ProjectSpec, slugify
from concoct.planning.prompts import PLAN_SCHEMA, PLAN_SYSTEM, PlanRequest, build_plan_prompt
from concoct.providers.base import LLMProvider, LLMRequest

log = get_logger("planning")

# Kinds dropped first when a plan is too long; the scaffold is never dropped.
_TRIM_ORDER = (
    CommitKind.CHORE,
    CommitKind.DOCS,
    CommitKind.PERF,
    CommitKind.CONFIG,
    CommitKind.DEPS,
    CommitKind.REFACTOR,
    CommitKind.TEST,
)


class PlanningError(RuntimeError):
    pass


class _RawProject(BaseModel):
    name: str
    title: str = ""
    description: str
    architecture: str = ""
    features: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class _RawPlan(BaseModel):
    project: _RawProject
    commits: list[PlannedCommit]


class Planner:
    def __init__(self, provider: LLMProvider, *, max_attempts: int = 2) -> None:
        self.provider = provider
        self.max_attempts = max_attempts

    def plan(self, req: PlanRequest) -> DevelopmentPlan:
        feedback: str | None = None
        last_error = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            response = self.provider.complete(
                LLMRequest(
                    task="plan",
                    system=PLAN_SYSTEM,
                    prompt=build_plan_prompt(req, feedback),
                    json_schema=PLAN_SCHEMA,
                    max_output_tokens=16_000,
                    context={"request": asdict(req), "attempt": attempt},
                )
            )
            try:
                raw = parse_model(response.text, _RawPlan)
            except ResponseParseError as exc:
                last_error = f"reply was not a valid plan: {exc}"
                feedback = last_error
                log.warning("plan attempt %d rejected: %s", attempt, last_error)
                continue

            problems = self._problems(raw, req)
            if problems and attempt < self.max_attempts:
                feedback = "\n".join(f"- {p}" for p in problems)
                last_error = "; ".join(problems)
                log.warning("plan attempt %d rejected: %s", attempt, last_error)
                continue
            return self._normalise(raw, req)
        raise PlanningError(f"could not obtain a usable development plan: {last_error}")

    # ------------------------------------------------------------------
    @staticmethod
    def _problems(raw: _RawPlan, req: PlanRequest) -> list[str]:
        problems = []
        count = len(raw.commits)
        if not req.min_commits <= count <= req.max_commits:
            problems.append(
                f"the plan has {count} commits; it must have exactly {req.commit_count}"
            )
        if raw.commits and raw.commits[0].kind != CommitKind.SCAFFOLD:
            problems.append("the first commit must have kind 'scaffold'")
        if slugify(raw.project.name) in req.existing_names:
            problems.append(f"the name {raw.project.name!r} is already taken")
        return problems

    @staticmethod
    def _normalise(raw: _RawPlan, req: PlanRequest) -> DevelopmentPlan:
        commits = list(raw.commits)
        if len(commits) < req.min_commits:
            raise PlanningError(
                f"plan has {len(commits)} commits, fewer than the minimum {req.min_commits}"
            )
        commits[0].kind = CommitKind.SCAFFOLD
        while len(commits) > req.max_commits:
            commits.pop(_trim_index(commits))

        name = slugify(raw.project.name)
        base, suffix = name, 2
        while name in req.existing_names:
            name = f"{base}-{suffix}"
            suffix += 1

        spec = ProjectSpec(
            name=name,
            title=raw.project.title or raw.project.name,
            description=raw.project.description.strip(),
            category=req.category,
            language=req.language,
            technologies=list(req.technologies),
            architecture=raw.project.architecture.strip(),
            features=raw.project.features,
            topics=raw.project.topics,
            complexity=req.complexity,
            license=req.license,
        )
        return DevelopmentPlan(spec=spec, commits=commits)


def _trim_index(commits: list[PlannedCommit]) -> int:
    for kind in _TRIM_ORDER:
        # Remove the latest commit of this kind (keeps early structure intact).
        for index in range(len(commits) - 1, 0, -1):
            if commits[index].kind == kind:
                return index
    return len(commits) - 1
