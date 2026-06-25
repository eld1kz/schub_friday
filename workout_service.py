from __future__ import annotations

# Workout Builder — turns a readiness status (Module 1 dict) into a half-marathon
# training session. `build` is the core; WorkoutService is a thin convenience wrapper.

# Normal (READY) sessions, keyed by type. distance in km, duration in minutes.
_SESSIONS = {
    "easy_run": {
        "intensity": "easy", "distance_km": 8.0, "duration_min": 45,
        "summary": "Easy run — 8 km conversational pace.",
        "detail": "Keep it aerobic; you should be able to hold a conversation throughout.",
    },
    "intervals": {
        "intensity": "hard", "distance_km": 9.0, "duration_min": 50,
        "summary": "Intervals — 6×800m @ 5K pace, 9 km total.",
        "detail": "2 km warmup, 6×800m hard with 400m jog recovery, 1.5 km cooldown.",
    },
    "tempo": {
        "intensity": "hard", "distance_km": 10.0, "duration_min": 55,
        "summary": "Tempo — 5 km at half-marathon pace, 10 km total.",
        "detail": "2.5 km warmup, 5 km comfortably hard, 2.5 km cooldown.",
    },
    "long_run": {
        "intensity": "moderate", "distance_km": 18.0, "duration_min": 110,
        "summary": "Long run — 18 km steady, easy effort.",
        "detail": "Keep effort easy and fuel/hydrate; this is the key half-marathon session.",
    },
    "rest": {
        "intensity": "rest", "distance_km": None, "duration_min": None,
        "summary": "Rest day — recovery walk if you feel like moving.",
        "detail": "Optional 20–30 min easy walk or very-easy jog. Prioritise sleep and recovery.",
    },
}

_VALID_STATUS = {"READY", "MODERATE", "REST"}


def _pick_ready_type(weekday: int) -> str:
    # 0..6 Mon..Sun. Weekend → long run; Tue intervals, Thu tempo; otherwise easy.
    if weekday in (5, 6):
        return "long_run"
    if weekday == 1:
        return "intervals"
    if weekday == 3:
        return "tempo"
    return "easy_run"


def _session(type_: str, **overrides) -> dict:
    out = dict(_SESSIONS[type_])
    out["type"] = type_
    out.update(overrides)
    return out


def build(status: dict, weekday: int | None = None) -> dict:
    """Build a half-marathon session from a Module 1 readiness dict.

    `weekday` (0..6 Mon..Sun) only selects the quality session on READY days;
    defaults to today's weekday. Defensive: malformed input never crashes —
    it falls back to a conservative easy run and explains why in `detail`.
    """
    if weekday is None:
        from datetime import date
        weekday = date.today().weekday()

    state = status.get("status") if isinstance(status, dict) else None

    if state not in _VALID_STATUS:
        return _session(
            "easy_run",
            detail="Readiness unknown or malformed — defaulting to a conservative easy "
                   "run. " + _SESSIONS["easy_run"]["detail"],
        )

    if state == "REST":
        return _session("rest")

    if state == "MODERATE":
        # ~30% below a normal easy day; keep it easy.
        base = _SESSIONS["easy_run"]
        dist = round(base["distance_km"] * 0.7, 1)
        dur = int(base["duration_min"] * 0.7)
        return _session(
            "easy_run",
            distance_km=dist, duration_min=dur,
            summary=f"Easy run — {dist:g} km, scaled back.",
            detail="Readiness is moderate, so distance is cut ~30%. Keep it gentle and "
                   "reassess tomorrow.",
        )

    # READY
    return _session(_pick_ready_type(weekday))


class WorkoutService:
    """Thin wrapper; `build` holds the logic."""

    def build(self, status: dict, weekday: int | None = None) -> dict:
        return build(status, weekday)
