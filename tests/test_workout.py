from __future__ import annotations

from workout_service import build


def _status(state, **extra):
    return {"status": state, "date": "2026-06-26", "reasons": [], "metrics": {},
            "baseline_days": 14, "summary": f"{state} day", **extra}


def test_ready_weekend_is_long_run():
    out = build(_status("READY"), weekday=5)  # Saturday
    assert out["type"] == "long_run"
    assert out["intensity"] == "moderate"
    assert out["distance_km"] >= 16  # half-marathon-appropriate long run
    assert isinstance(out["duration_min"], int)


def test_ready_tuesday_is_intervals():
    out = build(_status("READY"), weekday=1)  # Tuesday
    assert out["type"] == "intervals"
    assert out["intensity"] == "hard"
    assert out["distance_km"] is not None


def test_ready_monday_is_easy_run():
    out = build(_status("READY"), weekday=0)  # Monday — non-quality day
    assert out["type"] == "easy_run"
    assert out["intensity"] == "easy"


def test_moderate_scales_down_easy_run():
    easy = build(_status("READY"), weekday=0)["distance_km"]
    out = build(_status("MODERATE"), weekday=0)
    assert out["type"] == "easy_run"
    assert out["intensity"] == "easy"
    assert out["distance_km"] < easy  # ~30% below a normal easy day
    assert "moderate" in out["detail"].lower()


def test_rest_status_yields_rest():
    out = build(_status("REST"), weekday=1)  # interval day, but suppressed
    assert out["type"] == "rest"
    assert out["intensity"] == "rest"
    assert out["distance_km"] is None and out["duration_min"] is None


def test_malformed_status_falls_back_without_crashing():
    for bad in ({}, {"status": "WAT"}, {"foo": "bar"}, None):
        out = build(bad, weekday=1)
        assert out["type"] == "easy_run"
        assert out["intensity"] == "easy"
        assert set(out) == {"type", "intensity", "distance_km", "duration_min",
                            "summary", "detail"}


def test_return_shape_is_exact():
    out = build(_status("READY"), weekday=5)
    assert set(out) == {"type", "intensity", "distance_km", "duration_min",
                        "summary", "detail"}
