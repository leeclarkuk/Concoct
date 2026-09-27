"""Turn a planned commit (or a validation failure) into concrete file changes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from concoct.generation.parsing import ResponseParseError, parse_model
from concoct.generation.prompts import (
    COMMIT_SCHEMA,
    COMMIT_SYSTEM,
    REPAIR_SCHEMA,
    REPAIR_SYSTEM,
    build_commit_prompt,
    build_prefix,
    build_repair_prompt,
)
from concoct.generation.workspace import DEFAULT_CONTEXT_CHARS, render_workspace
from concoct.logging import get_logger
from concoct.models import DevelopmentPlan, FileChange, PlannedCommit
from concoct.providers.base import LLMProvider, LLMRequest, ProviderTruncatedError
from concoct.secrets import scan_text
from concoct.validation.syntax import check_syntax

log = get_logger("generation")


class GenerationError(RuntimeError):
    pass


@dataclass
class Draft:
    """File changes ready to apply, plus the commit message to use."""

    message: str
    changes: list[FileChange]
    summary: str = ""
    attempts: int = 1
    warnings: list[str] = field(default_factory=list)


class _CommitReply(BaseModel):
    summary: str = ""
    changes: list[FileChange] = Field(default_factory=list)


class _RepairReply(BaseModel):
    message: str
    changes: list[FileChange] = Field(default_factory=list)


def screen_changes(changes: list[FileChange], current: dict[str, str]) -> list[str]:
    """Problems that make a set of changes unacceptable to commit."""
    problems: list[str] = []
    seen: set[str] = set()
    effective = 0
    for change in changes:
        if change.path in seen:
            problems.append(f"{change.path} appears more than once")
        seen.add(change.path)
        if change.action == "delete":
            if change.path in current:
                effective += 1
            continue
        content = change.content or ""
        existing = current.get(change.path)
        if existing is None or existing.rstrip("\n") != content.rstrip("\n"):
            effective += 1
        for finding in scan_text(change.path, content):
            problems.append(
                f"{change.path}:{finding.line} contains something that looks like a secret "
                f"({finding.kind}); use a placeholder read from the environment instead"
            )
        error = check_syntax(change.path, content)
        if error:
            problems.append(f"syntax error: {error}")
    if effective == 0:
        problems.append("the changes do not modify the repository")
    return problems


class CommitGenerator:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_attempts: int = 2,
        context_chars: int = DEFAULT_CONTEXT_CHARS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.provider = provider
        self.max_attempts = max_attempts
        self.context_chars = context_chars

    def implement(self, plan: DevelopmentPlan, step: PlannedCommit, files: dict[str, str]) -> Draft:
        workspace = render_workspace(files, focus=step.files, max_chars=self.context_chars)

        def request(feedback: str | None) -> LLMRequest:
            return LLMRequest(
                task="commit",
                system=COMMIT_SYSTEM,
                prompt=build_commit_prompt(plan, step, workspace.text, feedback),
                prompt_prefix=build_prefix(plan),
                json_schema=COMMIT_SCHEMA,
                max_output_tokens=64_000,
                context={
                    "plan": plan.model_dump(mode="json"),
                    "step_index": step.index,
                    "files": files,
                    "feedback": feedback,
                },
            )

        def to_draft(text: str) -> Draft:
            reply = parse_model(text, _CommitReply)
            return Draft(message=step.message, changes=reply.changes, summary=reply.summary)

        return self._negotiate(request, to_draft, files, label=f"commit {step.index + 1}")

    def repair(self, plan: DevelopmentPlan, files: dict[str, str], diagnostics: str) -> Draft:
        workspace = render_workspace(files, max_chars=self.context_chars)

        def request(feedback: str | None) -> LLMRequest:
            diag = diagnostics if not feedback else f"{diagnostics}\n\n## Also fix\n{feedback}"
            return LLMRequest(
                task="repair",
                system=REPAIR_SYSTEM,
                prompt=build_repair_prompt(workspace.text, diag),
                prompt_prefix=build_prefix(plan),
                json_schema=REPAIR_SCHEMA,
                max_output_tokens=64_000,
                context={
                    "plan": plan.model_dump(mode="json"),
                    "files": files,
                    "diagnostics": diagnostics,
                },
            )

        def to_draft(text: str) -> Draft:
            reply = parse_model(text, _RepairReply)
            message = reply.message.strip() or "fix: repair failing checks"
            return Draft(message=message, changes=reply.changes, summary=message)

        return self._negotiate(request, to_draft, files, label="repair")

    # ------------------------------------------------------------------
    def _negotiate(
        self,
        build_request: Callable[[str | None], LLMRequest],
        to_draft: Callable[[str], Draft],
        files: dict[str, str],
        *,
        label: str,
    ) -> Draft:
        feedback: str | None = None
        problems: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.provider.complete(build_request(feedback))
            except ProviderTruncatedError:
                problems = [
                    "your reply was cut off by the output limit: change fewer files or keep "
                    "files shorter"
                ]
                feedback = "\n".join(f"- {p}" for p in problems)
                log.warning("%s attempt %d truncated", label, attempt)
                continue
            try:
                draft = to_draft(response.text)
            except ResponseParseError as exc:
                problems = [f"the reply was not valid JSON of the required shape: {exc}"]
            else:
                problems = screen_changes(draft.changes, files)
                if not problems:
                    draft.attempts = attempt
                    return draft
            feedback = "\n".join(f"- {p}" for p in problems)
            log.warning("%s attempt %d rejected: %s", label, attempt, "; ".join(problems))
        raise GenerationError(
            f"{label}: no acceptable changes after {self.max_attempts} attempts: "
            + "; ".join(problems)
        )
