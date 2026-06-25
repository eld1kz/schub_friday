from __future__ import annotations


class SupabaseHealthRepository:
    def __init__(self, client):
        self.client = client

    def upsert_daily(self, row: dict) -> dict:
        """Insert or overwrite the (user_id, metric_date) row."""
        return (self.client.table("health_metrics")
                .upsert(row, on_conflict="user_id,metric_date")
                .execute().data or [row])[0]

    def recent(self, user_id: str, days: int = 30) -> list[dict]:
        """Most recent rows first, newest `days` of them."""
        return (self.client.table("health_metrics").select("*")
                .eq("user_id", user_id).order("metric_date", desc=True)
                .limit(days).execute().data or [])
