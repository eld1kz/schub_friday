from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class SupabaseHabitRepository:
    def __init__(self, client):
        self.client = client

    def ensure_settings(self, user_id: str) -> dict:
        rows = self.client.table("user_location_settings").select("*").eq("user_id", user_id).execute().data or []
        if rows:
            return rows[0]
        row = {"user_id": user_id}
        return (self.client.table("user_location_settings").insert(row).execute().data or [row])[0]

    def update_settings(self, user_id: str, **fields) -> dict:
        self.ensure_settings(user_id)
        fields["updated_at"] = iso(utcnow())
        rows = self.client.table("user_location_settings").update(fields).eq("user_id", user_id).execute().data or []
        return rows[0] if rows else self.ensure_settings(user_id)

    def save_location_update(self, row: dict) -> dict:
        return (self.client.table("location_updates").insert(row).execute().data or [row])[0]

    def get_last_location_update(self, user_id: str) -> dict | None:
        rows = (self.client.table("location_updates").select("*").eq("user_id", user_id)
                .order("received_at", desc=True).limit(1).execute().data or [])
        return rows[0] if rows else None

    def find_candidate(self, user_id: str, provider_place_id: str) -> dict | None:
        rows = (self.client.table("place_candidates").select("*").eq("user_id", user_id)
                .eq("provider_place_id", provider_place_id).eq("status", "candidate").limit(1)
                .execute().data or [])
        return rows[0] if rows else None

    def upsert_candidate(self, row: dict) -> dict:
        existing = self.find_candidate(row["user_id"], row["provider_place_id"])
        if existing:
            updated = {
                "last_seen_at": row["last_seen_at"],
                "accumulated_dwell_seconds": row["accumulated_dwell_seconds"],
                "normalized_brand": row.get("normalized_brand"),
                "normalized_category": row["normalized_category"],
            }
            rows = self.client.table("place_candidates").update(updated).eq("id", existing["id"]).execute().data or []
            return rows[0] if rows else {**existing, **updated}
        return (self.client.table("place_candidates").insert(row).execute().data or [row])[0]

    def mark_candidate(self, candidate_id: str, status: str) -> None:
        self.client.table("place_candidates").update({"status": status}).eq("id", candidate_id).execute()

    def recent_visit(self, user_id: str, provider_place_id: str, since: datetime) -> dict | None:
        rows = (self.client.table("place_visits").select("*").eq("user_id", user_id)
                .eq("provider_place_id", provider_place_id).gte("confirmed_at", iso(since))
                .order("confirmed_at", desc=True).limit(1).execute().data or [])
        return rows[0] if rows else None

    def create_visit(self, row: dict) -> dict:
        return (self.client.table("place_visits").insert(row).execute().data or [row])[0]

    def list_visits(self, user_id: str, limit: int = 20) -> list[dict]:
        return (self.client.table("place_visits").select("*").eq("user_id", user_id)
                .order("confirmed_at", desc=True).limit(limit).execute().data or [])

    def visits_by_category_since(self, user_id: str, category: str, since: datetime) -> list[dict]:
        return (self.client.table("place_visits").select("*").eq("user_id", user_id)
                .eq("normalized_category", category).gte("confirmed_at", iso(since)).execute().data or [])

    def get_classification(self, provider_place_id: str) -> dict | None:
        rows = (self.client.table("place_classification_cache").select("*")
                .eq("provider_place_id", provider_place_id).limit(1).execute().data or [])
        return rows[0] if rows else None

    def save_classification(self, row: dict) -> dict:
        existing = self.get_classification(row["provider_place_id"])
        if existing:
            rows = self.client.table("place_classification_cache").update(row).eq("id", existing["id"]).execute().data or []
            return rows[0] if rows else {**existing, **row}
        return (self.client.table("place_classification_cache").insert(row).execute().data or [row])[0]

    def list_watchers(self, user_id: str) -> list[dict]:
        return (self.client.table("watcher_rules").select("*").eq("user_id", user_id)
                .eq("is_active", True).order("created_at").execute().data or [])

    def create_watcher(self, row: dict) -> dict:
        return (self.client.table("watcher_rules").insert(row).execute().data or [row])[0]

    def delete_watcher(self, user_id: str, watcher_id: str) -> bool:
        self.client.table("watcher_rules").update({"is_active": False}).eq("user_id", user_id).eq("id", watcher_id).execute()
        return True

    def create_suggestion(self, row: dict) -> dict:
        return (self.client.table("habit_suggestions").insert(row).execute().data or [row])[0]

    def get_suggestion(self, user_id: str, suggestion_id: str) -> dict | None:
        rows = (self.client.table("habit_suggestions").select("*").eq("user_id", user_id)
                .eq("id", suggestion_id).limit(1).execute().data or [])
        return rows[0] if rows else None

    def suggestions_for_category_since(self, user_id: str, category: str, since: datetime) -> list[dict]:
        return (self.client.table("habit_suggestions").select("*").eq("user_id", user_id)
                .eq("category", category).gte("suggested_at", iso(since)).execute().data or [])

    def update_suggestion(self, user_id: str, suggestion_id: str, **fields) -> None:
        self.client.table("habit_suggestions").update(fields).eq("user_id", user_id).eq("id", suggestion_id).execute()

    def nudge_exists(self, deduplication_key: str) -> bool:
        rows = self.client.table("nudge_history").select("id").eq("deduplication_key", deduplication_key).limit(1).execute().data or []
        return bool(rows)

    def nudges_today(self, user_id: str, day_start: datetime) -> int:
        rows = (self.client.table("nudge_history").select("id").eq("user_id", user_id)
                .gte("sent_at", iso(day_start)).execute().data or [])
        return len(rows)

    def last_nudge_for_watcher(self, user_id: str, watcher_id: str) -> dict | None:
        rows = (self.client.table("nudge_history").select("*").eq("user_id", user_id)
                .eq("watcher_rule_id", watcher_id).order("sent_at", desc=True)
                .limit(1).execute().data or [])
        return rows[0] if rows else None

    def save_nudge(self, row: dict) -> dict:
        return (self.client.table("nudge_history").insert(row).execute().data or [row])[0]

    def delete_locations(self, user_id: str) -> None:
        for table in ("location_updates", "place_candidates", "place_visits"):
            self.client.table(table).delete().eq("user_id", user_id).execute()
        self.update_settings(user_id, tracking_enabled=False, last_location_at=None)

    def delete_habits(self, user_id: str) -> None:
        for table in ("watcher_rules", "habit_suggestions", "nudge_history"):
            self.client.table(table).delete().eq("user_id", user_id).execute()

    def cleanup_retention(self, user_id: str | None, location_retention_days: int) -> None:
        cutoff = iso(utcnow() - timedelta(days=location_retention_days))
        q = self.client.table("location_updates").delete().lt("received_at", cutoff)
        if user_id:
            q = q.eq("user_id", user_id)
        q.execute()


