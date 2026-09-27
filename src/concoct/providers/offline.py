"""Deterministic, credential-free provider.

The offline provider answers the same ``plan``/``commit``/``repair`` requests
as Claude, but from a built-in project template instead of a model. It exists
so the complete pipeline (planning, incremental history, validation, GitHub
publishing) can be exercised in CI, demos and air-gapped environments without
an API key or spend.

The template is a small FastAPI developer tool, "hooklens", that captures and
inspects incoming webhooks. Its history is described as *stages*; every file
is rendered as a pure function of the set of stages applied so far, so any
subset of stages (chosen to fit the requested commit count) yields a coherent,
working repository at every commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from concoct.providers.base import LLMRequest, LLMResponse, ProviderError, TokenUsage

PACKAGE = "hooklens"
SUPPORTED_LANGUAGES = ("python", "py", "python3")


@dataclass(frozen=True)
class Stage:
    key: str
    kind: str
    message: str
    intent: str
    files: tuple[str, ...]


ROUTES = "@routes"  # resolved to app.py (and api.py once the router exists)

STAGES: tuple[Stage, ...] = (
    Stage(
        "scaffold",
        "scaffold",
        "Initial project scaffold",
        "Create the package layout with a FastAPI app factory exposing GET /health, "
        "pyproject.toml with FastAPI/uvicorn and a dev extra, a .gitignore, a short README "
        "and a first test for the health endpoint.",
        (
            "pyproject.toml",
            ".gitignore",
            "README.md",
            "LICENSE",
            "src/hooklens/__init__.py",
            "src/hooklens/__main__.py",
            "src/hooklens/app.py",
            "tests/test_health.py",
        ),
    ),
    Stage(
        "capture",
        "feature",
        "feat: capture incoming webhook requests",
        "Add a CapturedRequest model, an in-memory RequestStore, a POST/PUT/PATCH "
        "/hooks/{channel} endpoint that records method, path, query, headers and body, and "
        "GET /hooks/{channel} listing the newest requests first.",
        ("src/hooklens/models.py", "src/hooklens/store.py", ROUTES),
    ),
    Stage(
        "capture_tests",
        "test",
        "test: cover webhook capture and listing",
        "Add a TestClient fixture and tests for capturing, ordering and channel isolation.",
        ("tests/conftest.py", "tests/test_capture.py"),
    ),
    Stage(
        "inspect",
        "feature",
        "feat: inspect individual captured requests",
        "Add RequestStore.get and GET /requests/{request_id}, returning 404 for unknown ids, "
        "with tests.",
        ("src/hooklens/store.py", ROUTES, "tests/conftest.py", "tests/test_capture.py"),
    ),
    Stage(
        "tooling",
        "config",
        "build: configure ruff and pytest",
        "Add ruff to the dev extra and configure [tool.ruff] and [tool.pytest.ini_options].",
        ("pyproject.toml",),
    ),
    Stage(
        "limit_bug",
        "feature",
        "feat: make per-channel history limit configurable\n\n"
        "Reads HOOKLENS_MAX_PER_CHANNEL so long-running sessions do not grow without bound.",
        "Add a Settings dataclass loaded from HOOKLENS_MAX_PER_CHANNEL, pass it through "
        "create_app and cap each channel's history in RequestStore.add.",
        ("src/hooklens/settings.py", "src/hooklens/store.py", ROUTES, "tests/test_settings.py"),
    ),
    Stage(
        "limit_fix",
        "fix",
        "fix: evict oldest requests first when a channel is full\n\n"
        "RequestStore.add dropped the request it had just stored instead of the oldest "
        "one, so full channels stopped showing new deliveries.",
        "Fix the eviction in RequestStore.add to discard the oldest entries and add a "
        "regression test.",
        ("src/hooklens/store.py", "tests/test_store.py"),
    ),
    Stage(
        "router",
        "refactor",
        "refactor: move routes into an APIRouter module",
        "Move the capture/inspection endpoints from app.py into hooklens/api.py using an "
        "APIRouter and a get_store dependency; create_app keeps the store on app.state.",
        ("src/hooklens/app.py", "src/hooklens/api.py"),
    ),
    Stage(
        "filter",
        "feature",
        "feat: filter captured requests by method and body text",
        "Support ?method= and ?contains= query parameters on GET /hooks/{channel}.",
        ("src/hooklens/store.py", ROUTES),
    ),
    Stage(
        "curl",
        "feature",
        "feat: export captured requests as curl commands",
        "Add hooklens.curl.to_curl and GET /requests/{request_id}/curl returning a "
        "plain-text command that replays the request.",
        ("src/hooklens/curl.py", "src/hooklens/store.py", ROUTES),
    ),
    Stage(
        "extra_tests",
        "test",
        "test: add coverage for filters and curl export",
        "Test method/body filtering and the curl export endpoint and helper.",
        ("tests/conftest.py", "tests/test_extras.py"),
    ),
    Stage(
        "docs",
        "docs",
        "docs: document endpoints and configuration",
        "Expand the README with an endpoint reference, configuration and development notes.",
        ("README.md",),
    ),
    Stage(
        "ci",
        "ci",
        "ci: run lint and tests on GitHub Actions",
        "Add a GitHub Actions workflow installing the dev extra and running the checks.",
        (".github/workflows/ci.yml",),
    ),
    Stage(
        "clear",
        "feature",
        "feat: allow clearing a channel's history",
        "Add RequestStore.clear and DELETE /hooks/{channel} returning the number removed.",
        ("src/hooklens/store.py", ROUTES, "tests/conftest.py", "tests/test_capture.py"),
    ),
    Stage(
        "pins",
        "deps",
        "build: raise minimum dependency versions",
        "Require fastapi>=0.115 and uvicorn[standard]>=0.30 with upper bounds below 1.0.",
        ("pyproject.toml",),
    ),
    Stage(
        "make",
        "chore",
        "chore: add Makefile with common development tasks",
        "Add install, test, lint and run targets.",
        ("Makefile",),
    ),
)

# Which stages to keep when fewer commits are requested (most important first).
PRIORITY = (
    "scaffold",
    "capture",
    "capture_tests",
    "inspect",
    "limit_bug",
    "limit_fix",
    "router",
    "curl",
    "docs",
    "ci",
    "filter",
    "extra_tests",
    "tooling",
    "clear",
    "pins",
    "make",
)
MAX_COMMITS = len(STAGES)
_BY_MESSAGE = {stage.message: stage for stage in STAGES}
_BY_KEY = {stage.key: stage for stage in STAGES}
_NAMES = ("hooklens", "hookshelf", "webhook-lens", "hookscope", "deliverybox")


def select_stages(count: int) -> list[Stage]:
    count = max(2, min(count, MAX_COMMITS))
    chosen = set(PRIORITY[:count])
    return [stage for stage in STAGES if stage.key in chosen]


class OfflineProvider:
    """Template-backed provider. Costs nothing, needs no network."""

    name = "offline"
    model = "offline"

    def complete(self, request: LLMRequest) -> LLMResponse:
        handler = {"plan": self._plan, "commit": self._commit, "repair": self._repair}.get(
            request.task
        )
        if handler is None:
            raise ProviderError(f"offline provider cannot handle task {request.task!r}")
        payload = handler(request.context)
        text = json.dumps(payload, indent=1)
        usage = TokenUsage(input_tokens=len(request.prompt) // 4, output_tokens=len(text) // 4)
        return LLMResponse(text=text, usage=usage, model=self.model, stop_reason="end_turn")

    # ------------------------------------------------------------------ plan
    def _plan(self, context: dict[str, Any]) -> dict[str, Any]:
        req = context.get("request", {})
        language = str(req.get("language", "python")).lower()
        if language not in SUPPORTED_LANGUAGES:
            raise ProviderError(
                f"the offline provider only has a Python template (requested {language!r}); "
                "use the anthropic provider for other languages"
            )
        existing = set(req.get("existing_names", ()))
        name = next((n for n in _NAMES if n not in existing), f"{_NAMES[0]}-{len(existing) + 1}")
        stages = select_stages(int(req.get("commit_count", 10)))
        return {
            "project": {
                "name": name,
                "title": "Hooklens",
                "description": "Capture, inspect and replay incoming webhooks during local "
                "development.",
                "architecture": (
                    "A FastAPI application created by an app factory (hooklens.app). "
                    "Captured requests are pydantic models (hooklens.models) kept in a "
                    "thread-safe in-memory RequestStore (hooklens.store) with a configurable "
                    "per-channel limit (hooklens.settings). HTTP routes live in hooklens.api and "
                    "hooklens.curl renders stored requests as curl commands."
                ),
                "features": [
                    "Capture webhook deliveries on named channels",
                    "List and inspect captured requests",
                    "Filter by HTTP method and body text",
                    "Export any request as a curl command",
                ],
                "topics": ["webhooks", "fastapi", "developer-tools", "debugging"],
            },
            "commits": [
                {
                    "kind": s.kind,
                    "message": s.message,
                    "intent": s.intent,
                    "files": [f for f in s.files if f != ROUTES],
                }
                for s in stages
            ],
        }

    # ---------------------------------------------------------------- commit
    def _commit(self, context: dict[str, Any]) -> dict[str, Any]:
        plan = context["plan"]
        step = int(context["step_index"])
        files: dict[str, str] = context.get("files", {})
        applied: list[Stage] = []
        for commit in plan["commits"][: step + 1]:
            stage = _BY_MESSAGE.get(commit["message"])
            if stage is None:
                raise ProviderError("offline provider received a plan it did not create")
            applied.append(stage)
        flags = {stage.key for stage in applied}
        project = Project(flags, license=plan["spec"].get("license"))
        current = applied[-1]
        targets: list[str] = []
        for path in current.files:
            if path == ROUTES:
                targets += ["src/hooklens/app.py"]
                if "router" in flags:
                    targets.append("src/hooklens/api.py")
            else:
                targets.append(path)
        changes = []
        for path in dict.fromkeys(targets):
            content = project.render(path)
            if content is None:
                continue
            if files.get(path, "").rstrip("\n") == content.rstrip("\n"):
                continue
            changes.append({"path": path, "action": "write", "content": content})
        return {"summary": current.intent, "changes": changes}

    def _repair(self, context: dict[str, Any]) -> dict[str, Any]:
        # The template is correct by construction; there is nothing to repair.
        return {"message": "fix: address validation failures", "changes": []}


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------
class Project:
    def __init__(self, flags: set[str], license: str | None = None) -> None:
        self.f = flags
        self.license = license

    def has(self, *keys: str) -> bool:
        return all(k in self.f for k in keys)

    def render(self, path: str) -> str | None:
        renderers = {
            "pyproject.toml": self.pyproject,
            ".gitignore": self.gitignore,
            "README.md": self.readme,
            "LICENSE": self.license_text,
            "src/hooklens/__init__.py": self.init,
            "src/hooklens/__main__.py": self.main,
            "src/hooklens/app.py": self.app,
            "src/hooklens/api.py": self.api,
            "src/hooklens/models.py": self.models,
            "src/hooklens/store.py": self.store,
            "src/hooklens/settings.py": self.settings,
            "src/hooklens/curl.py": self.curl,
            "tests/test_health.py": self.test_health,
            "tests/conftest.py": self.conftest,
            "tests/test_capture.py": self.test_capture,
            "tests/test_settings.py": self.test_settings,
            "tests/test_store.py": self.test_store,
            "tests/test_extras.py": self.test_extras,
            ".github/workflows/ci.yml": self.ci,
            "Makefile": self.makefile,
        }
        renderer = renderers.get(path)
        return renderer() if renderer else None

    # -- metadata ---------------------------------------------------------
    def pyproject(self) -> str:
        if self.has("pins"):
            deps = ['"fastapi>=0.115,<1.0"', '"uvicorn[standard]>=0.30,<1.0"']
        else:
            deps = ['"fastapi>=0.110"', '"uvicorn>=0.29"']
        dev = ['"httpx2>=2.0"', '"pytest>=8.0"'] + (['"ruff>=0.6"'] if self.has("tooling") else [])
        license_line = '\nlicense = { text = "MIT" }' if self.license else ""
        text = f"""[project]
