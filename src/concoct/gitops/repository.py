"""Local Git repository operations built on GitPython."""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from git import Actor, Repo

from concoct.models import CommitKind, CommitRecord, FileChange, normalise_repo_path

DEFAULT_BRANCH = "main"
# Used when no author is configured. Deliberately not a real person, so that
# synthetic history is never attributed to anyone by default.
SYNTHETIC_IDENTITY_NAME = "Concoct Synthetic"
SYNTHETIC_IDENTITY_EMAIL = "synthetic@concoct.invalid"
# Files larger than this are not included in snapshots sent to the model.
MAX_SNAPSHOT_FILE_BYTES = 200_000


class GitOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    name: str
    email: str

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    def actor(self) -> Actor:
        return Actor(self.name, self.email)


class GitRepository:
    """A repository Concoct is building, one dated commit at a time."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.root = Path(repo.working_tree_dir or ".").resolve()

    # ------------------------------------------------------------------ setup
    @classmethod
    def create(cls, path: Path, branch: str = DEFAULT_BRANCH) -> GitRepository:
        path = Path(path)
        if path.exists() and any(path.iterdir()):
            raise GitOperationError(f"refusing to initialise non-empty directory {path}")
        path.mkdir(parents=True, exist_ok=True)
        repo = Repo.init(path, initial_branch=branch)
        with repo.config_writer() as cfg:
            # Keep generated repos independent of the host's global settings.
            cfg.set_value("commit", "gpgsign", "false")
            cfg.set_value("core", "autocrlf", "false")
        return cls(repo)

    @classmethod
    def open(cls, path: Path) -> GitRepository:
        return cls(Repo(path))

    @property
    def git_dir(self) -> Path:
        return Path(self.repo.git_dir)

    @property
    def branch(self) -> str:
        return self.repo.active_branch.name

    # ------------------------------------------------------------- file state
    def _resolve(self, rel_path: str) -> Path:
        rel = normalise_repo_path(rel_path)
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise GitOperationError(f"path escapes repository: {rel_path}")
        return target

    def read_file(self, rel_path: str) -> str | None:
        target = self._resolve(rel_path)
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def list_files(self) -> list[str]:
        """Working-tree files (tracked and untracked), excluding ignored ones."""
        tracked = set(self.repo.git.ls_files().splitlines())
        untracked = set(self.repo.untracked_files)
        return sorted(p for p in tracked | untracked if (self.root / p).is_file())

    def snapshot(self) -> dict[str, str]:
        """Mapping of path → text content of the current working tree."""
        files: dict[str, str] = {}
        for rel in self.list_files():
            full = self.root / rel
            try:
                if full.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
                    continue
                data = full.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue  # binary
            files[rel] = data.decode("utf-8", errors="replace")
        return files

    def apply(self, changes: list[FileChange]) -> list[str]:
        """Apply changes to the working tree; return paths that actually changed."""
        changed: list[str] = []
        for change in changes:
            target = self._resolve(change.path)
            if change.action == "delete":
                if target.is_file():
                    target.unlink()
                    changed.append(change.path)
                    self._prune_empty_dirs(target.parent)
                continue
            content = change.content or ""
            if not content.endswith("\n") and content:
                content += "\n"
            if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == content:
                continue
            if target.exists() and not target.is_file():
                raise GitOperationError(f"cannot write file over directory: {change.path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            if target.suffix in {".sh"} or content.startswith("#!"):
                target.chmod(target.stat().st_mode | 0o111)
            changed.append(change.path)
        return changed

    def _prune_empty_dirs(self, directory: Path) -> None:
        while directory != self.root and directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            directory = directory.parent

    # ---------------------------------------------------------------- commits
    def has_changes(self) -> bool:
        return self.repo.is_dirty(untracked_files=True)

    def commit(
        self,
        message: str,
        *,
        author: Identity,
        when: datetime,
        kind: CommitKind,
        index: int,
        repair: bool = False,
        committer: Identity | None = None,
    ) -> CommitRecord:
        if when.tzinfo is None:
            raise GitOperationError("commit timestamps must be timezone-aware")
        if not self.has_changes():
            raise GitOperationError("nothing to commit")
        # Stage everything under the working tree (respecting .gitignore),
        # including deletions.
        self.repo.git.add("--all")
        stamp = _git_date(when)
        commit = self.repo.index.commit(
            message.strip() + "\n",
            author=author.actor(),
            committer=(committer or author).actor(),
            author_date=stamp,
            commit_date=stamp,
            skip_hooks=True,
        )
        stats = commit.stats
        return CommitRecord(
            index=index,
            sha=commit.hexsha,
            kind=kind,
            message=message.strip(),
            authored_at=when,
            files_changed=sorted(str(p) for p in stats.files),
            insertions=int(stats.total.get("insertions", 0)),
            deletions=int(stats.total.get("deletions", 0)),
            repair=repair,
        )

    def history(self) -> list[dict[str, object]]:
        """Commits oldest-first with the metadata ``inspect`` shows."""
        if not self.repo.head.is_valid():
            return []
        entries = []
        for commit in reversed(list(self.repo.iter_commits(self.branch))):
            entries.append(
                {
                    "sha": commit.hexsha,
                    "subject": commit.summary,
                    "author": f"{commit.author.name} <{commit.author.email}>",
                    "authored_at": commit.authored_datetime,
                    "committed_at": commit.committed_datetime,
                    "files": len(commit.stats.files),
                    "insertions": commit.stats.total.get("insertions", 0),
                    "deletions": commit.stats.total.get("deletions", 0),
                }
            )
        return entries

    def export_head(self, destination: Path) -> Path:
        """Export the committed tree (exactly what history contains) to ``destination``."""
        destination.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        self.repo.archive(buffer, treeish="HEAD", format="tar")
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode="r") as archive:
            archive.extractall(destination, filter="data")
        return destination


def _git_date(when: datetime) -> str:
    """Git's internal date format: ``<unix seconds> <+hhmm offset>``."""
    offset = when.utcoffset()
    assert offset is not None
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{int(when.timestamp())} {sign}{hours:02d}{mins:02d}"
