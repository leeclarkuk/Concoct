"""Validate a repository by checking an export of its committed HEAD."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from concoct.gitops.repository import GitRepository
from concoct.languages import get_profile
from concoct.logging import get_logger
from concoct.models import CheckResult, CheckStatus, ProjectSpec, ValidationReport
from concoct.validation.checks import (
    STATIC_CHECKS,
    ValidationContext,
    check_python_commands,
    check_toolchain_commands,
)

log = get_logger("validation")


class Validator:
    """Runs static checks, then build/test/lint commands when feasible.

    Validation always runs on a clean ``git archive`` of HEAD in a temporary
    directory, so it sees exactly what history contains and never pollutes
    the working tree with virtualenvs, lock files or caches.
    """

    def __init__(
        self,
        *,
        run_commands: bool = True,
        command_timeout: int = 600,
        strict_lint: bool = False,
    ) -> None:
        self.run_commands = run_commands
        self.command_timeout = command_timeout
        self.strict_lint = strict_lint

    def validate(
        self, repo: GitRepository, spec: ProjectSpec, attempt: int = 0
    ) -> ValidationReport:
        workdir = Path(tempfile.mkdtemp(prefix="concoct-validate-"))
        try:
            root = repo.export_head(workdir / "tree")
            return self.validate_tree(root, spec, attempt)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def validate_tree(self, root: Path, spec: ProjectSpec, attempt: int = 0) -> ValidationReport:
        files = _read_tree(root)
        profile = get_profile(spec.language)
        ctx = ValidationContext(
            root=root,
            spec=spec,
            profile=profile,
            files=files,
            run_commands=self.run_commands,
            command_timeout=self.command_timeout,
            strict_lint=self.strict_lint,
        )
        report = ValidationReport(attempt=attempt)
        for check in STATIC_CHECKS:
            try:
                report.checks.append(check(ctx))
            except Exception as exc:  # a broken check must not abort generation
                log.exception("check %s crashed", check.__name__)
                report.checks.append(
                    CheckResult(
                        name=check.__name__,
                        status=CheckStatus.FAILED,
                        summary=f"check crashed: {exc}",
                    )
                )

        if not report.passed:
            report.checks.append(
                CheckResult(
                    name="tests",
                    status=CheckStatus.SKIPPED,
                    blocking=False,
                    summary="not run because static checks failed",
                )
            )
            return report

        if profile.name == "python":
            report.checks.extend(check_python_commands(ctx))
        else:
            report.checks.extend(check_toolchain_commands(ctx))
        return report


def _read_tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            continue
        files[rel] = data.decode("utf-8", errors="replace")
    return files
