"""Spaced-repetition scheduling — the reference implementation.

The web app carries the same algorithm in JavaScript (``app/js/srs.js``); both
are checked against ``tests/srs_vectors.json`` so a review made on the phone and
a review replayed here always land on the same next-due date.

State of one card (times are epoch milliseconds, intervals are days)::

    {"ease": 2.5, "interval": 0, "reps": 0, "lapses": 0, "due": 0, "last": 0}

Ratings: 0 again, 1 hard, 2 good, 3 easy.  It is the SM-2 variant already used
by the owner's rehearsal studio:

* again  -> streak resets, ease -0.20 (floor 1.3), back in 10 minutes;
* first pass 1 day, second pass 3 days, then interval x ease;
* hard   -> interval x0.7 (at least half a day), ease -0.15;
* easy   -> interval x1.3, ease +0.10.
"""
from __future__ import annotations

import math

DAY_MS = 86_400_000
AGAIN_DELAY_MS = 10 * 60 * 1000
MIN_EASE = 1.3
MATURE_DAYS = 21.0

NEW_STATE = {"ease": 2.5, "interval": 0.0, "reps": 0, "lapses": 0, "due": 0, "last": 0}


def round2(x: float) -> float:
    """Round half up to 2 decimals — identical in Python and JavaScript."""
    return math.floor(x * 100 + 0.5) / 100


def schedule(state: dict | None, rating: int, now_ms: int) -> dict:
    """Apply one review and return the new state (the input is not modified)."""
    if rating not in (0, 1, 2, 3):
        raise ValueError("rating must be 0, 1, 2 or 3")
    s = {**NEW_STATE, **(state or {})}
    ease, interval, reps, lapses = float(s["ease"]), float(s["interval"]), int(s["reps"]), int(s["lapses"])

    if rating == 0:
        return {
            "ease": round2(max(MIN_EASE, ease - 0.2)),
            "interval": 0.0,
            "reps": 0,
            "lapses": lapses + (1 if reps > 0 else 0),
            "due": now_ms + AGAIN_DELAY_MS,
            "last": now_ms,
        }

    reps += 1
    if reps == 1:
        interval = 1.0
    elif reps == 2:
        interval = 3.0
    else:
        interval = round2(interval * ease)

    if rating == 1:
        interval = round2(max(0.5, interval * 0.7))
        ease = round2(max(MIN_EASE, ease - 0.15))
    elif rating == 3:
        interval = round2(interval * 1.3)
        ease = round2(ease + 0.1)

    return {
        "ease": ease,
        "interval": interval,
        "reps": reps,
        "lapses": lapses,
        "due": now_ms + int(math.floor(interval * DAY_MS + 0.5)),
        "last": now_ms,
    }


def stage(state: dict | None) -> str:
    """new -> learning -> young -> mature."""
    if not state or not state.get("last"):
        return "new"
    if state.get("reps", 0) == 0:
        return "learning"
    return "mature" if state.get("interval", 0) >= MATURE_DAYS else "young"


def strength(state: dict | None) -> float:
    """0..1 — how settled one card is; the mean over a project is its mastery."""
    if not state or not state.get("last") or state.get("reps", 0) == 0:
        return 0.0
    return min(1.0, float(state.get("interval", 0)) / MATURE_DAYS)


def mastery(states: list[dict | None]) -> float:
    return round(sum(strength(s) for s in states) / len(states), 4) if states else 0.0