name = "hooklens"
version = "0.1.0"
description = "Capture, inspect and replay incoming webhooks during local development."
readme = "README.md"
requires-python = ">=3.12"{license_line}
dependencies = [
{_items(deps)}
]

[project.optional-dependencies]
dev = [
{_items(dev)}
]

[project.scripts]
hooklens = "hooklens.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/hooklens"]
"""
        if self.has("tooling"):
            text += """
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""
        return text

    def gitignore(self) -> str:
        return """__pycache__/
*.py[cod]
.venv/
venv/
.env
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/
"""

    def license_text(self) -> str | None:
        if not self.license:
            return None
        if self.license.upper() != "MIT":
            return f"{self.license} License\n\nCopyright (c) Hooklens contributors\n"
        return """MIT License

Copyright (c) Hooklens contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

    def readme(self) -> str:
        text = """# Hooklens

Capture, inspect and replay incoming webhooks during local development.

## Quick start

```bash
pip install -e ".[dev]"
python -m hooklens          # serves on http://127.0.0.1:8080
```
"""
        if not self.has("docs"):
            return text
        rows = ["| `GET` | `/health` | Liveness check and version |"]
        if self.has("capture"):
            rows += [
                "| `POST`/`PUT`/`PATCH` | `/hooks/{channel}` | Capture a delivery |",
                "| `GET` | `/hooks/{channel}` | List captured requests, newest first |",
            ]
        if self.has("clear"):
            rows.append("| `DELETE` | `/hooks/{channel}` | Clear a channel |")
        if self.has("inspect"):
            rows.append("| `GET` | `/requests/{id}` | Inspect one request |")
        if self.has("curl"):
            rows.append("| `GET` | `/requests/{id}/curl` | Replay command for a request |")
        text += "\n## Endpoints\n\n| Method | Path | Description |\n| --- | --- | --- |\n"
        text += "\n".join(rows) + "\n"
        if self.has("filter"):
            text += (
                "\n`GET /hooks/{channel}` accepts `method` (e.g. `?method=POST`) and "
                "`contains` (substring of the body) query parameters.\n"
            )
        text += "\n## Example\n\n```bash\n"
        text += 'curl -X POST localhost:8080/hooks/github -d \'{"action": "opened"}\'\n'
        text += "curl localhost:8080/hooks/github\n```\n"
        text += "\n## Configuration\n\n| Variable | Default | Description |\n| --- | --- | --- |\n"
        text += "| `HOOKLENS_HOST` | `127.0.0.1` | Interface to bind |\n"
        text += "| `HOOKLENS_PORT` | `8080` | Port to listen on |\n"
        if self.has("limit_bug"):
            text += "| `HOOKLENS_MAX_PER_CHANNEL` | `100` | Requests kept per channel |\n"
        text += "\n## Development\n\n```bash\npython -m pytest\n"
        if self.has("tooling"):
            text += "ruff check .\n"
        text += "```\n"
        return text

    # -- package ----------------------------------------------------------
    def init(self) -> str:
        return '''"""Hooklens: capture and inspect webhooks during local development."""

__version__ = "0.1.0"
'''

    def main(self) -> str:
        return '''"""Run the Hooklens server with ``python -m hooklens``."""

import os

import uvicorn


def main() -> None:
    host = os.environ.get("HOOKLENS_HOST", "127.0.0.1")
    port = int(os.environ.get("HOOKLENS_PORT", "8080"))
    uvicorn.run("hooklens.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
'''

    def models(self) -> str:
        return '''"""Data models for captured webhook requests."""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    return uuid4().hex[:12]


class CapturedRequest(BaseModel):
    """A single HTTP request received on a capture channel."""

    id: str = Field(default_factory=_new_id)
    channel: str
    method: str
    path: str
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
'''

    def settings(self) -> str:
        return '''"""Runtime configuration read from environment variables."""

import os
from dataclasses import dataclass
from typing import Self

DEFAULT_MAX_PER_CHANNEL = 100


@dataclass(frozen=True)
class Settings:
    max_per_channel: int = DEFAULT_MAX_PER_CHANNEL

    @classmethod
    def from_env(cls) -> Self:
        raw = os.environ.get("HOOKLENS_MAX_PER_CHANNEL", str(DEFAULT_MAX_PER_CHANNEL))
        value = int(raw)
        if value < 1:
            raise ValueError("HOOKLENS_MAX_PER_CHANNEL must be at least 1")
        return cls(max_per_channel=value)
'''

    def store(self) -> str:
        limited = self.has("limit_bug")
        lines = [
            '"""In-memory storage for captured requests."""',
            "",
            "from threading import Lock",
            "",
            "from hooklens.models import CapturedRequest",
        ]
        if limited:
            lines.append("from hooklens.settings import DEFAULT_MAX_PER_CHANNEL")
        lines += [
            "",
            "",
            "class RequestStore:",
            '    """Keeps captured requests per channel, oldest first."""',
            "",
        ]
        if limited:
            lines += [
                "    def __init__(self, max_per_channel: int = DEFAULT_MAX_PER_CHANNEL) -> None:",
                "        self._max_per_channel = max_per_channel",
            ]
        else:
            lines.append("    def __init__(self) -> None:")
        lines += [
            "        self._channels: dict[str, list[CapturedRequest]] = {}",
            "        self._lock = Lock()",
            "",
            "    def add(self, request: CapturedRequest) -> None:",
            "        with self._lock:",
            "            items = self._channels.setdefault(request.channel, [])",
            "            items.append(request)",
        ]
        if self.has("limit_fix"):
            lines += [
                "            overflow = len(items) - self._max_per_channel",
                "            if overflow > 0:",
                "                del items[:overflow]",
            ]
        elif limited:
            lines += [
                "            if len(items) > self._max_per_channel:",
                "                items.pop()",
            ]
        lines.append("")
        if self.has("filter"):
            lines += [
                "    def recent(",
                "        self, channel: str, *, method: str | None = None, "
                "contains: str | None = None",
                "    ) -> list[CapturedRequest]:",
                '        """Requests on ``channel``, newest first, optionally filtered."""',
                "        with self._lock:",
                "            items = list(reversed(self._channels.get(channel, [])))",
                "        if method:",
                "            items = [item for item in items if item.method == method.upper()]",
                "        if contains:",
                "            items = [item for item in items if contains in item.body]",
                "        return items",
            ]
        else:
            lines += [
                "    def recent(self, channel: str) -> list[CapturedRequest]:",
                '        """Requests on ``channel``, newest first."""',
                "        with self._lock:",
                "            return list(reversed(self._channels.get(channel, [])))",
            ]
        if self.has("inspect") or self.has("curl"):
            lines += [
                "",
                "    def get(self, request_id: str) -> CapturedRequest | None:",
                "        with self._lock:",
                "            for items in self._channels.values():",
                "                for item in items:",
                "                    if item.id == request_id:",
                "                        return item",
                "        return None",
            ]
        if self.has("clear"):
            lines += [
                "",
                "    def clear(self, channel: str) -> int:",
                '        """Remove every request on ``channel``; return how many were removed."""',
                "        with self._lock:",
                "            return len(self._channels.pop(channel, []))",
            ]
        return "\n".join(lines) + "\n"

    def curl(self) -> str:
        return '''"""Render captured requests as reproducible curl commands."""

import shlex
from urllib.parse import urlencode

from hooklens.models import CapturedRequest

_SKIPPED_HEADERS = {"host", "content-length", "connection", "accept-encoding", "user-agent"}


def to_curl(request: CapturedRequest, base_url: str = "http://localhost:8080") -> str:
    """Return a curl command that replays ``request`` against ``base_url``."""
    url = f"{base_url.rstrip('/')}{request.path}"
    if request.query:
        url += "?" + urlencode(request.query)
    parts = ["curl", "-X", request.method, shlex.quote(url)]
    for name, value in sorted(request.headers.items()):
        if name.lower() in _SKIPPED_HEADERS:
            continue
        parts += ["-H", shlex.quote(f"{name}: {value}")]
    if request.body:
        parts += ["--data-raw", shlex.quote(request.body)]
    return " ".join(parts)
'''

    # -- routes -------------------------------------------------------------
    def _route_functions(self, decorator: str, store_expr: str, store_param: str) -> list[str]:
        """Route handlers shared by the inline (app.py) and router (api.py) layouts."""
        sp = f", {store_param}" if store_param else ""
        lines = [
            f'@{decorator}.api_route("/hooks/{{channel}}", methods=["POST", "PUT", "PATCH"], '
            "status_code=201)",
            f"async def capture(channel: str, request: Request{sp}) -> CapturedRequest:",
            '    body = (await request.body()).decode("utf-8", errors="replace")',
            "    captured = CapturedRequest(",
            "        channel=channel,",
            "        method=request.method,",
            "        path=request.url.path,",
            "        query=dict(request.query_params),",
            "        headers=dict(request.headers),",
            "        body=body,",
            "    )",
            f"    {store_expr}.add(captured)",
            "    return captured",
            "",
            "",
            f'@{decorator}.get("/hooks/{{channel}}")',
        ]
        if self.has("filter"):
            lines += [
                "def list_requests(",
                f"    channel: str{sp}, method: str | None = None, contains: str | None = None",
                ") -> list[CapturedRequest]:",
                f"    return {store_expr}.recent(channel, method=method, contains=contains)",
            ]
        else:
            lines += [
                f"def list_requests(channel: str{sp}) -> list[CapturedRequest]:",
                f"    return {store_expr}.recent(channel)",
            ]
        if self.has("clear"):
            lines += [
                "",
                "",
                f'@{decorator}.delete("/hooks/{{channel}}")',
                f"def clear_channel(channel: str{sp}) -> dict[str, int]:",
                f'    return {{"deleted": {store_expr}.clear(channel)}}',
            ]
        if self.has("inspect") or self.has("curl"):
            lines += [
                "",
                "",
                "def _find(store: RequestStore, request_id: str) -> CapturedRequest:",
                "    captured = store.get(request_id)",
                "    if captured is None:",
                '        raise HTTPException(status_code=404, detail="request not found")',
                "    return captured",
            ]
        if self.has("inspect"):
            lines += [
                "",
                "",
                f'@{decorator}.get("/requests/{{request_id}}")',
                f"def get_request(request_id: str{sp}) -> CapturedRequest:",
                f"    return _find({store_expr}, request_id)",
            ]
        if self.has("curl"):
            lines += [
                "",
                "",
                f'@{decorator}.get("/requests/{{request_id}}/curl", '
                "response_class=PlainTextResponse)",
                f"def get_request_as_curl(request_id: str, request: Request{sp}) -> str:",
                f"    captured = _find({store_expr}, request_id)",
                "    return to_curl(captured, base_url=str(request.base_url))",
            ]
        return lines

    def _route_imports(self, fastapi_names: list[str]) -> list[str]:
        names = list(fastapi_names)
        if self.has("inspect") or self.has("curl"):
            names.append("HTTPException")
        names.append("Request")
        lines = [f"from fastapi import {', '.join(sorted(set(names)))}"]
        if self.has("curl"):
            lines.append("from fastapi.responses import PlainTextResponse")
        return lines

    def app(self) -> str:
        limited = self.has("limit_bug")
        router = self.has("router")
        capture = self.has("capture")
        lines = ['"""FastAPI application factory for Hooklens."""', ""]
        if router:
            lines.append("from fastapi import FastAPI")
        elif capture:
            lines += self._route_imports(["FastAPI"])
        else:
            lines.append("from fastapi import FastAPI")
        lines += ["", "from hooklens import __version__"]
        if router:
            lines.append("from hooklens.api import router")
        elif capture:
            if self.has("curl"):
                lines.append("from hooklens.curl import to_curl")
            lines.append("from hooklens.models import CapturedRequest")
        if limited:
            lines.append("from hooklens.settings import Settings")
        if capture:
            lines.append("from hooklens.store import RequestStore")
        lines += ["", ""]
        signature = (
            "def create_app(settings: Settings | None = None) -> FastAPI:"
            if limited
            else "def create_app() -> FastAPI:"
        )
        lines += [signature, '    app = FastAPI(title="Hooklens", version=__version__)']
        if limited:
            lines.append("    settings = settings or Settings.from_env()")
        if capture:
            ctor = "RequestStore(settings.max_per_channel)" if limited else "RequestStore()"
            if router:
                lines += [f"    app.state.store = {ctor}", "    app.include_router(router)"]
            else:
                lines.append(f"    store = {ctor}")
        lines += [
            "",
            '    @app.get("/health")',
            "    def health() -> dict[str, str]:",
            '        return {"status": "ok", "version": __version__}',
        ]
        if capture and not router:
            lines.append("")
            for line in self._route_functions("app", "store", ""):
                lines.append(("    " + line) if line else "")
        lines += ["", "    return app", "", "", "app = create_app()"]
        # Nested helper functions are separated by a single blank line.
        return _collapse_nested_blank_lines("\n".join(lines)) + "\n"

    def api(self) -> str | None:
        if not self.has("router"):
            return None
        lines = [
            '"""HTTP routes for capturing and inspecting webhooks."""',
            "",
            "from typing import Annotated",
            "",
        ]
        lines += self._route_imports(["APIRouter", "Depends"])
        lines.append("")
        if self.has("curl"):
            lines.append("from hooklens.curl import to_curl")
        lines += [
            "from hooklens.models import CapturedRequest",
            "from hooklens.store import RequestStore",
            "",
            "router = APIRouter()",
            "",
            "",
            "def get_store(request: Request) -> RequestStore:",
            "    return request.app.state.store",
            "",
            "",
            "StoreDep = Annotated[RequestStore, Depends(get_store)]",
            "",
            "",
        ]
        lines += self._route_functions("router", "store", "store: StoreDep")
        return "\n".join(lines) + "\n"

    # -- tests --------------------------------------------------------------
    def test_health(self) -> str:
        return """from fastapi.testclient import TestClient

