from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from habit_logging import metric
from habit_repository import iso, parse_dt, utcnow


class NudgeService:
    def __init__(self, repo, send_func):
        self.repo = repo
        self.send_func = send_func

    def _day_start(self, timezone_name: str) -> datetime:
        tz = ZoneInfo(timezone_name or "Asia/Seoul")
        local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return local.astimezone(timezone.utc)

    def can_send(self, user_id: str, deduplication_key: str, watcher: dict | None = None) -> bool:
        settings = self.repo.ensure_settings(user_id)
        if not settings.get("nudges_enabled", True):
            metric("blocked_nudge", reason="nudges_off")
            return False
        if self.repo.nudge_exists(deduplication_key):
            metric("blocked_duplicate_nudge")
            return False
        day_start = self._day_start(settings.get("timezone") or "Asia/Seoul")
        if self.repo.nudges_today(user_id, day_start) >= int(settings.get("daily_nudge_limit") or 3):
            metric("blocked_nudge", reason="daily_limit")
            return False
        if watcher:
            last = self.repo.last_nudge_for_watcher(user_id, watcher["id"])
            if last:
                cooldown = timedelta(hours=int(watcher.get("cooldown_hours") or 24))
                if utcnow() - parse_dt(last["sent_at"]) < cooldown:
                    metric("blocked_nudge", reason="cooldown", watcher_id=watcher["id"])
                    return False
        return True

    def send(
        self,
        user_id: str,
        message: str,
        deduplication_key: str,
        watcher_rule_id: str | None = None,
        habit_suggestion_id: str | None = None,
        place_visit_id: str | None = None,
        watcher: dict | None = None,
        buttons: list | None = None,
    ) -> bool:
        if not self.can_send(user_id, deduplication_key, watcher):
            return False
        self.send_func(user_id, message, buttons=buttons)
        self.repo.save_nudge({
            "user_id": user_id,
            "watcher_rule_id": watcher_rule_id,
            "habit_suggestion_id": habit_suggestion_id,
            "place_visit_id": place_visit_id,
            "message": message,
            "deduplication_key": deduplication_key,
            "sent_at": iso(utcnow()),
        })
        return True
