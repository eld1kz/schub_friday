from __future__ import annotations

from habit_repository import iso, utcnow


class SupabasePlannerRepository:
    def __init__(self, client):
        self.client = client

    def get_plan(self, user_id: str, plan_date: str) -> dict | None:
        rows = (self.client.table("day_plans").select("*").eq("user_id", user_id)
                .eq("plan_date", plan_date).limit(1).execute().data or [])
        return rows[0] if rows else None

    def upsert_plan(self, user_id: str, plan_date: str, plan_text: str) -> dict:
        row = {"user_id": user_id, "plan_date": plan_date, "plan_text": plan_text,
               "updated_at": iso(utcnow())}
        return (self.client.table("day_plans")
                .upsert(row, on_conflict="user_id,plan_date")
                .execute().data or [row])[0]

    def set_review(self, user_id: str, plan_date: str, review_text: str) -> None:
        (self.client.table("day_plans")
         .update({"review_text": review_text, "updated_at": iso(utcnow())})
         .eq("user_id", user_id).eq("plan_date", plan_date).execute())
