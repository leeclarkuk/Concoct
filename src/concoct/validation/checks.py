"""Individual validation checks run against an exported repository tree."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from concoct.languages import LanguageProfile
from concoct.models import CheckResult, CheckStatus, ProjectSpec
from concoct.secrets import scan_text
from concoct.validation.syntax import check_syntax

OUTPUT_TAIL_CHARS = 6_000

# PEP 508-ish: name, optional extras, optional version spec / marker / URL.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,\s-]+\])?\s*"
    r"((==|>=|<=|~=|!=|>|<|===)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*(==|>=|<=|~=|!=|>|<)\s*"
    r"[A-Za-z0-9.*+!_-]+)*)?\s*(;.*)?$|^[A-Za-z0-9][A-Za-z0-9._-]*\s*@\s*\S+$"
)
_SENSITIVE_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|CREDENTIAL|PRIVATE)", re.I)
_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?/|__tests__/)|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$"
)


@dataclass
class ValidationContext:
    root: Path
    spec: ProjectSpec
    profile: LanguageProfile
    files: dict[str, str]
    run_commands: bool = True
    command_timeout: int = 600
    strict_lint: bool = False
    notes: list[str] = field(default_factory=list)


Check = Callable[[ValidationContext], CheckResult]


def _tail(text: str, limit: int = OUTPUT_TAIL_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for running generated code: credentials are never passed on."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not _SENSITIVE_ENV.search(key)
        and not key.startswith(("CONCOCT_", "ANTHROPIC_", "GITHUB_", "GH_", "VIRTUAL_ENV"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CI"] = "true"
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------- static
def check_required_files(ctx: ValidationContext) -> CheckResult:
    missing = [f for f in ctx.profile.required_files if f not in ctx.files]
    if ctx.profile.dependency_files and not any(
        f in ctx.files for f in ctx.profile.dependency_files
    ):
        missing.append(" or ".join(ctx.profile.dependency_files))
    if ctx.spec.license and not any(
        p.upper().startswith(("LICENSE", "LICENCE", "COPYING")) for p in ctx.files
    ):
        missing.append("LICENSE")
    sources = [p for p in ctx.files if p.endswith(ctx.profile.source_extensions)]
    if ctx.profile.source_extensions and not sources:
        missing.append(f"source files ({', '.join(ctx.profile.source_extensions)})")
    if not any(_TEST_FILE_RE.search(p) for p in ctx.files):
        missing.append("tests")
    if missing:
        return CheckResult(
            name="required files",
            status=CheckStatus.FAILED,
            summary=f"missing: {', '.join(missing)}",
        )
    return CheckResult(
        name="required files",
        status=CheckStatus.PASSED,
        summary=f"{len(ctx.files)} files, {len(sources)} source",
    )


def check_syntax_all(ctx: ValidationContext) -> CheckResult:
    errors = [err for p, c in sorted(ctx.files.items()) if (err := check_syntax(p, c))]
    if errors:
        return CheckResult(
            name="syntax",
            status=CheckStatus.FAILED,
            summary=f"{len(errors)} file(s) failed to parse",
            details="\n".join(errors),
        )
    return CheckResult(name="syntax", status=CheckStatus.PASSED, summary="all files parse")


def check_secrets(ctx: ValidationContext) -> CheckResult:
    findings = [f for p, c in ctx.files.items() for f in scan_text(p, c)]
    if findings:
        return CheckResult(
            name="secrets",
            status=CheckStatus.FAILED,
            summary=f"{len(findings)} secret-like value(s) found",
            details="\n".join(f"{f.path}:{f.line}: {f.kind}" for f in findings),
        )
    return CheckResult(name="secrets", status=CheckStatus.PASSED, summary="no secrets found")


def check_dependencies(ctx: ValidationContext) -> CheckResult:
    handlers: dict[str, Callable[[ValidationContext], list[str]]] = {
        "pyproject.toml": _pyproject_problems,
        "requirements.txt": _requirements_problems,
        "package.json": _package_json_problems,
        "go.mod": _go_mod_problems,
        "Cargo.toml": _cargo_problems,
    }
    problems: list[str] = []
    checked: list[str] = []
    for name, handler in handlers.items():
        if name in ctx.files:
            checked.append(name)
            problems.extend(handler(ctx))
    if not checked:
        return CheckResult(
            name="dependency metadata",
            status=CheckStatus.SKIPPED if not ctx.profile.dependency_files else CheckStatus.FAILED,
            summary="no dependency manifest found",
        )
    missing_tech = _missing_technologies(ctx)
    if problems:
        return CheckResult(
            name="dependency metadata",
            status=CheckStatus.FAILED,
            summary=f"{len(problems)} problem(s) in {', '.join(checked)}",
            details="\n".join(problems),
        )
    if missing_tech:
        return CheckResult(
            name="dependency metadata",
            status=CheckStatus.WARNING,
            summary=f"requested technologies not declared: {', '.join(missing_tech)}",
            blocking=False,
        )
    return CheckResult(
        name="dependency metadata", status=CheckStatus.PASSED, summary=f"valid {', '.join(checked)}"
    )


def _pyproject_problems(ctx: ValidationContext) -> list[str]:
    try:
        data = tomllib.loads(ctx.files["pyproject.toml"])
    except tomllib.TOMLDecodeError as exc:
        return [f"pyproject.toml: {exc}"]
    problems = []
    project = data.get("project")
    if not isinstance(project, dict):
        return ["pyproject.toml: missing [project] table"]
    if not project.get("name"):
        problems.append("pyproject.toml: [project].name is required")
    if "version" not in project and "version" not in project.get("dynamic", []):
        problems.append("pyproject.toml: [project].version (or dynamic version) is required")
    deps = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        deps.extend(extra_deps)
    for group in data.get("dependency-groups", {}).values():
        deps.extend(d for d in group if isinstance(d, str))
    for dep in deps:
        if not isinstance(dep, str) or not _REQUIREMENT_RE.match(dep.strip()):
            problems.append(f"pyproject.toml: invalid requirement {dep!r}")
    if "build-system" not in data:
        problems.append("pyproject.toml: missing [build-system] table (project is not installable)")
    return problems


def _requirements_problems(ctx: ValidationContext) -> list[str]:
    problems = []
    for lineno, line in enumerate(ctx.files["requirements.txt"].splitlines(), 1):
        entry = line.split(" #", 1)[0].strip()
        if not entry or entry.startswith(("#", "-r", "-c", "-e", "--")):
            continue
        if not _REQUIREMENT_RE.match(entry):
            problems.append(f"requirements.txt:{lineno}: invalid requirement {entry!r}")
    return problems


def _package_json_problems(ctx: ValidationContext) -> list[str]:
    try:
        data = json.loads(ctx.files["package.json"])
    except json.JSONDecodeError as exc:
        return [f"package.json: {exc}"]
    problems = []
    if not isinstance(data, dict):
        return ["package.json: top level must be an object"]
    if not data.get("name"):
        problems.append("package.json: name is required")
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section, {})
        if not isinstance(deps, dict) or not all(isinstance(v, str) for v in deps.values()):
            problems.append(f"package.json: {section} must map names to version strings")
    if not isinstance(data.get("scripts", {}), dict) or "test" not in data.get("scripts", {}):
        problems.append("package.json: scripts.test is required")
    return problems


def _go_mod_problems(ctx: ValidationContext) -> list[str]:
    if not re.search(r"^module\s+\S+", ctx.files["go.mod"], re.M):
        return ["go.mod: missing module directive"]
    return []


def _cargo_problems(ctx: ValidationContext) -> list[str]:
    try:
        data = tomllib.loads(ctx.files["Cargo.toml"])
    except tomllib.TOMLDecodeError as exc:
        return [f"Cargo.toml: {exc}"]
    if "package" not in data and "workspace" not in data:
        return ["Cargo.toml: missing [package] or [workspace]"]
    return []


def _missing_technologies(ctx: ValidationContext) -> list[str]:
    manifests = " ".join(
        ctx.files.get(name, "")
        for name in ("pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml")
    ).lower()
    missing = []
    for tech in ctx.spec.technologies:
        token = tech.lower().replace(" ", "-")
        if token and token not in manifests:
            missing.append(tech)
    return missing


# ------------------------------------------------------------------- commands
@dataclass
class CommandOutcome:
    returncode: int
    output: str
    timed_out: bool = False


def run_command(
    args: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> CommandOutcome:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            env=env or sanitized_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        return CommandOutcome(-1, output, timed_out=True)
    except OSError as exc:
        return CommandOutcome(-1, str(exc))
    return CommandOutcome(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def check_python_commands(ctx: ValidationContext) -> list[CheckResult]:
    if not ctx.run_commands:
        return [
            CheckResult(
                name="tests",
                status=CheckStatus.SKIPPED,
                summary="command execution disabled",
                blocking=False,
            )
        ]
    uv = shutil.which("uv")
    venv = ctx.root / ".concoct-venv"
    python = venv / "bin" / "python"
    if uv:
        setup = run_command(
            [uv, "venv", str(venv), "--python", "3.12", "-q"], ctx.root, ctx.command_timeout
        )
    else:
        setup = run_command(
            [sys.executable, "-m", "venv", str(venv)], ctx.root, ctx.command_timeout
        )
    if setup.returncode != 0:
        return [
            CheckResult(
                name="environment",
                status=CheckStatus.WARNING,
                blocking=False,
                summary="could not create a virtualenv; tests skipped",
                details=_tail(setup.output),
            )
        ]

    install_cmd = _python_install_command(ctx, uv, python)
    install = run_command(install_cmd, ctx.root, ctx.command_timeout)
    results: list[CheckResult] = []
    if install.returncode != 0:
        return [
            CheckResult(
                name="install",
                status=CheckStatus.FAILED,
                summary="dependencies failed to install"
                + (" (timed out)" if install.timed_out else ""),
                details=f"$ {' '.join(install_cmd[-3:])}\n{_tail(install.output)}",
            )
        ]
    results.append(
        CheckResult(
            name="install",
            status=CheckStatus.PASSED,
            summary="project and dev dependencies installed",
        )
    )

    test = run_command(
        [str(python), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        ctx.root,
        ctx.command_timeout,
        env=sanitized_env({"PYTHONPATH": str(ctx.root / "src")}),
    )
    summary_line = _last_nonempty_line(test.output)
    if test.returncode == 0:
        results.append(
            CheckResult(
                name="tests", status=CheckStatus.PASSED, summary=summary_line or "pytest passed"
            )
        )
    else:
        reason = {5: "no tests were collected"}.get(test.returncode, summary_line)
        results.append(
            CheckResult(
                name="tests",
                status=CheckStatus.FAILED,
                summary=("timed out" if test.timed_out else reason) or "pytest failed",
                details=_tail(test.output),
            )
        )
    results.append(_ruff_check(ctx))
    return results


def _python_install_command(ctx: ValidationContext, uv: str | None, python: Path) -> list[str]:
    target: list[str]
    if "pyproject.toml" in ctx.files:
        extras = ""
        try:
            optional = (
                tomllib.loads(ctx.files["pyproject.toml"])
                .get("project", {})
                .get("optional-dependencies", {})
            )
            if "dev" in optional:
                extras = "[dev]"
            elif "test" in optional:
                extras = "[test]"
        except tomllib.TOMLDecodeError:
            pass
        target = ["-e", f".{extras}", "pytest"]
    else:
        target = ["pytest"]
        for req in ("requirements.txt", "requirements-dev.txt", "requirements/dev.txt"):
            if req in ctx.files:
                target += ["-r", req]
    if uv:
        return [uv, "pip", "install", "-q", "--python", str(python), *target]
    return [str(python), "-m", "pip", "install", "-q", *target]


def _ruff_check(ctx: ValidationContext) -> CheckResult:
    ruff = shutil.which("ruff")
    args = [ruff] if ruff else [sys.executable, "-m", "ruff"]
    outcome = run_command(
        [*args, "check", "--no-cache", "--exclude", ".concoct-venv", "."],
        ctx.root,
        ctx.command_timeout,
    )
    if outcome.returncode == 0:
        return CheckResult(
            name="lint",
            status=CheckStatus.PASSED,
            summary="ruff: no issues",
            blocking=ctx.strict_lint,
        )
    if "No module named ruff" in outcome.output or outcome.returncode == -1:
        return CheckResult(
            name="lint", status=CheckStatus.SKIPPED, summary="ruff not available", blocking=False
        )
    return CheckResult(
        name="lint",
        status=CheckStatus.FAILED if ctx.strict_lint else CheckStatus.WARNING,
        summary=_last_nonempty_line(outcome.output) or "ruff reported issues",
        details=_tail(outcome.output),
        blocking=ctx.strict_lint,
    )


def check_toolchain_commands(ctx: ValidationContext) -> list[CheckResult]:
    """Build/test for non-Python languages when the toolchain is installed."""
    commands: dict[str, list[list[str]]] = {
        "go": [["go", "vet", "./..."], ["go", "test", "./..."]],
        "rust": [["cargo", "test", "--quiet"]],
        "typescript": [["npm", "install", "--no-audit", "--no-fund"], ["npm", "test"]],
        "javascript": [["npm", "install", "--no-audit", "--no-fund"], ["npm", "test"]],
    }
    steps = commands.get(ctx.profile.name)
    if not steps:
        return [
            CheckResult(
                name="tests",
                status=CheckStatus.SKIPPED,
                blocking=False,
                summary=f"no test runner configured for {ctx.profile.display}",
            )
        ]
    if not ctx.run_commands:
        return [
            CheckResult(
                name="tests",
                status=CheckStatus.SKIPPED,
                blocking=False,
                summary="command execution disabled",
            )
        ]
    if not shutil.which(steps[0][0]):
        return [
            CheckResult(
                name="tests",
                status=CheckStatus.SKIPPED,
                blocking=False,
                summary=f"{steps[0][0]} not installed; build/tests not run",
            )
        ]
    for args in steps:
        outcome = run_command(args, ctx.root, ctx.command_timeout)
        if outcome.returncode != 0:
            return [
                CheckResult(
                    name="tests",
                    status=CheckStatus.FAILED,
                    summary=f"`{' '.join(args)}` failed",
                    details=_tail(outcome.output),
                )
            ]
    return [
        CheckResult(
            name="tests",
            status=CheckStatus.PASSED,
            summary=" && ".join(" ".join(a) for a in steps) + " succeeded",
        )
    ]


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        if line.strip():
            return line.strip().strip("=").strip()[:200]
    return ""


STATIC_CHECKS: tuple[Check, ...] = (
    check_required_files,
    check_syntax_all,
    check_dependencies,
    check_secrets,
)
