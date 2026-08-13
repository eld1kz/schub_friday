from __future__ import annotations


class SupabaseOpportunityRepository:
    def __init__(self, client):
        self.client = client

    def seen_urls(self, user_id: str) -> set[str]:
        rows = (self.client.table("opportunities").select("url")
                .eq("user_id", user_id).execute().data or [])
        return {r["url"] for r in rows}

    def save(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        return self.client.table("opportunities").insert(rows).execute().data or rows

    def list_recent(self, user_id: str, limit: int = 20) -> list[dict]:
        return (self.client.table("opportunities").select("*").eq("user_id", user_id)
                .order("created_at", desc=True).limit(limit).execute().data or [])
