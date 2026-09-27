from __future__ import annotations

import pytest

from concoct.generation.commits import CommitGenerator, GenerationError, screen_changes
from concoct.generation.parsing import ResponseParseError, extract_json
from concoct.generation.workspace import render_workspace
from concoct.models import DevelopmentPlan, FileChange, PlannedCommit, ProjectSpec
from concoct.providers.base import ProviderTruncatedError, TokenUsage
from tests.conftest import ScriptedProvider


def make_plan() -> DevelopmentPlan:
    spec = ProjectSpec(name="demo", description="Demo", category="library", language="python")
    commits = [
        PlannedCommit(kind="scaffold", message="Initial commit", intent="scaffold"),
        PlannedCommit(kind="feat", message="feat: add greet", intent="add greet() to demo.core"),
    ]
    return DevelopmentPlan(spec=spec, commits=commits)


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Here you go:\n{"a": 1}\nThanks!',
        'noise {not json} then {"a": 1}',
    ],
)
def test_extract_json_variants(text: str) -> None:
    assert extract_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "no braces", "[1, 2]"])
def test_extract_json_failures(text: str) -> None:
    with pytest.raises(ResponseParseError):
        extract_json(text)


# ------------------------------------------------------------------ screening
def test_screen_rejects_secrets_syntax_and_duplicates() -> None:
    problems = screen_changes(
        [
            FileChange(path="a.py", content="def broken(:\n"),
            FileChange(path="b.py", content='KEY = "sk-ant-' + "x" * 30 + '"\n'),
            FileChange(path="c.json", content="{bad json"),
            FileChange(path="c.json", content="{}"),
        ],
        {},
    )
    joined = "\n".join(problems)
    assert "syntax error: a.py" in joined
    assert "looks like a secret" in joined
    assert "c.json" in joined and "more than once" in joined


def test_screen_rejects_no_op_changes() -> None:
    current = {"a.py": "x = 1\n"}
    assert screen_changes([FileChange(path="a.py", content="x = 1")], current) == [
        "the changes do not modify the repository"
    ]
    assert screen_changes([FileChange(path="missing.py", action="delete")], current)


def test_screen_accepts_valid_changes() -> None:
    changes = [
        FileChange(path="a.py", content="x = 2\n"),
        FileChange(path="pyproject.toml", content='[project]\nname = "x"\n'),
        FileChange(path="ci.yml", content="on: push\n"),
    ]
    assert screen_changes(changes, {"a.py": "x = 1\n"}) == []


# ------------------------------------------------------------------ workspace
def test_render_workspace_includes_contents_and_prioritises_focus() -> None:
    files = {"big.py": "x" * 500, "small.py": "y = 1", "focus.py": "z" * 300}
    rendered = render_workspace(files, focus=["focus.py"], max_chars=400)
    assert "focus.py" in rendered.included
    assert "big.py" in rendered.omitted
    assert "=== FILE: focus.py ===" in rendered.text
    assert "- big.py" in rendered.text  # still listed by path
    assert "Contents omitted" in rendered.text


def test_render_empty_workspace() -> None:
    assert "empty" in render_workspace({}).text


# ------------------------------------------------------------------ generator
def test_implement_sends_current_state_and_returns_plan_message() -> None:
    plan = make_plan()
    provider = ScriptedProvider(
        [
            {
                "summary": "add greet",
                "changes": [
                    {
                        "path": "src/demo/core.py",
                        "action": "write",
                        "content": "def greet():\n    ...\n",
                    }
                ],
            }
        ]
    )
    files = {"src/demo/__init__.py": "VERSION = '1'\n"}
    draft = CommitGenerator(provider).implement(plan, plan.commits[1], files)
    assert draft.message == "feat: add greet"
    assert [c.path for c in draft.changes] == ["src/demo/core.py"]
    request = provider.requests[0]
    assert request.task == "commit"
    assert "VERSION = '1'" in request.prompt  # the model sees existing contents
    assert "[done]  1." in request.prompt and "[NOW ]  2." in request.prompt
    assert request.context["step_index"] == 1


def test_implement_retries_with_feedback_then_succeeds() -> None:
    plan = make_plan()
    good = {"summary": "", "changes": [{"path": "a.py", "action": "write", "content": "a = 1"}]}
    bad = {"summary": "", "changes": [{"path": "a.py", "action": "write", "content": "a = ("}]}
    provider = ScriptedProvider([bad, good])
    draft = CommitGenerator(provider).implement(plan, plan.commits[0], {})
    assert draft.attempts == 2
    assert "syntax error" in provider.requests[1].prompt


def test_implement_handles_truncation_then_gives_up() -> None:
    plan = make_plan()
    provider = ScriptedProvider(
        [ProviderTruncatedError("cut", usage=TokenUsage(output_tokens=5)), "not json"]
    )
    with pytest.raises(GenerationError, match="no acceptable changes after 2 attempts"):
        CommitGenerator(provider).implement(plan, plan.commits[0], {})
    assert "cut off" in provider.requests[1].prompt


def test_repair_uses_diagnostics_and_model_message() -> None:
    plan = make_plan()
    provider = ScriptedProvider(
        [
            {
                "message": "fix: import greet",
                "changes": [{"path": "a.py", "action": "write", "content": "from x import y\n"}],
            }
        ]
    )
    draft = CommitGenerator(provider).repair(plan, {"a.py": "broken = 1\n"}, "tests: 1 failed")
    assert draft.message == "fix: import greet"
    assert "tests: 1 failed" in provider.requests[0].prompt
    assert provider.requests[0].task == "repair"
