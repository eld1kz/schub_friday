from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from habit_repository import iso, utcnow


WEEKLY_LIMIT_TEMPLATE = (
    "You have visited {target} {count} times this week. "
    "You asked me to let you know when this happens."
)
LOCATION_REMINDER_TEMPLATE = "You are near a {category}. Reminder: {reminder_text}"


def week_start_for(timezone_name: str, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(timezone_name or "Asia/Seoul")
    local_now = (now or utcnow()).astimezone(tz)
    local_start = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_start.astimezone(timezone.utc)


class WatcherService:
    def __init__(self, repo, nudge_service):
        self.repo = repo
        self.nudges = nudge_service

    def create_habit_watcher(
        self,
        user_id: str,
        rule_type: str,
        target_category: str | None = None,
        target_brand: str | None = None,
        threshold_count: int | None = None,
        window_days: int | None = None,
        reminder_text: str | None = None,
    ) -> dict:
        allowed = {
            "weekly_visit_limit", "rolling_window_limit", "near_category_reminder",
            "near_place_reminder", "inactivity_goal",
        }
        if rule_type not in allowed:
            raise ValueError("unsupported watcher rule type")
        if threshold_count is not None and threshold_count < 1:
            raise ValueError("threshold_count must be positive")
        row = {
            "user_id": user_id,
            "rule_type": rule_type,
            "target_category": target_category,
            "target_brand": target_brand,
            "target_place_id": None,
            "threshold_count": threshold_count,
            "window_days": window_days,
            "reminder_text": reminder_text,
            "is_active": True,
            "cooldown_hours": 24,
            "created_at": iso(utcnow()),
            "updated_at": iso(utcnow()),
        }
        return self.repo.create_watcher(row)

    def list_habit_watchers(self, user_id: str) -> list[dict]:
        return self.repo.list_watchers(user_id)

    def delete_habit_watcher(self, user_id: str, watcher_id: str) -> bool:
        return self.repo.delete_watcher(user_id, watcher_id)

    def evaluate_visit(self, user_id: str, visit: dict) -> list[str]:
        settings = self.repo.ensure_settings(user_id)
        sent: list[str] = []
        for watcher in self.repo.list_watchers(user_id):
            if watcher.get("target_category") and watcher["target_category"] != visit["normalized_category"]:
                continue
            if watcher.get("target_brand") and watcher["target_brand"] != visit.get("normalized_brand"):
                continue
            rule_type = watcher["rule_type"]
            if rule_type == "weekly_visit_limit":
                start = week_start_for(settings.get("timezone") or "Asia/Seoul")
                count = len(self.repo.visits_by_category_since(user_id, visit["normalized_category"], start))
                threshold = int(watcher.get("threshold_count") or 0)
                if count <= threshold:
                    continue
                period_key = start.date().isoformat()
                target = watcher.get("target_brand") or _target_label(visit["normalized_category"])
                msg = WEEKLY_LIMIT_TEMPLATE.format(target=target, count=count)
                key = f"active:{user_id}:{watcher['id']}:{visit['id']}:{period_key}"
                if self.nudges.send(user_id, msg, key, watcher["id"], None, visit["id"], watcher):
                    sent.append(msg)
            elif rule_type in ("near_category_reminder", "near_place_reminder"):
                msg = LOCATION_REMINDER_TEMPLATE.format(
                    category=_target_label(visit["normalized_category"]),
                    reminder_text=watcher.get("reminder_text") or "",
                )
                key = f"reminder:{user_id}:{watcher['id']}:{visit['id']}"
                if self.nudges.send(user_id, msg, key, watcher["id"], None, visit["id"], watcher):
                    sent.append(msg)
        return sent


def _target_label(category: str) -> str:
    return category.replace("_", " ")
