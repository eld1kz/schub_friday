from __future__ import annotations

from readiness_service import ReadinessService, assess


def _hist(n, hrv=60.0, rhr=50.0, rr=14.0, sleep=8.0):
    return [
        {"metric_date": f"2026-06-{10 + i:02d}", "hrv": hrv,
         "resting_hr": rhr, "respiratory_rate": rr, "sleep_hours": sleep}
        for i in range(n)
    ]


class InMemoryHealthRepository:
    """Test double mirroring SupabaseHealthRepository.recent."""

    def __init__(self, rows=None):
        self.rows = rows or []  # newest first

    def recent(self, user_id: str, days: int = 30) -> list[dict]:
        return self.rows[:days]


def test_normal_ready_day():
    today = {"metric_date": "2026-06-26", "hrv": 62.0,
             "resting_hr": 49.0, "respiratory_rate": 14.0, "sleep_hours": 8.0}
    out = assess(today, _hist(20))
    assert out["status"] == "READY"
    assert out["date"] == "2026-06-26"
    assert out["baseline_days"] == 20
    assert out["metrics"]["hrv"]["baseline"] == 60.0


def test_suppressed_rest_day():
    # Low HRV (>10% below) + high RHR (>3 over) = two negatives → REST.
    today = {"metric_date": "2026-06-26", "hrv": 50.0,
             "resting_hr": 56.0, "respiratory_rate": 14.0, "sleep_hours": 8.0}
    out = assess(today, _hist(20))
    assert out["status"] == "REST"
    assert any("HRV" in r and "below" in r for r in out["reasons"])
    assert any("resting HR" in r for r in out["reasons"])


def test_severe_rhr_forces_rest_alone():
    today = {"metric_date": "2026-06-26", "hrv": 60.0,
             "resting_hr": 58.0, "respiratory_rate": 14.0, "sleep_hours": 8.0}
    out = assess(today, _hist(20))
    assert out["status"] == "REST"  # single signal but >baseline+7


def test_empty_history_falls_back_to_ready():
    today = {"metric_date": "2026-06-26", "hrv": 60.0, "sleep_hours": 8.0}
    out = assess(today, [])
    assert out["status"] == "READY"
    assert out["baseline_days"] == 0
    assert any("limited history" in r for r in out["reasons"])
    assert out["metrics"]["hrv"]["baseline"] is None


def test_missing_and_none_fields_do_not_crash():
    today = {"metric_date": "2026-06-26", "hrv": None, "resting_hr": 50.0}
    out = assess(today, _hist(10))
    assert out["status"] == "READY"
    assert "hrv" not in out["metrics"]  # None today value is skipped
    assert "resting_hr" in out["metrics"]


def test_string_typed_numbers_are_coerced():
    today = {"metric_date": "2026-06-26", "hrv": "50",
             "resting_hr": "56", "respiratory_rate": "14", "sleep_hours": "8"}
    hist = [{"metric_date": "x", "hrv": "60", "resting_hr": "50",
             "respiratory_rate": "14", "sleep_hours": "8"} for _ in range(10)]
    out = assess(today, hist)
    assert out["status"] == "REST"  # 50 vs 60 HRV, 56 vs 50 RHR
    assert out["metrics"]["hrv"]["today"] == 50.0


def test_short_sleep_is_single_negative_moderate():
    today = {"metric_date": "2026-06-26", "hrv": 60.0,
             "resting_hr": 50.0, "respiratory_rate": 14.0, "sleep_hours": 6.0}
    out = assess(today, _hist(20))
    assert out["status"] == "MODERATE"
    assert any("short sleep" in r for r in out["reasons"])


def test_service_check_via_in_memory_repo():
    rows = [{"metric_date": "2026-06-26", "hrv": 62.0, "resting_hr": 49.0,
             "respiratory_rate": 14.0, "sleep_hours": 8.0}] + _hist(15)
    svc = ReadinessService(InMemoryHealthRepository(rows))
    out = svc.check("user-1")
    assert out["status"] == "READY"
    assert out["baseline_days"] == 15


def test_service_check_no_data():
    svc = ReadinessService(InMemoryHealthRepository([]))
    out = svc.check("user-1")
    assert out["status"] == "READY"
    assert out["baseline_days"] == 0
    assert out["date"] is None
