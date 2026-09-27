"""Per-language conventions used by prompts and validators."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    display: str
    aliases: tuple[str, ...] = ()
    source_extensions: tuple[str, ...] = ()
    # At least one of these must exist for dependency metadata to be valid.
    dependency_files: tuple[str, ...] = ()
    required_files: tuple[str, ...] = ("README.md", ".gitignore")
    gitignore_hint: str = ""
    conventions: str = ""
    extra: dict[str, str] = field(default_factory=dict)


PYTHON = LanguageProfile(
    name="python",
    display="Python",
    aliases=("py", "python3"),
    source_extensions=(".py",),
    dependency_files=("pyproject.toml", "requirements.txt"),
    gitignore_hint="__pycache__/, *.pyc, .venv/, .pytest_cache/, dist/, *.egg-info/",
    conventions="""\
- Target Python 3.12+. Use a `src/<package>/` layout with type hints.
- Declare metadata in a PEP 621 `pyproject.toml` with a `[build-system]` table
  (hatchling is a good default) and list runtime dependencies under
  `[project] dependencies`.
- Put test/lint tooling under `[project.optional-dependencies] dev` and always
  include `pytest` there (plus `httpx2` when testing ASGI apps with TestClient).
- Tests live in `tests/` and must pass with `python -m pytest` after
  `pip install -e ".[dev]"`. Tests must not need network access or external
  services; use in-memory fakes.
- Configure ruff in `pyproject.toml` (`[tool.ruff]`) once linting is introduced.""",
)

TYPESCRIPT = LanguageProfile(
    name="typescript",
    display="TypeScript",
    aliases=("ts", "node-ts"),
    source_extensions=(".ts", ".tsx"),
    dependency_files=("package.json",),
    gitignore_hint="node_modules/, dist/, coverage/, .env",
    conventions="""\
- Use a valid `package.json` with `scripts.build` and `scripts.test` and a
  `tsconfig.json`. Prefer vitest for tests.
- Source in `src/`, tests alongside (`*.test.ts`) or in `tests/`.""",
)

JAVASCRIPT = LanguageProfile(
    name="javascript",
    display="JavaScript",
    aliases=("js", "node", "nodejs"),
    source_extensions=(".js", ".mjs", ".cjs", ".jsx"),
    dependency_files=("package.json",),
    gitignore_hint="node_modules/, dist/, coverage/, .env",
    conventions="""\
- Use a valid `package.json` (ES modules) with a `scripts.test` entry.
  Prefer the built-in `node:test` runner or vitest.""",
)

GO = LanguageProfile(
    name="go",
    display="Go",
    aliases=("golang",),
    source_extensions=(".go",),
    dependency_files=("go.mod",),
    gitignore_hint="/bin/, *.exe, *.test, coverage.out",
    conventions="""\
- Include `go.mod` (Go 1.22+). Tests use the standard `testing` package and must
  pass with `go test ./...`. Avoid third-party dependencies unless requested.""",
)

RUST = LanguageProfile(
    name="rust",
    display="Rust",
    aliases=("rs",),
    source_extensions=(".rs",),
    dependency_files=("Cargo.toml",),
    gitignore_hint="/target/",
    conventions="""\
- Include `Cargo.toml` (edition 2021). Unit tests in `#[cfg(test)]` modules and
  integration tests in `tests/`; `cargo test` must pass offline where possible.""",
)

PROFILES: dict[str, LanguageProfile] = {
    p.name: p for p in (PYTHON, TYPESCRIPT, JAVASCRIPT, GO, RUST)
}


def get_profile(language: str) -> LanguageProfile:
    key = language.strip().lower()
    if key in PROFILES:
        return PROFILES[key]
    for profile in PROFILES.values():
        if key in profile.aliases:
            return profile
    return LanguageProfile(
        name=key,
        display=language,
        conventions=f"- Follow idiomatic {language} project conventions, including a "
        "standard dependency manifest and automated tests.",
    )
