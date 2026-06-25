from __future__ import annotations

from datetime import date, datetime, timezone

# Canonical numeric columns the Shortcut can send. Anything else is kept in `raw`.
FLOAT_FIELDS = [
    "hrv", "resting_hr", "respiratory_rate", "sleep_hours",
    "active_energy", "exercise_minutes", "workout_distance_km",
    "vo2max", "body_weight_kg", "blood_oxygen",
]
INT_FIELDS = ["steps"]

# Friendly aliases so the Shortcut payload can use natural names.
ALIASES = {
    "rhr": "resting_hr",
    "heart_rate_variability": "hrv",
    "resp_rate": "respiratory_rate",
    "sleep": "sleep_hours",
    "distance_km": "workout_distance_km",
    "weight": "body_weight_kg",
    "spo2": "blood_oxygen",
    "vo2_max": "vo2max",
}


def _num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(v, unit=""):
    return f"{v:.1f}{unit}" if isinstance(v, (int, float)) else "—"


class HealthService:
    def __init__(self, repo):
        self.repo = repo

    # ---- ingest (called by the /health/ingest endpoint) -------------------
    def ingest(self, payload: dict, default_user_id: str) -> dict:
        user_id = str(payload.get("user_id") or default_user_id or "").strip()
        if not user_id:
            raise ValueError("no user_id (set TELEGRAM_CHAT_ID or send user_id)")

        metric_date = str(payload.get("date") or "").strip() \
            or datetime.now(timezone.utc).date().isoformat()

        row = {"user_id": user_id, "metric_date": metric_date, "raw": payload}
        for key, value in payload.items():
            field = ALIASES.get(key, key)
            if field in FLOAT_FIELDS:
                n = _num(value)
                if n is not None:
                    row[field] = n
            elif field in INT_FIELDS:
                n = _num(value)
                if n is not None:
                    row[field] = int(n)

        return self.repo.upsert_daily(row)

    # ---- readiness summary (called by the get_health tool) ----------------
    def status_summary(self, user_id: str) -> str:
        rows = self.repo.recent(user_id, days=30)
        if not rows:
            return ("No health data yet. Set up the Apple Shortcut to push daily "
                    "metrics to the bot, then ask again.")

        today = rows[0]                 # newest first
        baseline = rows[1:29]           # prior ~4 weeks

        def base(field):
            return _avg([r.get(field) for r in baseline])

        signals = []
        hrv, hrv_b = today.get("hrv"), base("hrv")
        if hrv is not None and hrv_b:
            if hrv < hrv_b * 0.90:
                signals.append("HRV suppressed vs baseline → lower readiness")
            elif hrv > hrv_b * 1.05:
                signals.append("HRV above baseline → well recovered")

        rhr, rhr_b = today.get("resting_hr"), base("resting_hr")
        if rhr is not None and rhr_b and rhr > rhr_b + 3:
            signals.append("Resting HR elevated → incomplete recovery or illness")

        rr, rr_b = today.get("respiratory_rate"), base("respiratory_rate")
        if rr is not None and rr_b and rr > rr_b + 1:
            signals.append("Respiratory rate elevated → strain or oncoming illness")

        sleep = today.get("sleep_hours")
        if sleep is not None and sleep < 7:
            signals.append(f"Short sleep ({_fmt(sleep, 'h')})")

        negatives = len(signals) - sum(1 for s in signals if "well recovered" in s)
        readiness = ("Recover — keep it easy or rest" if negatives >= 2
                     else "Caution — moderate session" if negatives == 1
                     else "Ready — green light for intensity")

        lines = [
            f"Readiness: {readiness}",
            f"  Today ({today.get('metric_date')}): "
            f"HRV {_fmt(hrv, 'ms')} (base {_fmt(hrv_b, 'ms')}), "
            f"RHR {_fmt(rhr, 'bpm')} (base {_fmt(rhr_b, 'bpm')}), "
            f"resp {_fmt(rr)} (base {_fmt(rr_b)}), sleep {_fmt(sleep, 'h')}",
        ]
        if signals:
            lines.append("  Signals: " + "; ".join(signals))

        lines.append("\nRecent load (newest first):")
        for r in rows[:7]:
            lines.append(
                f"  {r.get('metric_date')}: "
                f"steps {r.get('steps') or '—'}, "
                f"active {_fmt(r.get('active_energy'), 'kcal')}, "
                f"exercise {_fmt(r.get('exercise_minutes'), 'min')}, "
                f"run {_fmt(r.get('workout_distance_km'), 'km')}"
            )

        vo2, weight = today.get("vo2max"), today.get("body_weight_kg")
        if vo2 is not None or weight is not None:
            lines.append(f"\nFitness: VO2max {_fmt(vo2)}, weight {_fmt(weight, 'kg')}")

        return "\n".join(lines)
