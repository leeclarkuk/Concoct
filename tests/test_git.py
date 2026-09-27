from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from concoct.gitops.repository import GitOperationError, GitRepository, Identity
from concoct.models import CommitKind, FileChange

AUTHOR = Identity("Ada Example", "ada@example.invalid")


def make_repo(tmp_path: Path) -> GitRepository:
    return GitRepository.create(tmp_path / "repo")


def test_create_initialises_main_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert repo.branch == "main"
    assert (tmp_path / "repo" / ".git").is_dir()


def test_create_refuses_non_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "file.txt").write_text("x")
    with pytest.raises(GitOperationError, match="non-empty"):
        GitRepository.create(target)


def test_commits_carry_historical_dates_and_author(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    tz = timezone(timedelta(hours=2))
    when = datetime(2023, 3, 14, 9, 26, 53, tzinfo=tz)
    repo.apply([FileChange(path="README.md", content="# Hi")])
    record = repo.commit(
        "Initial commit", author=AUTHOR, when=when, kind=CommitKind.SCAFFOLD, index=0
    )
    commit = repo.repo.head.commit
    assert record.sha == commit.hexsha
    assert commit.authored_datetime == when
    assert commit.committed_datetime == when
    assert commit.author.name == "Ada Example"
    assert commit.author.email == "ada@example.invalid"
    assert commit.committer.email == "ada@example.invalid"
    assert commit.authored_datetime.utcoffset() == timedelta(hours=2)
    assert record.files_changed == ["README.md"]
    assert record.insertions == 1


def test_incremental_history_writes_and_deletes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    t0 = datetime(2024, 1, 1, 10, tzinfo=UTC)
    repo.apply(
        [
            FileChange(path="src/a.py", content="A = 1\n"),
            FileChange(path="src/b.py", content="B = 2\n"),
        ]
    )
    repo.commit("scaffold", author=AUTHOR, when=t0, kind=CommitKind.SCAFFOLD, index=0)

    assert repo.snapshot() == {"src/a.py": "A = 1\n", "src/b.py": "B = 2\n"}
    changed = repo.apply(
        [
            FileChange(path="src/a.py", content="A = 10"),
            FileChange(path="src/b.py", action="delete"),
            FileChange(path="src/c.py", content="C = 3\n"),
        ]
    )
    assert sorted(changed) == ["src/a.py", "src/b.py", "src/c.py"]
    record = repo.commit(
        "refactor", author=AUTHOR, when=t0 + timedelta(days=1), kind=CommitKind.REFACTOR, index=1
    )
    assert record.files_changed == ["src/a.py", "src/b.py", "src/c.py"]
    assert repo.snapshot() == {"src/a.py": "A = 10\n", "src/c.py": "C = 3\n"}

    history = repo.history()
    assert [h["subject"] for h in history] == ["scaffold", "refactor"]
    assert history[0]["authored_at"] < history[1]["authored_at"]


def test_unchanged_write_is_not_reported(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    repo.apply([FileChange(path="x.txt", content="same\n")])
    repo.commit(
        "add",
        author=AUTHOR,
        when=datetime(2024, 1, 1, tzinfo=UTC),
        kind=CommitKind.FEATURE,
        index=0,
    )
    assert repo.apply([FileChange(path="x.txt", content="same")]) == []
    with pytest.raises(GitOperationError, match="nothing to commit"):
        repo.commit(
            "noop",
            author=AUTHOR,
            when=datetime(2024, 1, 2, tzinfo=UTC),
            kind=CommitKind.CHORE,
            index=1,
        )


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", ".git/config", "a/../../b"])
def test_paths_outside_repository_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        FileChange(path=bad, content="x")


def test_naive_timestamp_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    repo.apply([FileChange(path="x.txt", content="x")])
    with pytest.raises(GitOperationError, match="timezone-aware"):
        repo.commit("x", author=AUTHOR, when=datetime(2024, 1, 1), kind=CommitKind.CHORE, index=0)


def test_export_head_contains_only_committed_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    repo.apply([FileChange(path="kept.txt", content="yes")])
    repo.commit(
        "add",
        author=AUTHOR,
        when=datetime(2024, 1, 1, tzinfo=UTC),
        kind=CommitKind.FEATURE,
        index=0,
    )
    (repo.root / "uncommitted.txt").write_text("no")
    exported = repo.export_head(tmp_path / "export")
    assert (exported / "kept.txt").read_text() == "yes\n"
    assert not (exported / "uncommitted.txt").exists()


def test_snapshot_respects_gitignore_and_skips_binary(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    repo.apply(
        [
            FileChange(path=".gitignore", content="*.log\n"),
            FileChange(path="app.py", content="print('hi')"),
        ]
    )
    (repo.root / "debug.log").write_text("noise")
    (repo.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert sorted(repo.snapshot()) == [".gitignore", "app.py"]
