"""
Life admin: quick-capture inbox + deadline tracking with escalating warnings.
Pure logic over the repository; no LLM calls, so it stays fast and testable.
"""
from __future__ import annotations

from datetime import date

KIND_ICONS = {"idea": "💡", "task": "☑️", "note": "📝"}
WARN_AT_DAYS = (7, 3, 1, 0)


class LifeService:
    def __init__(self, repo):
        self.repo = repo

    # ---- inbox -------------------------------------------------------------

    def capture(self, user_id: str, text: str, kind: str = "note") -> str:
        if kind not in KIND_ICONS:
            kind = "note"
        text = (text or "").strip()
        if not text:
            return "Nothing to capture — the text was empty."
        self.repo.add_inbox_item(user_id, text, kind)
        return f"{KIND_ICONS[kind]} Captured: {text}"

    def show_inbox(self, user_id: str) -> str:
        items = self.repo.list_inbox(user_id)
        if not items:
            return "Inbox is empty — capture ideas with 'idea: ...' or 'note: ...'."
        lines = ["📥 Inbox:"]
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {KIND_ICONS.get(item['kind'], '📝')} {item['text']}")
        lines.append("\nSay e.g. \"inbox 2 done\" to clear an item.")
        return "\n".join(lines)

    def complete_inbox(self, user_id: str, number: int) -> str:
        items = self.repo.list_inbox(user_id)
        if not (1 <= number <= len(items)):
            return f"No inbox item #{number} — there are {len(items)} open items."
        item = items[number - 1]
        self.repo.close_inbox_item(item["id"])
        return f"✅ Done: {item['text']}"

    def inbox_context(self, user_id: str) -> str:
        """Open inbox items as plain text for the day planner."""
        items = self.repo.list_inbox(user_id)
        return "\n".join(f"- [{i['kind']}] {i['text']}" for i in items)

    # ---- deadlines ---------------------------------------------------------

    def add_deadline(self, user_id: str, title: str, due_date: str,
                     source: str = "manual") -> str:
        try:
            d = date.fromisoformat(due_date)
        except (ValueError, TypeError):
            return f"Could not parse due date '{due_date}' — use YYYY-MM-DD."
        self.repo.add_deadline(user_id, title.strip(), due_date, source)
        return f"⏳ Tracking: {title.strip()} — due {due_date} ({_days_str(d)})"

    def show_deadlines(self, user_id: str) -> str:
        rows = self.repo.list_deadlines(user_id)
        if not rows:
            return "No open deadlines. Add one with e.g. 'deadline: OS lab report by 2026-09-01'."
        lines = ["⏳ Open deadlines:"]
        for i, r in enumerate(rows, 1):
            d = date.fromisoformat(r["due_date"])
            lines.append(f"{i}. {r['title']} — {r['due_date']} ({_days_str(d)})")
        lines.append("\nSay e.g. \"deadline 1 done\" when it's handled.")
        return "\n".join(lines)

    def complete_deadline(self, user_id: str, number: int) -> str:
        rows = self.repo.list_deadlines(user_id)
        if not (1 <= number <= len(rows)):
            return f"No deadline #{number} — there are {len(rows)} open deadlines."
        row = rows[number - 1]
        self.repo.close_deadline(row["id"])
        return f"✅ Deadline done: {row['title']}"

    def deadline_context(self, user_id: str) -> str:
        """Open deadlines as plain text for the day planner."""
        rows = self.repo.list_deadlines(user_id)
        return "\n".join(
            f"- {r['title']} due {r['due_date']} ({_days_str(date.fromisoformat(r['due_date']))})"
            for r in rows
        )

    def pending_warnings(self, user_id: str, today: date | None = None) -> list[str]:
        """Escalating warnings at 7/3/1/0 days out. Marks each threshold as
        warned so it fires once; overdue items warn once too."""
        today = today or date.today()
        warnings = []
        for r in self.repo.list_deadlines(user_id):
            days = (date.fromisoformat(r["due_date"]) - today).days
            threshold = min((t for t in WARN_AT_DAYS if days <= t), default=None)
            if days < 0:
                threshold = 0
            if threshold is None:
                continue
            last = r.get("last_warned_days")
            if last is not None and last <= threshold:
                continue                     # already warned at this level or closer
            self.repo.mark_warned(r["id"], threshold)
            if days < 0:
                warnings.append(f"🚨 OVERDUE: {r['title']} was due {r['due_date']}.")
            elif days == 0:
                warnings.append(f"🚨 DUE TODAY: {r['title']}.")
            else:
                warnings.append(f"⏳ {r['title']} is due in {days} days ({r['due_date']}).")
        return warnings


def _days_str(d: date) -> str:
    days = (d - date.today()).days
    if days < 0:
        return f"{-days}d overdue"
    return "today" if days == 0 else f"{days}d left"
