from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from git import Repo
from github import GithubException, UnknownObjectException

from concoct.github.client import (
    MARKER_TOPIC,
    GitHubError,
    GitHubService,
    NotConcoctRepositoryError,
    RepositoryExistsError,
    push_with_token,
)
from concoct.gitops.repository import GitRepository, Identity
from concoct.models import CommitKind, FileChange, ProjectSpec

TOKEN = "ghp_" + "t" * 36


def remote_repo(name: str, owner: str = "me", topics: list[str] | None = None) -> MagicMock:
    repo = MagicMock()
    repo.name = name
    repo.full_name = f"{owner}/{name}"
    repo.owner = SimpleNamespace(login=owner)
    repo.topics = topics if topics is not None else [MARKER_TOPIC]
    repo.html_url = f"https://github.com/{owner}/{name}"
    repo.clone_url = f"https://github.com/{owner}/{name}.git"
    repo.private = True
    repo.description = "desc"
    repo.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    repo.pushed_at = None
    return repo


def service(
    repos: dict[str, MagicMock] | None = None, **kwargs: Any
) -> tuple[GitHubService, MagicMock]:
    repos = repos or {}
    client = MagicMock()
    user = MagicMock()
    user.login = "me"
    user.get_repos.return_value = list(repos.values())
    client.get_user.return_value = user

    def get_repo(full_name: str) -> MagicMock:
        if full_name in repos:
            return repos[full_name]
        raise UnknownObjectException(404, {"message": "Not Found"}, None)

    client.get_repo.side_effect = get_repo
    return GitHubService(TOKEN, client=client, **kwargs), client


def spec() -> ProjectSpec:
    return ProjectSpec(
        name="demo", description="A demo", category="c", language="python", topics=["cli"]
    )


def test_list_generated_only_returns_marked_repos_owned_by_user() -> None:
    repos = {
        "me/mine": remote_repo("mine"),
        "me/unrelated": remote_repo("unrelated", topics=["python"]),
        "other/theirs": remote_repo("theirs", owner="other"),
    }
    svc, _ = service(repos)
    assert [r.full_name for r in svc.list_generated()] == ["me/mine"]


def test_create_refuses_existing_repository() -> None:
    svc, client = service({"me/demo": remote_repo("demo", topics=[])})
    with pytest.raises(RepositoryExistsError, match="never reuses"):
        svc.create_repository(spec(), "private")
    client.get_user.return_value.create_repo.assert_not_called()


def test_create_tags_repository_and_respects_visibility() -> None:
    svc, client = service()
    created = remote_repo("demo")
    client.get_user.return_value.create_repo.return_value = created
    svc.create_repository(spec(), "public")
    kwargs = client.get_user.return_value.create_repo.call_args.kwargs
    assert kwargs["name"] == "demo"
    assert kwargs["private"] is False
    assert kwargs["auto_init"] is False
    topics = created.replace_topics.call_args.args[0]
    assert topics[0] == MARKER_TOPIC and "cli" in topics


def test_create_rolls_back_if_tagging_fails() -> None:
    svc, client = service()
    created = remote_repo("demo")
    created.replace_topics.side_effect = GithubException(500, {"message": "boom"}, None)
    client.get_user.return_value.create_repo.return_value = created
    with pytest.raises(GitHubError, match="could not tag"):
        svc.create_repository(spec(), "private")
    created.delete.assert_called_once()


def test_publish_pushes_and_rolls_back_on_push_failure() -> None:
    pushed: list[tuple[str, str]] = []
    svc, client = service(pusher=lambda repo, url, token: pushed.append((url, token)))
    created = remote_repo("demo")
    client.get_user.return_value.create_repo.return_value = created
    info = svc.publish(MagicMock(), spec(), "private")
    assert pushed == [("https://github.com/me/demo.git", TOKEN)]
    assert info.full_name == "me/demo" and info.visibility == "private"

    def failing(repo: Any, url: str, token: str) -> None:
        raise RuntimeError(f"denied for {token}")

    svc2, client2 = service(pusher=failing)
    created2 = remote_repo("demo")
    client2.get_user.return_value.create_repo.return_value = created2
    with pytest.raises(GitHubError) as exc:
        svc2.publish(MagicMock(), spec(), "private")
    assert TOKEN not in str(exc.value)
    created2.delete.assert_called_once()


def test_delete_only_marked_repositories() -> None:
    marked = remote_repo("mine")
    unmarked = remote_repo("precious", topics=["python"])
    svc, _ = service({"me/mine": marked, "me/precious": unmarked})
    assert svc.delete("mine") == "me/mine"
    marked.delete.assert_called_once()
    with pytest.raises(NotConcoctRepositoryError, match="refusing"):
        svc.delete("precious")
    unmarked.delete.assert_not_called()
    with pytest.raises(GitHubError, match="does not exist"):
        svc.delete("missing")


def test_organisation_namespace() -> None:
    svc, client = service(owner="acme")
    org = MagicMock()
    org.get_repos.return_value = [remote_repo("x", owner="acme")]
    client.get_organization.return_value = org
    assert svc.owner == "acme"
    assert [r.full_name for r in svc.list_generated()] == ["acme/x"]


def test_push_with_token_preserves_history_and_never_stores_token(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    Repo.init(bare, bare=True)
    repo = GitRepository.create(tmp_path / "local")
    author = Identity("A", "a@example.invalid")
    for i in range(3):
        repo.apply([FileChange(path="f.txt", content=f"v{i}")])
        repo.commit(
            f"commit {i}",
            author=author,
            kind=CommitKind.FEATURE,
            index=i,
            when=datetime(2024, 1, i + 1, 12, tzinfo=UTC),
        )
    push_with_token(repo, str(bare), TOKEN)

    remote_log = [c.hexsha for c in Repo(bare).iter_commits("main")]
    local_log = [c.hexsha for c in repo.repo.iter_commits("main")]
    assert remote_log == local_log and len(remote_log) == 3
    assert Repo(bare).commit("main").authored_datetime == datetime(2024, 1, 3, 12, tzinfo=UTC)
    config = (repo.git_dir / "config").read_text()
    assert TOKEN not in config and "extraheader" not in config
    assert repo.repo.remote("origin").url == str(bare)


def test_push_does_not_force(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    Repo.init(bare, bare=True)
    other = GitRepository.create(tmp_path / "other")
    other.apply([FileChange(path="x.txt", content="unrelated")])
    other.commit(
        "unrelated",
        author=Identity("B", "b@example.invalid"),
        kind=CommitKind.CHORE,
        index=0,
        when=datetime(2024, 1, 1, tzinfo=UTC),
    )
    push_with_token(other, str(bare), TOKEN)

    mine = GitRepository.create(tmp_path / "mine")
    mine.apply([FileChange(path="y.txt", content="mine")])
    mine.commit(
        "mine",
        author=Identity("A", "a@example.invalid"),
        kind=CommitKind.CHORE,
        index=0,
        when=datetime(2024, 1, 2, tzinfo=UTC),
    )
    with pytest.raises(GitHubError, match="push failed"):
        push_with_token(mine, str(bare), TOKEN)
    assert Repo(bare).commit("main").summary == "unrelated"
