from __future__ import annotations

import os
from pathlib import Path

import pytest

from concoct.models import CheckStatus, ProjectSpec
from concoct.validation.checks import sanitized_env
from concoct.validation.runner import Validator
from concoct.validation.syntax import check_syntax

PYPROJECT = """[project]
name = "demo"
version = "0.1.0"
dependencies = ["fastapi>=0.110", "uvicorn[standard]>=0.30,<1.0"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""


def spec(**overrides: object) -> ProjectSpec:
    values: dict[str, object] = {
        "name": "demo",
        "description": "d",
        "category": "library",
        "language": "python",
        "technologies": ["fastapi"],
    }
    values.update(overrides)
    return ProjectSpec(**values)  # type: ignore[arg-type]


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def good_tree() -> dict[str, str]:
    return {
        "README.md": "# demo\n",
        ".gitignore": "__pycache__/\n",
        "pyproject.toml": PYPROJECT,
        "src/demo/__init__.py": "VALUE = 1\n",
        "tests/test_demo.py": "def test_ok():\n    assert True\n",
    }


def statuses(report) -> dict[str, CheckStatus]:  # type: ignore[no-untyped-def]
    return {c.name: c.status for c in report.checks}


def test_valid_tree_passes_static_checks(tmp_path: Path) -> None:
    root = write_tree(tmp_path / "t", good_tree())
    report = Validator(run_commands=False).validate_tree(root, spec())
    assert report.passed, report.diagnostics()
    result = statuses(report)
    assert result["required files"] == CheckStatus.PASSED
    assert result["dependency metadata"] == CheckStatus.PASSED
    assert result["tests"] == CheckStatus.SKIPPED


def test_missing_files_and_tests_fail(tmp_path: Path) -> None:
    files = good_tree()
    del files["README.md"]
    del files["tests/test_demo.py"]
    root = write_tree(tmp_path / "t", files)
    report = Validator(run_commands=False).validate_tree(root, spec(license="MIT"))
    assert not report.passed
    failure = report.failures[0]
    assert "README.md" in failure.summary
    assert "tests" in failure.summary
    assert "LICENSE" in failure.summary
    # commands are not attempted once static checks fail
    assert statuses(report)["tests"] == CheckStatus.SKIPPED


def test_syntax_errors_reported_with_location(tmp_path: Path) -> None:
    files = good_tree() | {"src/demo/bad.py": "def f(:\n    pass\n"}
    report = Validator(run_commands=False).validate_tree(write_tree(tmp_path / "t", files), spec())
    assert not report.passed
    assert "src/demo/bad.py:1" in report.diagnostics()


def test_invalid_dependency_metadata(tmp_path: Path) -> None:
    files = good_tree()
    files["pyproject.toml"] = '[project]\nname = "demo"\ndependencies = ["not a valid req!!"]\n'
    report = Validator(run_commands=False).validate_tree(write_tree(tmp_path / "t", files), spec())
    diagnostics = report.diagnostics()
    assert "invalid requirement" in diagnostics
    assert "version" in diagnostics
    assert "build-system" in diagnostics


def test_missing_requested_technology_is_warning(tmp_path: Path) -> None:
    root = write_tree(tmp_path / "t", good_tree())
    report = Validator(run_commands=False).validate_tree(root, spec(technologies=["django"]))
    assert report.passed
    assert statuses(report)["dependency metadata"] == CheckStatus.WARNING


def test_secrets_fail_validation(tmp_path: Path) -> None:
    files = good_tree() | {"config.py": 'TOKEN = "ghp_' + "a" * 36 + '"\n'}
    report = Validator(run_commands=False).validate_tree(write_tree(tmp_path / "t", files), spec())
    assert statuses(report)["secrets"] == CheckStatus.FAILED


def test_package_json_checks(tmp_path: Path) -> None:
    files = {
        "README.md": "x",
        ".gitignore": "node_modules/",
        "package.json": '{"name": "x", "scripts": {}}',
        "src/index.ts": "export const x = 1;\n",
        "src/index.test.ts": "test\n",
    }
    report = Validator(run_commands=False).validate_tree(
        write_tree(tmp_path / "t", files), spec(language="typescript", technologies=[])
    )
    assert "scripts.test" in report.diagnostics()


@pytest.mark.parametrize(
    ("path", "content", "ok"),
    [
        ("a.py", "x = 1\n", True),
        ("a.py", "x = (\n", False),
        ("a.json", '{"a": 1}', True),
        ("a.json", "{", False),
        ("tsconfig.json", "{ // comments allowed\n}", True),
        ("a.toml", "a = 1\n", True),
        ("a.toml", "a = \n", False),
        ("a.yml", "a: [1, 2]\n", True),
        ("a.yaml", "a: [1, 2\n", False),
        ("a.md", "anything {", True),
    ],
)
def test_check_syntax(path: str, content: str, ok: bool) -> None:
    assert (check_syntax(path, content) is None) is ok


def test_sanitized_env_strips_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCOCT_ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("MY_SERVICE_PASSWORD", "x")
    monkeypatch.setenv("HARMLESS", "1")
    env = sanitized_env()
    for key in (
        "CONCOCT_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "MY_SERVICE_PASSWORD",
    ):
        assert key not in env
    assert env["HARMLESS"] == "1"
    assert env["PATH"] == os.environ["PATH"]


@pytest.mark.skipif(
    not os.environ.get("CONCOCT_RUN_INTEGRATION"), reason="set CONCOCT_RUN_INTEGRATION=1"
)
def test_commands_run_real_tests(tmp_path: Path) -> None:
    files = good_tree()
    files["pyproject.toml"] = (
        PYPROJECT.replace('["fastapi>=0.110", "uvicorn[standard]>=0.30,<1.0"]', "[]")
        + '\n[tool.hatch.build.targets.wheel]\npackages = ["src/demo"]\n'
    )
    files["tests/test_demo.py"] = (
        "from demo import VALUE\n\ndef test_value():\n    assert VALUE == 2\n"
    )
    report = Validator(run_commands=True).validate_tree(
        write_tree(tmp_path / "t", files), spec(technologies=[])
    )
    assert statuses(report)["tests"] == CheckStatus.FAILED
    assert "assert 1 == 2" in report.diagnostics()
