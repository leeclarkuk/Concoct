"""Render the current repository state for inclusion in prompts."""

from __future__ import annotations

from dataclasses import dataclass

# ~4 characters per token; keep repository context well inside the window
# while leaving room for the plan and the reply.
DEFAULT_CONTEXT_CHARS = 240_000


@dataclass(frozen=True)
class RenderedWorkspace:
    text: str
    included: list[str]
    omitted: list[str]


def render_workspace(
    files: dict[str, str],
    *,
    focus: list[str] | None = None,
    max_chars: int = DEFAULT_CONTEXT_CHARS,
) -> RenderedWorkspace:
    """Render ``files`` as fenced blocks, prioritising ``focus`` paths.

    Every file is included verbatim while the budget allows; beyond that,
    focus files and small files win and the rest are listed by path so the
    model still knows they exist.
    """
    if not files:
        return RenderedWorkspace("(the repository is empty)", [], [])
    focus_set = set(focus or [])
    order = sorted(files, key=lambda p: (p not in focus_set, len(files[p]), p))
    included: list[str] = []
    omitted: list[str] = []
    used = 0
    for path in order:
        cost = len(files[path]) + len(path) + 16
        if used + cost <= max_chars:
            included.append(path)
            used += cost
        else:
            omitted.append(path)

    parts = ["Files (" + str(len(files)) + " total):"]
    parts += [f"- {p}" for p in sorted(files)]
    parts.append("")
    for path in sorted(included):
        parts.append(f"=== FILE: {path} ===")
        parts.append(files[path].rstrip("\n"))
        parts.append(f"=== END FILE: {path} ===")
        parts.append("")
    if omitted:
        parts.append(
            "Contents omitted for size (do not rewrite these unless necessary): "
            + ", ".join(sorted(omitted))
        )
    return RenderedWorkspace("\n".join(parts), sorted(included), sorted(omitted))
