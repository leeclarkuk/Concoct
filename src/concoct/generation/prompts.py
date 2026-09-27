"""Prompts and schemas for implementing plan steps and repairing failures."""

from __future__ import annotations

from concoct.languages import get_profile
from concoct.models import DevelopmentPlan, PlannedCommit

COMMIT_SYSTEM = """\
You are the engineer implementing one planned commit in an existing repository.
You receive the project brief, the full development plan for context, the \
complete current contents of the repository, and the one commit to implement now.

Rules:
- Implement exactly the current commit: do not do work that later commits in \
the plan are responsible for, and do not undo earlier work.
- The repository you are shown is the ground truth. Stay consistent with the \
existing code: module names, imports, function signatures, configuration.
- For every file you create or modify, return its complete new content. Never \
return diffs, placeholders, ellipses or "rest unchanged" comments.
- Only return files that actually change. Use action "delete" (with empty \
content) to remove a file.
- After your change the project must still install, build and pass its tests. \
When you change behaviour, update the affected tests in the same commit.
- Keep any existing notice in README.md stating that the repository is synthetic.
- Never write secrets, API keys, tokens, passwords or real personal data; use \
obviously fake placeholders and read real values from the environment.

Reply with JSON only."""

REPAIR_SYSTEM = """\
You are the engineer fixing a failing build. You receive the project brief, \
the complete current repository and the validation failures (test output, \
lint errors, parse errors or missing files). Produce the smallest set of \
changes that makes validation pass without deleting tests or weakening them \
to hide real defects, and a realistic commit message for the fix.

Rules:
- Return complete new contents for every file you change; never diffs.
- Stay consistent with the existing code and dependencies. If a dependency is \
missing from the manifest, add it there.
- Never write secrets or real personal data.

Reply with JSON only."""

_CHANGE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "action", "content"],
    "properties": {
        "path": {"type": "string", "description": "repository-relative POSIX path"},
        "action": {"type": "string", "enum": ["write", "delete"]},
        "content": {"type": "string", "description": "full file content; empty for delete"},
    },
}

COMMIT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "changes"],
    "properties": {
        "summary": {"type": "string", "description": "one sentence describing what changed"},
        "changes": {"type": "array", "items": _CHANGE_ITEM},
    },
}

REPAIR_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["message", "changes"],
    "properties": {
        "message": {"type": "string", "description": "commit message for the fix"},
        "changes": {"type": "array", "items": _CHANGE_ITEM},
    },
}


def render_brief(plan: DevelopmentPlan) -> str:
    spec = plan.spec
    profile = get_profile(spec.language)
    lines = [
        f"# {spec.title or spec.name} (`{spec.name}`)",
        spec.description,
        "",
        f"- Language: {profile.display}",
        f"- Technologies: {', '.join(spec.technologies) or 'idiomatic defaults'}",
        f"- Category: {spec.category}",
    ]
    if spec.license:
        lines.append(f"- License: {spec.license}")
    if spec.architecture:
        lines += ["", "## Architecture", spec.architecture]
    if spec.features:
        lines += ["", "## Target features"] + [f"- {f}" for f in spec.features]
    lines += ["", f"## {profile.display} conventions", profile.conventions]
    return "\n".join(lines)


def render_plan(plan: DevelopmentPlan) -> str:
    lines = []
    for step in plan.commits:
        lines.append(f"{step.index + 1:>2}. ({step.kind.value}) {step.subject}")
        lines.append(f"    {step.intent}")
    return "\n".join(lines)


def build_prefix(plan: DevelopmentPlan) -> str:
    """Stable, cacheable context shared by every request for one repository."""
    return "\n".join([render_brief(plan), "", "## Development plan", render_plan(plan)])


def build_commit_prompt(
    plan: DevelopmentPlan,
    step: PlannedCommit,
    workspace_text: str,
    feedback: str | None = None,
) -> str:
    done = step.index
    progress = (
        "No commits exist yet: you are creating the repository."
        if done == 0
        else (
            "Commit 1 of the plan is already in the repository shown below."
            if done == 1
            else f"Commits 1-{done} of the plan are already in the repository shown below."
        )
    )
    parts = [
        "## Progress",
        progress,
        "",
        "## Current repository",
        workspace_text,
        "",
        f"## Commit to implement now ({step.index + 1} of {len(plan.commits)})",
        f"Kind: {step.kind.value}",
        "Message:",
        step.message,
        "",
        f"Intent: {step.intent}",
    ]
    if step.files:
        parts.append(f"Expected files: {', '.join(step.files)}")
    if feedback:
        parts += ["", "## Your previous attempt was rejected (fix these problems)", feedback]
    parts += [
        "",
        'Reply with JSON: {"summary": str, "changes": [{"path", "action", "content"}]}.',
    ]
    return "\n".join(parts)


def build_repair_prompt(workspace_text: str, diagnostics: str) -> str:
    return "\n".join(
        [
            "## Current repository",
            workspace_text,
            "",
            "## Validation failures",
            diagnostics,
            "",
            'Reply with JSON: {"message": str, "changes": [{"path", "action", "content"}]}.',
        ]
    )
