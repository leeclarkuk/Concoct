from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from concoct.budget import Budget, MeteredProvider
from concoct.events import RecordingReporter
from concoct.gitops.repository import GitRepository
from concoct.manifest import read_manifest
from concoct.models import (
    CheckResult,
    CheckStatus,
    ProjectSpec,
    RemoteInfo,
    RepositoryStatus,
    ValidationReport,
)
from concoct.orchestrator import DISCLOSURE_MARKER, BatchRunner
from concoct.providers.offline import OfflineProvider
from concoct.validation.runner import Validator
from tests.conftest import ScriptedProvider, plan_payload


def run(settings, provider=None, **kwargs: Any):  # type: ignore[no-untyped-def]
    metered = MeteredProvider(
        provider or OfflineProvider(), Budget(settings.max_cost_usd, settings.max_total_tokens)
    )
    reporter = RecordingReporter()
    batch = BatchRunner(settings, metered, reporter, **kwargs).run()
    return batch, reporter


class FakeValidator(Validator):
    """Returns queued pass/fail reports instead of running checks."""

    def __init__(self, outcomes: list[bool]) -> None:
        super().__init__(run_commands=False)
        self.outcomes = outcomes
        self.calls = 0

    def validate(self, repo, spec, attempt=0):  # type: ignore[no-untyped-def]
        self.calls += 1
        ok = self.outcomes.pop(0)
        status = CheckStatus.PASSED if ok else CheckStatus.FAILED
        return ValidationReport(
            attempt=attempt,
            checks=[
                CheckResult(
                    name="tests", status=status, summary="1 failed", details="E assert 1 == 2"
                )
            ],
        )


def commit_reply(path: str, content: str) -> dict[str, Any]:
    return {"summary": "s", "changes": [{"path": path, "action": "write", "content": content}]}