@dataclass
class InMemoryHabitRepository:
    settings: dict[str, dict] | None = None
    location_updates: list[dict] | None = None
    candidates: list[dict] | None = None
    visits: list[dict] | None = None
    classifications: list[dict] | None = None
    watchers: list[dict] | None = None
    suggestions: list[dict] | None = None
    nudges: list[dict] | None = None

    def __post_init__(self):
        self.settings = self.settings or {}
        self.location_updates = self.location_updates or []
        self.candidates = self.candidates or []
        self.visits = self.visits or []
        self.classifications = self.classifications or []
        self.watchers = self.watchers or []
        self.suggestions = self.suggestions or []
        self.nudges = self.nudges or []

    def _id(self, row: dict) -> dict:
        row.setdefault("id", str(uuid4()))
        return row

    def ensure_settings(self, user_id: str) -> dict:
        return self.settings.setdefault(user_id, {
            "user_id": user_id, "tracking_enabled": True, "nudges_enabled": True,
            "habit_suggestions_enabled": True, "timezone": "Asia/Seoul",
            "daily_nudge_limit": 3, "last_location_at": None,
        })

    def update_settings(self, user_id: str, **fields) -> dict:
        row = self.ensure_settings(user_id)
        row.update(fields)
        row["updated_at"] = iso(utcnow())
        return row

    def save_location_update(self, row: dict) -> dict:
        self.location_updates.append(self._id(dict(row)))
        return self.location_updates[-1]

    def get_last_location_update(self, user_id: str) -> dict | None:
        rows = [r for r in self.location_updates if r["user_id"] == user_id]
        return max(rows, key=lambda r: parse_dt(r["received_at"])) if rows else None

    def find_candidate(self, user_id: str, provider_place_id: str) -> dict | None:
        for r in self.candidates:
            if r["user_id"] == user_id and r["provider_place_id"] == provider_place_id and r["status"] == "candidate":
                return r
        return None

    def upsert_candidate(self, row: dict) -> dict:
        existing = self.find_candidate(row["user_id"], row["provider_place_id"])
        if existing:
            existing.update(row)
            return existing
        self.candidates.append(self._id(dict(row)))
        return self.candidates[-1]

    def mark_candidate(self, candidate_id: str, status: str) -> None:
        for r in self.candidates:
            if r["id"] == candidate_id:
                r["status"] = status

    def recent_visit(self, user_id: str, provider_place_id: str, since: datetime) -> dict | None:
        rows = [r for r in self.visits if r["user_id"] == user_id and r["provider_place_id"] == provider_place_id and parse_dt(r["confirmed_at"]) >= since]
        return rows[-1] if rows else None

    def create_visit(self, row: dict) -> dict:
        self.visits.append(self._id(dict(row)))
        return self.visits[-1]

    def list_visits(self, user_id: str, limit: int = 20) -> list[dict]:
        rows = [r for r in self.visits if r["user_id"] == user_id]
        rows.sort(key=lambda r: parse_dt(r["confirmed_at"]), reverse=True)
        return rows[:limit]

    def visits_by_category_since(self, user_id: str, category: str, since: datetime) -> list[dict]:
        return [r for r in self.visits if r["user_id"] == user_id and r["normalized_category"] == category and parse_dt(r["confirmed_at"]) >= since]

    def get_classification(self, provider_place_id: str) -> dict | None:
        return next((r for r in self.classifications if r["provider_place_id"] == provider_place_id), None)

    def save_classification(self, row: dict) -> dict:
        existing = self.get_classification(row["provider_place_id"])
        if existing:
            existing.update(row)
            return existing
        self.classifications.append(self._id(dict(row)))
        return self.classifications[-1]

    def list_watchers(self, user_id: str) -> list[dict]:
        return [r for r in self.watchers if r["user_id"] == user_id and r.get("is_active", True)]

    def create_watcher(self, row: dict) -> dict:
        self.watchers.append(self._id(dict(row)))
        return self.watchers[-1]

    def delete_watcher(self, user_id: str, watcher_id: str) -> bool:
        for r in self.watchers:
            if r["user_id"] == user_id and str(r["id"]).startswith(str(watcher_id)):
                r["is_active"] = False
                return True
        return False

    def create_suggestion(self, row: dict) -> dict:
        self.suggestions.append(self._id(dict(row)))
        return self.suggestions[-1]

    def get_suggestion(self, user_id: str, suggestion_id: str) -> dict | None:
        return next(
            (r for r in self.suggestions
             if r["user_id"] == user_id and str(r["id"]).startswith(str(suggestion_id))),
            None,
        )

    def suggestions_for_category_since(self, user_id: str, category: str, since: datetime) -> list[dict]:
        return [r for r in self.suggestions if r["user_id"] == user_id and r["category"] == category and parse_dt(r["suggested_at"]) >= since]

    def update_suggestion(self, user_id: str, suggestion_id: str, **fields) -> None:
        for r in self.suggestions:
            if r["user_id"] == user_id and str(r["id"]).startswith(str(suggestion_id)):
                r.update(fields)

    def nudge_exists(self, deduplication_key: str) -> bool:
        return any(r["deduplication_key"] == deduplication_key for r in self.nudges)

    def nudges_today(self, user_id: str, day_start: datetime) -> int:
        return len([r for r in self.nudges if r["user_id"] == user_id and parse_dt(r["sent_at"]) >= day_start])

    def last_nudge_for_watcher(self, user_id: str, watcher_id: str) -> dict | None:
        rows = [r for r in self.nudges if r["user_id"] == user_id and r.get("watcher_rule_id") == watcher_id]
        rows.sort(key=lambda r: parse_dt(r["sent_at"]), reverse=True)
        return rows[0] if rows else None

    def save_nudge(self, row: dict) -> dict:
        self.nudges.append(self._id(dict(row)))
        return self.nudges[-1]

    def delete_locations(self, user_id: str) -> None:
        self.location_updates[:] = [r for r in self.location_updates if r["user_id"] != user_id]
        self.candidates[:] = [r for r in self.candidates if r["user_id"] != user_id]
        self.visits[:] = [r for r in self.visits if r["user_id"] != user_id]

    def delete_habits(self, user_id: str) -> None:
        self.watchers[:] = [r for r in self.watchers if r["user_id"] != user_id]
        self.suggestions[:] = [r for r in self.suggestions if r["user_id"] != user_id]
        self.nudges[:] = [r for r in self.nudges if r["user_id"] != user_id]

    def cleanup_retention(self, user_id: str | None, location_retention_days: int) -> None:
        cutoff = utcnow() - timedelta(days=location_retention_days)
        self.location_updates[:] = [
            r for r in self.location_updates
            if (user_id and r["user_id"] != user_id) or parse_dt(r["received_at"]) >= cutoff
        ]
