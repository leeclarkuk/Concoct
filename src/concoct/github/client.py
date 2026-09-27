"""GitHub integration (PyGithub).

Safety rules enforced here, not in the CLI:

* A repository is only ever *created* if the name is free; existing
  repositories are never reused, overwritten or force-pushed.
* ``list_generated`` and ``delete`` only consider repositories in the
  configured namespace that carry the ``concoct-generated`` topic.
* The token is never written into a remote URL or ``.git/config``: pushes
  authenticate through a transient HTTP header passed via the environment.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Any

from github import Auth, Github, GithubException, UnknownObjectException

from concoct.gitops.repository import GitRepository
from concoct.logging import get_logger
from concoct.models import ProjectSpec, RemoteInfo
from concoct.secrets import redact, registry

log = get_logger("github")

MARKER_TOPIC = "concoct-generated"
SYNTHETIC_TOPIC = "synthetic-data"
MAX_TOPICS = 20


class GitHubError(RuntimeError):
    pass


class RepositoryExistsError(GitHubError):
    pass


class NotConcoctRepositoryError(GitHubError):
    pass


@dataclass(frozen=True)
class RemoteRepository:
    full_name: str
    name: str
    html_url: str
    private: bool
    description: str
    created_at: datetime | None
    pushed_at: datetime | None


Pusher = Callable[[GitRepository, str, str], None]


def push_with_token(repo: GitRepository, clone_url: str, token: str) -> None:
    """Push ``main`` to ``clone_url`` without persisting credentials anywhere."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    registry.register(basic)
    env = dict(os.environ)
    # Append to (never clobber) any GIT_CONFIG_* entries already in the environment.
    count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    env[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraheader"
    env[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {basic}"
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env["GIT_TERMINAL_PROMPT"] = "0"
    branch = repo.branch
    try:
        repo.repo.git.push(clone_url, f"refs/heads/{branch}:refs/heads/{branch}", env=env)
    except Exception as exc:  # GitCommandError; message may contain the header
        raise GitHubError(f"git push failed: {redact(str(exc))}") from None
    remotes = {r.name for r in repo.repo.remotes}
    if "origin" in remotes:
        repo.repo.delete_remote(repo.repo.remote("origin"))
    repo.repo.create_remote("origin", clone_url)


class GitHubService:
    def __init__(
        self,
        token: str,
        owner: str | None = None,
        *,
        client: Github | None = None,
        pusher: Pusher = push_with_token,
    ) -> None:
        registry.register(token)
        self._token = token
        self._owner = owner
        self._gh = client or Github(auth=Auth.Token(token))
        self._pusher = pusher

    # ------------------------------------------------------------ identity
    @cached_property
    def user(self) -> Any:
        return self._gh.get_user()

    @property
    def owner(self) -> str:
        return self._owner or self.user.login

    @cached_property
    def _namespace(self) -> Any:
        if self._owner and self._owner.lower() != self.user.login.lower():
            return self._gh.get_organization(self._owner)
        return self.user

    def status(self) -> dict[str, Any]:
        try:
            login = self.user.login
            rate = self._gh.get_rate_limit()
            core = getattr(rate, "core", None) or getattr(
                getattr(rate, "resources", None), "core", None
            )
        except GithubException as exc:
            raise GitHubError(f"GitHub authentication failed: {_message(exc)}") from None
        return {
            "login": login,
            "owner": self.owner,
            "scopes": list(self._gh.oauth_scopes or []),
            "rate_remaining": getattr(core, "remaining", None),
        }

    # -------------------------------------------------------------- queries
    def _repos(self) -> list[Any]:
        if self._namespace is self.user:
            return list(self.user.get_repos(affiliation="owner"))
        return list(self._namespace.get_repos())

    def existing_names(self) -> set[str]:
        try:
            return {repo.name for repo in self._repos()}
        except GithubException as exc:
            raise GitHubError(f"could not list repositories: {_message(exc)}") from None

    @staticmethod
    def is_generated(repo: Any) -> bool:
        topics = getattr(repo, "topics", None)
        if topics is None:
            topics = repo.get_topics()
        return MARKER_TOPIC in (topics or [])

    def list_generated(self) -> list[RemoteRepository]:
        try:
            repos = self._repos()
        except GithubException as exc:
            raise GitHubError(f"could not list repositories: {_message(exc)}") from None
        owner = self.owner.lower()
        return [
            _to_remote(repo)
            for repo in repos
            if repo.owner.login.lower() == owner and self.is_generated(repo)
        ]

    def get_generated(self, name: str) -> Any:
        try:
            repo = self._gh.get_repo(f"{self.owner}/{name}")
        except UnknownObjectException:
            raise GitHubError(f"{self.owner}/{name} does not exist") from None
        except GithubException as exc:
            raise GitHubError(f"could not read {self.owner}/{name}: {_message(exc)}") from None
        if repo.owner.login.lower() != self.owner.lower() or not self.is_generated(repo):
            raise NotConcoctRepositoryError(
                f"{repo.full_name} was not created by Concoct (no '{MARKER_TOPIC}' topic); "
                "refusing to touch it"
            )
        return repo

    # ------------------------------------------------------------ mutations
    def create_repository(self, spec: ProjectSpec, visibility: str) -> Any:
        full_name = f"{self.owner}/{spec.name}"
        try:
            self._gh.get_repo(full_name)
        except UnknownObjectException:
            pass
        except GithubException as exc:
            raise GitHubError(f"could not check {full_name}: {_message(exc)}") from None
        else:
            raise RepositoryExistsError(
                f"{full_name} already exists; Concoct never reuses or overwrites repositories"
            )
        try:
            remote = self._namespace.create_repo(
                name=spec.name,
                description=spec.description[:340],
                private=visibility != "public",
                has_wiki=False,
                has_projects=False,
                auto_init=False,
            )
        except GithubException as exc:
            raise GitHubError(f"could not create {full_name}: {_message(exc)}") from None
        topics = [MARKER_TOPIC, SYNTHETIC_TOPIC, *spec.topics]
        try:
            remote.replace_topics(list(dict.fromkeys(topics))[:MAX_TOPICS])
        except GithubException as exc:
            # Without the marker the repo could never be managed or deleted by
            # Concoct, so roll back rather than leave an unmarked repository.
            self._rollback(remote)
            raise GitHubError(f"could not tag {full_name}: {_message(exc)}") from None
        return remote

    def publish(self, repo: GitRepository, spec: ProjectSpec, visibility: str) -> RemoteInfo:
        remote = self.create_repository(spec, visibility)
        try:
            self._pusher(repo, remote.clone_url, self._token)
        except Exception as exc:
            self._rollback(remote)
            raise GitHubError(f"push to {remote.full_name} failed: {redact(str(exc))}") from None
        return RemoteInfo(
            full_name=remote.full_name,
            html_url=remote.html_url,
            visibility="private" if remote.private else "public",
            pushed_at=datetime.now(UTC),
        )

    def delete(self, name: str) -> str:
        repo = self.get_generated(name)
        try:
            repo.delete()
        except GithubException as exc:
            raise GitHubError(f"could not delete {repo.full_name}: {_message(exc)}") from None
        return repo.full_name

    def _rollback(self, remote: Any) -> None:
        """Delete a repository this process created moments ago (best effort)."""
        try:
            remote.delete()
        except GithubException as exc:
            log.warning("could not roll back %s: %s", remote.full_name, _message(exc))


def _to_remote(repo: Any) -> RemoteRepository:
    return RemoteRepository(
        full_name=repo.full_name,
        name=repo.name,
        html_url=repo.html_url,
        private=bool(repo.private),
        description=repo.description or "",
        created_at=repo.created_at,
        pushed_at=repo.pushed_at,
    )


def _message(exc: GithubException) -> str:
    data = exc.data if isinstance(exc.data, dict) else {}
    return redact(f"{exc.status} {data.get('message', '')}".strip())
