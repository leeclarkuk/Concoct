"""Prompts and JSON schemas for concept and development-plan generation."""

from __future__ import annotations

from dataclasses import dataclass

from concoct.languages import get_profile
from concoct.models import CommitKind, Complexity

PLAN_SYSTEM = """\
You are a senior software engineer planning the development history of a new \
open-source project. You design small, genuinely useful projects and break \
their construction into a realistic sequence of commits, the way an experienced \
developer would actually build them.

Principles:
- The project must be coherent and working, not a pile of plausible-looking files.
- Each commit is one focused, reviewable change that leaves the project in a \
working state (it builds and its tests pass).
- Later commits build on earlier ones: reference files and APIs that earlier \
commits introduced.
- Scope is deliberately modest so every file can be written in full, correctly.
- Commit messages read like a real developer wrote them: imperative mood, \
subject line at most 72 characters, mostly Conventional Commits \
(`feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`, `ci:`, `build:`), \
an optional short body for non-trivial changes. Never mention AI or generation tools.

Reply with JSON only."""

COMPLEXITY_GUIDANCE: dict[Complexity, str] = {
    Complexity.LOW: "a small utility: roughly 4-8 source files and 300-900 lines in total",
    Complexity.MEDIUM: "a focused tool or service: roughly 8-18 source files and "
    "800-2,500 lines in total",
    Complexity.HIGH: "a substantial application or library: roughly 18-35 source files and "
    "2,500-6,000 lines in total",
}

_KINDS = [kind.value for kind in CommitKind]

PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project", "commits"],
    "properties": {
        "project": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "title", "description", "architecture", "features", "topics"],
            "properties": {
                "name": {"type": "string", "description": "kebab-case repository name"},
                "title": {"type": "string"},
                "description": {"type": "string", "description": "one sentence"},
                "architecture": {
                    "type": "string",
                    "description": "modules/layers and how they interact, 3-8 sentences",
                },
                "features": {"type": "array", "items": {"type": "string"}},
                "topics": {"type": "array", "items": {"type": "string"}},
            },
        },
        "commits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "message", "intent", "files"],
                "properties": {
                    "kind": {"type": "string", "enum": _KINDS},
                    "message": {"type": "string", "description": "full commit message"},
                    "intent": {
                        "type": "string",
                        "description": "precisely what this commit changes and why",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "paths expected to be created or modified",
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True)
class PlanRequest:
    language: str
    technologies: list[str]
    category: str
    complexity: Complexity
    commit_count: int
    min_commits: int
    max_commits: int
    license: str | None = None
    include_ci: bool = True
    existing_names: tuple[str, ...] = ()
    synthetic_disclosure: bool = True


def build_plan_prompt(req: PlanRequest, feedback: str | None = None) -> str:
    profile = get_profile(req.language)
    tech = ", ".join(req.technologies) if req.technologies else "your choice of idiomatic tools"
    lines = [
        "Design a new project and its commit-by-commit development plan.",
        "",
        "## Constraints",
        f"- Language: {profile.display}",
        f"- Technologies that must be used: {tech}",
        f"- Category: {req.category}",
        f"- Size: {COMPLEXITY_GUIDANCE[req.complexity]}",
        f"- Number of commits: exactly {req.commit_count}",
    ]
    if req.license:
        lines.append(f"- Include a {req.license} LICENSE file in the first commit.")
    if req.include_ci:
        lines.append(
            "- Add a CI workflow (GitHub Actions running lint and tests) at a sensible "
            "point once tests exist."
        )
    if req.existing_names:
        lines.append(f"- The name must differ from: {', '.join(sorted(req.existing_names))}")
    lines += [
        "",
        f"## {profile.display} conventions",
        profile.conventions,
        "",
        "## Plan requirements",
        "- Commit 1 has kind `scaffold`: a minimal but runnable skeleton with the "
        "dependency manifest, a sensible .gitignore, a short README and a first test.",
        "- Mix kinds realistically across the history: several `feature` commits, "
        "`test` commits, at least one `refactor`, at least one `fix` that repairs a "
        "specific, plausible defect in earlier code (say which), `docs` updates, and "
        "`config`/`deps` changes. Use `ci` for the CI workflow.",
        "- Each `intent` must be concrete enough for another engineer to implement the "
        "commit without guessing: name the functions, endpoints, classes or options involved.",
        "- `files` lists the paths the commit will create or modify.",
        "- Keep every file small enough to be rewritten in full.",
        "- Never include secrets, API keys, tokens or real personal data.",
        "- `name` is a short kebab-case repository name (1-3 words) that fits the purpose.",
    ]
    if feedback:
        lines += ["", "## Problems with your previous answer (fix them)", feedback]
    lines += [
        "",
        "Reply with a JSON object: "
        '{"project": {"name", "title", "description", "architecture", "features", '
        '"topics"}, "commits": [{"kind", "message", "intent", "files"}]}.',
    ]
    return "\n".join(lines)
