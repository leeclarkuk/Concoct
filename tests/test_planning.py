from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from concoct.models import CommitKind, Complexity
from concoct.orchestrator import assign_repositories
from concoct.planning.planner import Planner, PlanningError
from concoct.planning.prompts import PlanRequest, build_plan_prompt
from concoct.planning.schedule import MIN_GAP, commit_schedule, follow_up_timestamp
from concoct.rng import derive_rng
from tests.conftest import ScriptedProvider, plan_payload


def request(**overrides: object) -> PlanRequest:
    values: dict[str, object] = {
        "language": "python",
        "technologies": ["fastapi"],
        "category": "developer tool",
        "complexity": Complexity.MEDIUM,
        "commit_count": 8,
        "min_commits": 8,
        "max_commits": 13,
    }
    values.update(overrides)
    return PlanRequest(**values)  # type: ignore[arg-type]


def test_plan_prompt_contains_constraints() -> None:
    prompt = build_plan_prompt(request(license="MIT", existing_names=("taken",)))
    assert "exactly 8" in prompt
    assert "fastapi" in prompt
    assert "developer tool" in prompt
    assert "MIT" in prompt
    assert "taken" in prompt


def test_valid_plan_is_accepted_and_spec_uses_requested_stack() -> None:
    provider = ScriptedProvider([plan_payload(8)])
    plan = Planner(provider).plan(request())
    assert len(plan.commits) == 8
    assert plan.commits[0].kind == CommitKind.SCAFFOLD
    assert [c.index for c in plan.commits] == list(range(8))
    assert plan.spec.name == "tiny-tool"
    assert plan.spec.technologies == ["fastapi"]
    assert plan.spec.language == "python"
    assert plan.spec.topics == ["tools", "tiny-tool"]
    assert provider.requests[0].json_schema is not None
    assert provider.requests[0].task == "plan"


def test_wrong_commit_count_triggers_feedback_retry() -> None:
    provider = ScriptedProvider([plan_payload(3), plan_payload(9)])
    plan = Planner(provider).plan(request())
    assert len(plan.commits) == 9
    assert "exactly 8" in provider.requests[1].prompt
    assert "has 3 commits" in provider.requests[1].prompt


def test_unparseable_reply_is_retried() -> None:
    provider = ScriptedProvider(["this is not json", plan_payload(8)])
    plan = Planner(provider).plan(request())
    assert len(plan.commits) == 8
    assert "not a valid plan" in provider.requests[1].prompt


def test_too_long_plan_is_trimmed_after_retries() -> None:
    provider = ScriptedProvider([plan_payload(20), plan_payload(20)])
    plan = Planner(provider).plan(request())
    assert len(plan.commits) == 13
    assert plan.commits[0].kind == CommitKind.SCAFFOLD


def test_too_short_plan_fails_after_retries() -> None:
    provider = ScriptedProvider([plan_payload(2), plan_payload(2)])
    with pytest.raises(PlanningError, match="fewer than the minimum"):
        Planner(provider).plan(request())


def test_first_commit_is_forced_to_scaffold() -> None:
    payload = plan_payload(8)
    payload["commits"][0]["kind"] = "feature"
    provider = ScriptedProvider([payload, payload])
    plan = Planner(provider).plan(request())
    assert plan.commits[0].kind == CommitKind.SCAFFOLD


def test_name_collision_gets_suffix() -> None:
    payload = plan_payload(8, name="Tiny Tool")
    provider = ScriptedProvider([payload, payload])
    plan = Planner(provider).plan(request(existing_names=("tiny-tool", "tiny-tool-2")))
    assert plan.spec.name == "tiny-tool-3"


def test_all_attempts_unparseable_raises() -> None:
    provider = ScriptedProvider(["nope", "{}"])
    with pytest.raises(PlanningError):
        Planner(provider).plan(request())


# ------------------------------------------------------------------ schedule
END = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def test_schedule_is_deterministic_for_a_seed() -> None:
    a = commit_schedule(derive_rng(7, "x"), 12, end=END, history_days=120, zone=ZoneInfo("UTC"))
    b = commit_schedule(derive_rng(7, "x"), 12, end=END, history_days=120, zone=ZoneInfo("UTC"))
    c = commit_schedule(derive_rng(8, "x"), 12, end=END, history_days=120, zone=ZoneInfo("UTC"))
    assert a == b
    assert a != c


@pytest.mark.parametrize("seed", range(25))
def test_schedule_is_strictly_increasing_within_window(seed: int) -> None:
    zone = ZoneInfo("Europe/London")
    stamps = commit_schedule(derive_rng(seed), 30, end=END, history_days=90, zone=zone)
    assert len(stamps) == 30
    assert all(s.tzinfo is not None for s in stamps)
    assert all(b - a >= MIN_GAP for a, b in itertools.pairwise(stamps))
    assert stamps[-1] <= END
    assert stamps[0] >= END - timedelta(days=92)


def test_schedule_prefers_weekdays_and_daytime() -> None:
    stamps = []
    for seed in range(40):
        stamps += commit_schedule(
            derive_rng(seed), 20, end=END, history_days=365, zone=ZoneInfo("UTC")
        )
    weekday_share = sum(s.weekday() < 5 for s in stamps) / len(stamps)
    daytime_share = sum(8 <= s.hour < 19 for s in stamps) / len(stamps)
    assert weekday_share > 0.8
    assert daytime_share > 0.65


def test_follow_up_is_after_previous_and_not_after_end() -> None:
    rng = derive_rng(1)
    previous = END - timedelta(minutes=30)
    for _ in range(50):
        nxt = follow_up_timestamp(rng, previous, END)
        assert nxt > previous
        assert nxt <= END
        previous = min(nxt, END - timedelta(minutes=5))


# --------------------------------------------------------------- assignments
def test_assignments_are_deterministic_and_bounded(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        repos=6, languages=["python", "go"], min_commits=8, max_commits=15, seed=99
    )
    first = assign_repositories(settings)
    second = assign_repositories(settings)
    assert [(a.language, a.category, a.commit_count) for a in first] == [
        (a.language, a.category, a.commit_count) for a in second
    ]
    assert [a.language for a in first] == ["python", "go"] * 3
    # two commits are reserved for bounded repairs
    assert all(8 <= a.commit_count <= 13 for a in first)


def test_assignment_random_complexity(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(repos=30, complexity="random", seed=5)
    kinds = {a.complexity for a in assign_repositories(settings)}
    assert kinds == {Complexity.LOW, Complexity.MEDIUM, Complexity.HIGH}