# --------------------------------------------------------------- vertical slice
def test_offline_end_to_end_builds_incremental_history(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(technologies=["fastapi"], categories=["developer tool"])
    batch, reporter = run(settings)
    assert batch.succeeded
    result = batch.results[0]
    assert result.status == RepositoryStatus.SUCCEEDED
    assert 8 <= len(result.commits) <= 13
    assert result.validation is not None and result.validation.passed

    repo = Repo(result.path)
    log = list(reversed(list(repo.iter_commits("main"))))
    assert len(log) == len(result.commits)
    assert [c.hexsha for c in log] == [r.sha for r in result.commits]
    # genuinely incremental: every commit has a parent chain and changes files
    assert len(log[0].parents) == 0
    assert all(tuple(c.parents) == (p,) for p, c in itertools.pairwise(log))
    assert all(c.stats.files for c in log)
    dates = [c.authored_datetime for c in log]
    assert dates == sorted(dates)
    assert log[0].summary == "Initial project scaffold"

    readme = (Path(result.path) / "README.md").read_text()
    assert DISCLOSURE_MARKER in readme.lower()
    assert not repo.is_dirty(untracked_files=True)

    manifest = read_manifest(Path(result.path))
    assert manifest is not None
    assert manifest.result.status == RepositoryStatus.SUCCEEDED
    assert manifest.seed == 1234
    assert "anthropic_api_key" not in manifest.options
    # the manifest lives inside .git and is not part of history
    assert "concoct" not in repo.git.ls_files()

    names = reporter.names()
    assert names.index("plan") < names.index("commit") < names.index("validation")
    assert names[-1] == "repo_finished"


def test_same_seed_reproduces_history_shape(make_settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    end = datetime(2025, 1, 1, tzinfo=UTC)
    first, _ = run(make_settings(output_dir=tmp_path / "a", end_date=end))
    second, _ = run(make_settings(output_dir=tmp_path / "b", end_date=end))
    a, b = first.results[0], second.results[0]
    assert [c.message for c in a.commits] == [c.message for c in b.commits]
    assert [c.authored_at for c in a.commits] == [c.authored_at for c in b.commits]
    # identical content, dates and author => identical commit hashes
    assert [c.sha for c in a.commits] == [c.sha for c in b.commits]


def test_multiple_repos_get_distinct_names(make_settings) -> None:  # type: ignore[no-untyped-def]
    batch, _ = run(make_settings(repos=2))
    names = [r.name for r in batch.results]
    assert len(set(names)) == 2
    assert all(r.status == RepositoryStatus.SUCCEEDED for r in batch.results)


def test_custom_author_identity(make_settings) -> None:  # type: ignore[no-untyped-def]
    batch, _ = run(make_settings(author_name="Test Person", author_email="t@example.invalid"))
    commit = Repo(batch.results[0].path).head.commit
    assert (commit.author.name, commit.author.email) == ("Test Person", "t@example.invalid")


def test_no_disclosure_when_disabled(make_settings) -> None:  # type: ignore[no-untyped-def]
    batch, _ = run(make_settings(synthetic_disclosure=False))
    readme = (Path(batch.results[0].path) / "README.md").read_text().lower()
    assert DISCLOSURE_MARKER not in readme


# ------------------------------------------------------------------ repair loop
def scripted_repo_provider(extra: list[Any]) -> ScriptedProvider:
    provider = ScriptedProvider([plan_payload(2)])
    provider.queue(
        commit_reply("README.md", "# tiny\n"),
        commit_reply("src/tiny/core.py", "VALUE = 1\n"),
        *extra,
    )
    return provider


def test_repair_commit_fixes_failed_validation(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(min_commits=2, max_commits=4, max_repair_attempts=2)
    provider = scripted_repo_provider(
        [
            {
                "message": "fix: correct VALUE",
                "changes": [
                    {"path": "src/tiny/core.py", "action": "write", "content": "VALUE = 2\n"}
                ],
            },
        ]
    )
    validator = FakeValidator([False, True])
    batch, reporter = run(settings, provider, validator=validator)
    result = batch.results[0]
    assert result.status == RepositoryStatus.SUCCEEDED
    assert result.repair_attempts == 1
    assert result.commits[-1].repair is True
    assert result.commits[-1].message == "fix: correct VALUE"
    assert result.commits[-1].authored_at > result.commits[-2].authored_at
    assert "E assert 1 == 2" in provider.requests[-1].prompt
    assert validator.calls == 2
    assert "repair" in reporter.names()


def test_repair_is_bounded(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(min_commits=2, max_commits=4, max_repair_attempts=2)
    fix = {
        "message": "fix: try",
        "changes": [{"path": "src/tiny/core.py", "action": "write", "content": "VALUE = 3\n"}],
    }
    fix2 = {
        "message": "fix: try again",
        "changes": [{"path": "src/tiny/core.py", "action": "write", "content": "VALUE = 4\n"}],
    }
    provider = scripted_repo_provider([fix, fix2])
    validator = FakeValidator([False, False, False])
    batch, _ = run(settings, provider, validator=validator)
    result = batch.results[0]
    assert result.status == RepositoryStatus.VALIDATION_FAILED
    assert result.repair_attempts == 2
    assert validator.calls == 3
    assert not batch.succeeded
    assert "validation failed" in (result.error or "")


def test_no_repairs_when_disabled(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(min_commits=2, max_commits=2, max_repair_attempts=0)
    provider = scripted_repo_provider([])
    batch, _ = run(settings, provider, validator=FakeValidator([False]))
    assert batch.results[0].status == RepositoryStatus.VALIDATION_FAILED
    assert batch.results[0].repair_attempts == 0


# --------------------------------------------------------------- failure modes
def test_generation_failure_keeps_partial_repo_with_manifest(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(min_commits=2, max_commits=2)
    provider = ScriptedProvider(
        [plan_payload(2), commit_reply("README.md", "# x"), "garbage", "more garbage"]
    )
    batch, _ = run(settings, provider)
    result = batch.results[0]
    assert result.status == RepositoryStatus.FAILED
    assert "commit 2" in (result.error or "")
    assert len(result.commits) == 1
    manifest = read_manifest(Path(result.path))
    assert manifest is not None and manifest.result.status == RepositoryStatus.FAILED


def test_budget_exhaustion_stops_the_batch(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(repos=3, max_total_tokens=5_000, max_cost_usd=None)
    batch, reporter = run(settings)
    assert batch.results[0].status == RepositoryStatus.BUDGET_EXCEEDED
    assert len(batch.results) == 1
    assert any("budget" in str(payload) for name, payload in reporter.events if name == "warning")


def test_planning_failure(make_settings) -> None:  # type: ignore[no-untyped-def]
    batch, _ = run(make_settings(languages=["cobol"]))
    assert batch.results[0].status == RepositoryStatus.FAILED
    assert "Python template" in (batch.results[0].error or "")
    assert batch.results[0].path is None


def test_existing_directory_is_never_overwritten(make_settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    (out / "hooklens").mkdir(parents=True)
    (out / "hooklens" / "precious.txt").write_text("keep me")
    batch, _ = run(make_settings(output_dir=out))
    assert batch.results[0].name != "hooklens"
    assert (out / "hooklens" / "precious.txt").read_text() == "keep me"


# ------------------------------------------------------------------ publishing
class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[str] = []

    def existing_names(self) -> set[str]:
        return {"hooklens"}

    def publish(self, repo: GitRepository, spec: ProjectSpec, visibility: str) -> RemoteInfo:
        if self.fail:
            raise RuntimeError("remote exploded ghp_" + "a" * 36)
        self.published.append(spec.name)
        return RemoteInfo(
            full_name=f"me/{spec.name}",
            html_url=f"https://github.com/me/{spec.name}",
            visibility="private",
        )


def test_push_requires_publisher(make_settings) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="publisher"):
        run(make_settings(push=True))


def test_publisher_not_used_without_push(make_settings) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    batch, _ = run(make_settings(), publisher=publisher)
    assert publisher.published == []
    assert batch.results[0].remote is None


def test_publish_with_cleanup(make_settings) -> None:  # type: ignore[no-untyped-def]
    publisher = FakePublisher()
    batch, _ = run(make_settings(push=True, cleanup=True), publisher=publisher)
    result = batch.results[0]
    # remote names are avoided when planning
    assert publisher.published == [result.name] and result.name != "hooklens"
    assert result.remote is not None
    assert result.path is None


def test_publish_failure_keeps_local_copy_and_redacts(make_settings) -> None:  # type: ignore[no-untyped-def]
    batch, _ = run(make_settings(push=True, cleanup=True), publisher=FakePublisher(fail=True))
    result = batch.results[0]
    assert result.status == RepositoryStatus.FAILED
    assert "ghp_" not in (result.error or "")
    assert result.path and Path(result.path).is_dir()
