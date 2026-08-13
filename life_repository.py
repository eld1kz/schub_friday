from __future__ import annotations


class SupabaseLifeRepository:
    """Inbox items + deadlines (quick capture and deadline tracking)."""

    def __init__(self, client):
        self.client = client

    # ---- inbox -------------------------------------------------------------

    def add_inbox_item(self, user_id: str, text: str, kind: str) -> dict:
        row = {"user_id": user_id, "text": text, "kind": kind}
        return (self.client.table("inbox_items").insert(row).execute().data or [row])[0]

    def list_inbox(self, user_id: str, limit: int = 30) -> list[dict]:
        return (self.client.table("inbox_items").select("*").eq("user_id", user_id)
                .eq("status", "open").order("created_at").limit(limit).execute().data or [])

    def close_inbox_item(self, item_id: str) -> None:
        (self.client.table("inbox_items").update({"status": "done"})
         .eq("id", item_id).execute())

    # ---- deadlines ---------------------------------------------------------

    def add_deadline(self, user_id: str, title: str, due_date: str,
                     source: str = "manual") -> dict:
        row = {"user_id": user_id, "title": title, "due_date": due_date, "source": source}
        return (self.client.table("deadlines").insert(row).execute().data or [row])[0]

    def list_deadlines(self, user_id: str) -> list[dict]:
        return (self.client.table("deadlines").select("*").eq("user_id", user_id)
                .eq("status", "open").order("due_date").execute().data or [])

    def close_deadline(self, deadline_id: str) -> None:
        (self.client.table("deadlines").update({"status": "done"})
         .eq("id", deadline_id).execute())

    def mark_warned(self, deadline_id: str, days: int) -> None:
        (self.client.table("deadlines").update({"last_warned_days": days})
         .eq("id", deadline_id).execute())
