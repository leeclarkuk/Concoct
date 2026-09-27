"""Domain models shared across Concoct's pipeline."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")


def slugify(value: str, max_length: int = 60) -> str:
    """Normalise ``value`` into a GitHub-compatible kebab-case repository name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:max_length].strip("-")
    return slug or "project"


class CommitKind(StrEnum):
    SCAFFOLD = "scaffold"
    FEATURE = "feature"
    TEST = "test"
    REFACTOR = "refactor"
    FIX = "fix"
    DOCS = "docs"
    CONFIG = "config"
    DEPS = "deps"
    CI = "ci"
    PERF = "perf"
    CHORE = "chore"

    @classmethod
    def coerce(cls, value: str) -> CommitKind:
        aliases = {
            "feat": cls.FEATURE,
            "tests": cls.TEST,
            "bugfix": cls.FIX,
            "doc": cls.DOCS,
            "documentation": cls.DOCS,
            "build": cls.CONFIG,
            "dependencies": cls.DEPS,
            "dependency": cls.DEPS,
            "init": cls.SCAFFOLD,
            "initial": cls.SCAFFOLD,
            "style": cls.CHORE,
            "polish": cls.CHORE,
        }
        key = value.strip().lower()
        if key in aliases:
            return aliases[key]
        return cls(key)


class Complexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProjectSpec(BaseModel):
    """What the repository is: produced by the planner, fixed for its lifetime."""

    model_config = ConfigDict(extra="ignore")

    name: str
    title: str = ""
    description: str
    category: str
    language: str
    technologies: list[str] = Field(default_factory=list)
    architecture: str = ""
    features: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    complexity: Complexity = Complexity.MEDIUM
    license: str | None = None

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, value: str) -> str:
        return value if _SLUG_RE.match(value) else slugify(value)

    @field_validator("topics")
    @classmethod
    def _normalise_topics(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for topic in value:
            slug = slugify(topic, max_length=50)
            if slug not in seen:
                seen.append(slug)
        return seen[:15]


class PlannedCommit(BaseModel):
    """One step of the development plan."""

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    kind: CommitKind
    message: str
    intent: str
    files: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value: object) -> object:
        return CommitKind.coerce(value) if isinstance(value, str) else value

    @field_validator("message")
    @classmethod
    def _clean_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("commit message must not be empty")
        return value

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0]


class DevelopmentPlan(BaseModel):
    spec: ProjectSpec
    commits: list[PlannedCommit]

    @model_validator(mode="after")
    def _reindex(self) -> DevelopmentPlan:
        for i, commit in enumerate(self.commits):
            commit.index = i
        return self


class FileChange(BaseModel):
    """A single file operation proposed by the model."""

    model_config = ConfigDict(extra="ignore")

    path: str
    action: Literal["write", "delete"] = "write"
    content: str | None = None

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return normalise_repo_path(value)

    @model_validator(mode="after")
    def _content_for_write(self) -> FileChange:
        if self.action == "write" and self.content is None:
            raise ValueError(f"write of {self.path!r} requires content")
        return self


def normalise_repo_path(value: str) -> str:
    """Validate that ``value`` is a safe, relative path inside a repository."""
    raw = value.strip().replace("\\", "/")
    if not raw:
        raise ValueError("empty path")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("~"):
        raise ValueError(f"absolute path not allowed: {value!r}")
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"path escapes repository: {value!r}")
    if parts[0] == ".git":
        raise ValueError(f"path inside .git not allowed: {value!r}")
    return "/".join(parts)


class CommitRecord(BaseModel):
    index: int
    sha: str
    kind: CommitKind
    message: str
    authored_at: datetime
    files_changed: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    repair: bool = False

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0]


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    summary: str
    details: str = ""
    blocking: bool = True

    @property
    def is_failure(self) -> bool:
        return self.status == CheckStatus.FAILED and self.blocking


class ValidationReport(BaseModel):
    attempt: int = 0
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(check.is_failure for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if check.is_failure]

    def diagnostics(self, max_chars: int = 12_000) -> str:
        """Human/LLM readable description of failures, bounded in size."""
        chunks = []
        for check in self.failures:
            chunks.append(f"## {check.name}: {check.summary}\n{check.details}".rstrip())
        text = "\n\n".join(chunks)
        if len(text) > max_chars:
            text = text[: max_chars // 2] + "\n…[truncated]…\n" + text[-max_chars // 2 :]
        return text


class UsageSummary(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def add(self, other: UsageSummary) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cost_usd += other.cost_usd


class RepositoryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    VALIDATION_FAILED = "validation_failed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"


class RemoteInfo(BaseModel):
    full_name: str
    html_url: str
    visibility: Literal["public", "private"]
    pushed_at: datetime | None = None


class RepositoryResult(BaseModel):
    index: int
    status: RepositoryStatus
    path: str | None = None
    spec: ProjectSpec | None = None
    plan: DevelopmentPlan | None = None
    commits: list[CommitRecord] = Field(default_factory=list)
    validation: ValidationReport | None = None
    repair_attempts: int = 0
    usage: UsageSummary = Field(default_factory=UsageSummary)
    remote: RemoteInfo | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        return self.spec.name if self.spec else f"repository-{self.index + 1}"


class Manifest(BaseModel):
    """Metadata Concoct keeps about each repository it generated.

    Stored inside the ``.git`` directory so it is never part of history.
    """

    tool: Literal["concoct"] = "concoct"
    concoct_version: str
    created_at: datetime
    seed: int
    repo_index: int
    provider: str
    model: str
    options: dict[str, object] = Field(default_factory=dict)
    result: RepositoryResult