from hooklens import __version__
from hooklens.app import create_app


def test_health_reports_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
"""

    def conftest(self) -> str:
        return """import pytest
from fastapi.testclient import TestClient

from hooklens.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())
"""

    def test_capture(self) -> str:
        text = """from fastapi.testclient import TestClient


def test_capture_records_request_details(client: TestClient) -> None:
    response = client.post(
        "/hooks/github?delivery=1", json={"action": "opened"}, headers={"X-Event": "push"}
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["channel"] == "github"
    assert payload["method"] == "POST"
    assert payload["query"] == {"delivery": "1"}
    assert payload["headers"]["x-event"] == "push"
    assert '"action"' in payload["body"]


def test_list_returns_newest_first(client: TestClient) -> None:
    client.post("/hooks/stripe", content="first")
    client.put("/hooks/stripe", content="second")
    bodies = [item["body"] for item in client.get("/hooks/stripe").json()]
    assert bodies == ["second", "first"]


def test_channels_are_isolated(client: TestClient) -> None:
    client.post("/hooks/a", content="x")
    assert client.get("/hooks/b").json() == []
"""
        if self.has("inspect"):
            text += """

def test_inspect_single_request(client: TestClient) -> None:
    created = client.post("/hooks/ci", content="build").json()
    response = client.get(f"/requests/{created['id']}")
    assert response.status_code == 200
    assert response.json()["body"] == "build"


def test_inspect_unknown_request_returns_404(client: TestClient) -> None:
    assert client.get("/requests/doesnotexist").status_code == 404
"""
        if self.has("clear"):
            text += """

def test_clear_channel(client: TestClient) -> None:
    client.post("/hooks/tmp", content="1")
    client.post("/hooks/tmp", content="2")
    assert client.delete("/hooks/tmp").json() == {"deleted": 2}
    assert client.get("/hooks/tmp").json() == []
"""
        return text

    def test_settings(self) -> str:
        return """import pytest

from hooklens.settings import DEFAULT_MAX_PER_CHANNEL, Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOOKLENS_MAX_PER_CHANNEL", raising=False)
    assert Settings.from_env().max_per_channel == DEFAULT_MAX_PER_CHANNEL


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOKLENS_MAX_PER_CHANNEL", "5")
    assert Settings.from_env().max_per_channel == 5


def test_rejects_non_positive_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOKLENS_MAX_PER_CHANNEL", "0")
    with pytest.raises(ValueError):
        Settings.from_env()
"""

    def test_store(self) -> str:
        return """from hooklens.models import CapturedRequest
from hooklens.store import RequestStore


def _request(body: str) -> CapturedRequest:
    return CapturedRequest(channel="c", method="POST", path="/hooks/c", body=body)


def test_store_evicts_oldest_when_full() -> None:
    store = RequestStore(max_per_channel=2)
    for body in ("one", "two", "three"):
        store.add(_request(body))
    assert [r.body for r in store.recent("c")] == ["three", "two"]
"""

    def test_extras(self) -> str | None:
        parts = ["from fastapi.testclient import TestClient"]
        if self.has("curl"):
            parts = [
                "from fastapi.testclient import TestClient",
                "",
                "from hooklens.curl import to_curl",
                "from hooklens.models import CapturedRequest",
            ]
        body = []
        if self.has("filter"):
            body.append("""def test_filter_by_method(client: TestClient) -> None:
    client.post("/hooks/f", content="a")
    client.put("/hooks/f", content="b")
    items = client.get("/hooks/f", params={"method": "put"}).json()
    assert [item["body"] for item in items] == ["b"]


def test_filter_by_body_text(client: TestClient) -> None:
    client.post("/hooks/f", content="order.created")
    client.post("/hooks/f", content="order.paid")
    items = client.get("/hooks/f", params={"contains": "paid"}).json()
    assert [item["body"] for item in items] == ["order.paid"]""")
        if self.has("curl"):
            body.append("""def test_curl_endpoint_replays_request(client: TestClient) -> None:
    created = client.post(
        "/hooks/gh?x=1", content='{"a": 1}', headers={"X-Signature": "abc"}
    ).json()
    response = client.get(f"/requests/{created['id']}/curl")
    assert response.status_code == 200
    command = response.text
    assert command.startswith("curl -X POST 'http://testserver/hooks/gh?x=1'")
    assert "-H 'x-signature: abc'" in command
    assert "--data-raw '{\\"a\\": 1}'" in command


def test_to_curl_quotes_body() -> None:
    request = CapturedRequest(channel="c", method="PUT", path="/hooks/c", body="it's")
    assert to_curl(request, "http://example.test/") == (
        "curl -X PUT http://example.test/hooks/c --data-raw 'it'\\"'\\"'s'"
    )""")
        if not body:
            return None
        return "\n".join(parts) + "\n\n\n" + "\n\n\n".join(body) + "\n"

    # -- automation ---------------------------------------------------------
    def ci(self) -> str:
        lint = (
            """      - name: Lint
        run: ruff check .
"""
            if self.has("tooling")
            else ""
        )
        return f"""name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install
        run: pip install -e ".[dev]"
{lint}      - name: Test
        run: python -m pytest
"""

    def makefile(self) -> str:
        return (
            ".PHONY: install test lint run\n\n"
            'install:\n\tpip install -e ".[dev]"\n\n'
            "test:\n\tpython -m pytest\n\n"
            "lint:\n\truff check .\n\n"
            "run:\n\tpython -m hooklens\n"
        )


def _items(values: list[str]) -> str:
    return "\n".join(f"    {v}," for v in values)


def _collapse_nested_blank_lines(text: str) -> str:
    """Inside create_app, use single blank lines between nested definitions."""
    out: list[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if (
            line == ""
            and out
            and out[-1] == ""
            and i + 1 < len(lines)
            and lines[i + 1].startswith("    ")
        ):
            continue
        out.append(line)
    return "\n".join(out)
