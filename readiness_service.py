from __future__ import annotations

from health_service import _avg, _num

# Fields we surface in `metrics` and reason over. Sleep is judged on an absolute
# threshold; the rest are compared to a baseline computed from history.
_METRIC_FIELDS = ["hrv", "resting_hr", "respiratory_rate", "sleep_hours"]


def _round(v):
    return round(v, 1) if isinstance(v, (int, float)) else None


def assess(today: dict, history: list[dict]) -> dict:
    today = today or {}
    history = history or []

    def baseline(field):
        return _avg([_num(r.get(field)) for r in (history or [])])

    bases = {f: baseline(f) for f in _METRIC_FIELDS}
    todays = {f: _num(today.get(f)) for f in _METRIC_FIELDS}

    # baseline_days = how many history rows carried at least one usable metric.
    baseline_days = sum(
        1 for r in history if any(_num(r.get(f)) is not None for f in _METRIC_FIELDS)
    )

    metrics = {}
    for f in _METRIC_FIELDS:
        if todays[f] is None:
            continue
        b = bases[f]
        delta = ((todays[f] - b) / b * 100) if b else None
        metrics[f] = {
            "today": _round(todays[f]),
            "baseline": _round(b),
            "delta_pct": _round(delta),
        }

    reasons = []
    negatives = 0
    severe_rhr = False

    hrv, hrv_b = todays["hrv"], bases["hrv"]
    if hrv is not None and hrv_b:
        if hrv < hrv_b * 0.90:
            negatives += 1
            reasons.append(f"HRV {abs(round((hrv - hrv_b) / hrv_b * 100))}% below baseline")
        elif hrv > hrv_b * 1.05:
            reasons.append(f"HRV {round((hrv - hrv_b) / hrv_b * 100)}% above baseline")

    rhr, rhr_b = todays["resting_hr"], bases["resting_hr"]
    if rhr is not None and rhr_b:
        if rhr > rhr_b + 3:
            negatives += 1
            reasons.append(f"resting HR {round(rhr - rhr_b)} bpm above baseline")
        if rhr > rhr_b + 7:
            severe_rhr = True

    rr, rr_b = todays["respiratory_rate"], bases["respiratory_rate"]
    if rr is not None and rr_b and rr > rr_b + 1:
        negatives += 1
        reasons.append(f"respiratory rate {round(rr - rr_b, 1)} br/min above baseline")

    sleep = todays["sleep_hours"]
    if sleep is not None and sleep < 7:
        negatives += 1
        reasons.append(f"short sleep ({_round(sleep)}h)")

    if baseline_days == 0:
        reasons.append("limited history — baseline not established")

    if negatives >= 2 or severe_rhr:
        status = "REST"
    elif negatives == 1:
        status = "MODERATE"
    else:
        status = "READY"

    summary = _summary(status, reasons)

    return {
        "date": today.get("metric_date"),
        "status": status,
        "reasons": reasons,
        "metrics": metrics,
        "baseline_days": baseline_days,
        "summary": summary,
    }


def _summary(status, reasons):
    head = {
        "READY": "Ready — green light for intensity",
        "MODERATE": "Caution — keep it moderate",
        "REST": "Recover — rest or very easy day",
    }[status]
    if reasons:
        return f"{head}: " + "; ".join(reasons) + "."
    return head + "."


class ReadinessService:
    def __init__(self, repo):
        self.repo = repo

    def check(self, user_id: str) -> dict:
        rows = self.repo.recent(user_id, days=30)
        if not rows:
            return assess({}, [])
        return assess(rows[0], rows[1:])
