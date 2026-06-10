from __future__ import annotations

from uuid import uuid4

from habit_repository import iso, utcnow


class SupabaseStudyRepository:
    def __init__(self, client):
        self.client = client

    def get_active_plan(self, user_id: str) -> dict | None:
        rows = (self.client.table("study_plans").select("*").eq("user_id", user_id)
                .eq("status", "active").order("created_at", desc=True).limit(1)
                .execute().data or [])
        return rows[0] if rows else None

    def archive_active_plans(self, user_id: str) -> None:
        (self.client.table("study_plans")
         .update({"status": "archived", "updated_at": iso(utcnow())})
         .eq("user_id", user_id).eq("status", "active").execute())

    def save_plan(self, row: dict) -> dict:
        return (self.client.table("study_plans").insert(row).execute().data or [row])[0]

    def save_tasks(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        return self.client.table("study_tasks").insert(rows).execute().data or rows

    def list_tasks(self, plan_id: str) -> list[dict]:
        return (self.client.table("study_tasks").select("*").eq("plan_id", plan_id)
                .order("priority").order("created_at").execute().data or [])

    def update_task(self, task_id: str, **fields) -> dict | None:
        fields["updated_at"] = iso(utcnow())
        rows = (self.client.table("study_tasks").update(fields)
                .eq("id", task_id).execute().data or [])
        return rows[0] if rows else None

    def save_log(self, row: dict) -> dict:
        return (self.client.table("study_logs").insert(row).execute().data or [row])[0]

    def list_logs(self, user_id: str, since_iso: str, limit: int = 40) -> list[dict]:
        return (self.client.table("study_logs").select("*").eq("user_id", user_id)
                .gte("logged_at", since_iso).order("logged_at", desc=True).limit(limit)
                .execute().data or [])

    def list_insights(self, user_id: str) -> list[dict]:
        return (self.client.table("learning_profile").select("*").eq("user_id", user_id)
                .eq("is_active", True).order("confidence", desc=True).execute().data or [])

    def deactivate_insights(self, user_id: str) -> None:
        (self.client.table("learning_profile")
         .update({"is_active": False, "updated_at": iso(utcnow())})
         .eq("user_id", user_id).eq("is_active", True).execute())

    def save_insights(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        return self.client.table("learning_profile").insert(rows).execute().data or rows


class InMemoryStudyRepository:
    """Test double mirroring SupabaseStudyRepository."""

    def __init__(self):
        self.plans: list[dict] = []
        self.tasks: list[dict] = []
        self.logs: list[dict] = []
        self.insights: list[dict] = []

    def get_active_plan(self, user_id: str) -> dict | None:
        rows = [p for p in self.plans if p["user_id"] == user_id and p["status"] == "active"]
        return rows[-1] if rows else None

    def archive_active_plans(self, user_id: str) -> None:
        for p in self.plans:
            if p["user_id"] == user_id and p["status"] == "active":
                p["status"] = "archived"

    def save_plan(self, row: dict) -> dict:
        row = {"id": str(uuid4()), **row}
        self.plans.append(row)
        return row

    def save_tasks(self, rows: list[dict]) -> list[dict]:
        saved = [{"id": str(uuid4()), **r} for r in rows]
        self.tasks.extend(saved)
        return saved

    def list_tasks(self, plan_id: str) -> list[dict]:
        return sorted(
            [t for t in self.tasks if t["plan_id"] == plan_id],
            key=lambda t: t.get("priority", 2),
        )

    def update_task(self, task_id: str, **fields) -> dict | None:
        for t in self.tasks:
            if t["id"] == task_id:
                t.update(fields)
                return t
        return None

    def save_log(self, row: dict) -> dict:
        row = {"id": str(uuid4()), **row}
        self.logs.append(row)
        return row

    def list_logs(self, user_id: str, since_iso: str, limit: int = 40) -> list[dict]:
        rows = [l for l in self.logs if l["user_id"] == user_id and l["logged_at"] >= since_iso]
        return sorted(rows, key=lambda l: l["logged_at"], reverse=True)[:limit]

    def list_insights(self, user_id: str) -> list[dict]:
        rows = [i for i in self.insights if i["user_id"] == user_id and i["is_active"]]
        return sorted(rows, key=lambda i: i.get("confidence", 0), reverse=True)

    def deactivate_insights(self, user_id: str) -> None:
        for i in self.insights:
            if i["user_id"] == user_id:
                i["is_active"] = False

    def save_insights(self, rows: list[dict]) -> list[dict]:
        saved = [{"id": str(uuid4()), **r} for r in rows]
        self.insights.extend(saved)
        return saved
