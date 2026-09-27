"""Deterministic, human-looking commit timestamps.

Real histories are bursty: several commits in an evening, then nothing for a
week. They cluster in working hours on weekdays, with some evening and weekend
activity. Everything here is driven by a caller-supplied ``random.Random`` so a
given seed always yields the same schedule.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

MIN_GAP = timedelta(minutes=4)

# (start hour, end hour, weight): mostly working hours, some evenings.
_HOUR_BANDS = ((9, 12, 0.38), (13, 18, 0.42), (19, 23, 0.17), (7, 9, 0.03))


def _pick_time_of_day(rng: random.Random) -> time:
    bands = [(start, end) for start, end, _ in _HOUR_BANDS]
    weights = [w for _, _, w in _HOUR_BANDS]
    start, end = rng.choices(bands, weights=weights)[0]
    return time(hour=rng.randrange(start, end), minute=rng.randrange(60), second=rng.randrange(60))


def repository_window(
    rng: random.Random, end: datetime, history_days: int
) -> tuple[datetime, datetime]:
    """Pick the active period of one repository inside the history window."""
    history = timedelta(days=history_days)
    window_start = end - history
    # Start somewhere in the first 60% of the window; stay active for a large
    # share of the remaining time, ending no later than ``end``.
    start = window_start + history * rng.uniform(0.0, 0.6)
    remaining = end - start
    stop = start + remaining * rng.uniform(0.55, 1.0)
    return start, stop


def commit_schedule(
    rng: random.Random,
    count: int,
    *,
    end: datetime,
    history_days: int,
    zone: ZoneInfo,
) -> list[datetime]:
    """Return ``count`` strictly increasing, timezone-aware commit timestamps."""
    if count <= 0:
        return []
    start, stop = repository_window(rng, end, history_days)
    span = (stop - start).total_seconds()

    # Bursty gaps: most are short, a few are long (exponential with occasional
    # "session" clusters).
    gaps = []
    for _ in range(count - 1):
        if rng.random() < 0.35:
            gaps.append(rng.uniform(0.02, 0.2))  # same working session / day
        else:
            gaps.append(rng.expovariate(1.0))
    total = sum(gaps) or 1.0
    offsets = [0.0]
    for gap in gaps:
        offsets.append(offsets[-1] + gap / total)

    stamps: list[datetime] = []
    for offset in offsets:
        moment = (start + timedelta(seconds=span * offset)).astimezone(zone)
        moment = _humanise(rng, moment)
        stamps.append(moment)

    stamps.sort()
    return _make_strictly_increasing(rng, stamps, end.astimezone(zone))


def _humanise(rng: random.Random, moment: datetime) -> datetime:
    day = moment.date()
    # Weekends are quieter: usually shift to the adjacent weekday.
    if day.weekday() >= 5 and rng.random() < 0.7:
        shift = -1 if day.weekday() == 5 else 1
        day = day + timedelta(days=shift)
    tod = _pick_time_of_day(rng)
    return datetime.combine(day, tod, tzinfo=moment.tzinfo)


def _make_strictly_increasing(
    rng: random.Random, stamps: list[datetime], end: datetime
) -> list[datetime]:
    result: list[datetime] = []
    for stamp in stamps:
        if result and stamp <= result[-1] + MIN_GAP:
            stamp = result[-1] + timedelta(minutes=rng.randint(6, 55))
        result.append(stamp)
    # Clamp to ``end`` while keeping order: compress the overflow backwards.
    if result and result[-1] > end:
        overflow = result[-1] - end
        result = [s - overflow for s in result]
    return result


def follow_up_timestamp(rng: random.Random, previous: datetime, end: datetime) -> datetime:
    """A timestamp shortly after ``previous`` (used for repair commits)."""
    candidate = previous + timedelta(minutes=rng.randint(12, 150))
    ceiling = end.astimezone(previous.tzinfo) if previous.tzinfo else end
    if candidate > ceiling:
        candidate = previous + (max(ceiling - previous, MIN_GAP)) / 2
    if candidate <= previous:
        candidate = previous + MIN_GAP
    return candidate
