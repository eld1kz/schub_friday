"""
Planning layer ("what should I do today?"). Morning: turns calendar, due
reminders, study/project tasks, and yesterday's unfinished items into a
realistic time-blocked day plan. Evening: reviews the day and writes the
rollover note that seeds tomorrow's plan.

The caller (assistant_step4 / daily_digest) gathers the context strings so
this service stays free of calendar/DB imports and easy to test.
"""
from __future__ import annotations

from datetime import date, timedelta

PLAN_SYSTEM = (
    "You are the user's personal planner writing TODAY'S plan, sent to their "
    "Telegram. From the data provided, produce a realistic time-blocked plan "
    "for the day (times in KST). Rules: fixed calendar events are immovable — "
    "plan around them; fit focused project/study blocks (60-90 min) into the "
    "gaps, picking the most important unfinished tasks; include due reminders "
    "at sensible times; leave real breaks and don't overpack — a plan that "
    "fits is better than one that impresses. If yesterday's rollover lists "
    "unfinished items, schedule them first. Format: short lines like "
    "'09:00-10:30 ...' with a few emojis, then one 'Top priority:' line. "
    "Plain text only — no Markdown (no ** or #), it renders literally in "
    "Telegram. Under ~150 words. Always write in English."
)

REVIEW_SYSTEM = (
    "You are the user's personal planner writing a short EVENING review of "
    "today's plan, sent to their Telegram. Compare the plan with the current "
    "task statuses and reminders. Output two short sections: '✅ Done' — what "
    "got completed (be encouraging, and infer completion from task statuses, "
    "not wishful thinking); '➡️ Rolls to tomorrow' — plan items that look "
    "unfinished, each on its own line. If everything got done, say so and "
    "leave the rollover empty. Plain text only — no Markdown (no ** or #). "
    "Under ~100 words. Always write in English."
)


class PlannerService:
    def __init__(self, repo, claude_client):
        self.repo = repo
        self.claude = claude_client

    def plan_today(self, user_id: str, calendar: str, reminders: str,
                   tasks: str, today: date | None = None) -> str:
        """Generate (or regenerate) today's plan and store it."""
        today = today or date.today()
        rollover = self._rollover_from_yesterday(user_id, today)
        context = _plan_context(today, calendar, reminders, tasks, rollover)
        try:
            plan = self._ask(PLAN_SYSTEM, context)
        except Exception as e:
            return f"Could not build today's plan: {type(e).__name__}: {e}"
        try:
            self.repo.upsert_plan(user_id, today.isoformat(), plan)
        except Exception as e:
            print(f"[plan] save failed (still returning plan): {e}")
        return plan

    def get_today(self, user_id: str, today: date | None = None) -> str:
        today = today or date.today()
        row = self.repo.get_plan(user_id, today.isoformat())
        if not row:
            return "No plan for today yet — ask me to plan your day."
        return row["plan_text"]

    def review_today(self, user_id: str, reminders: str, tasks: str,
                     today: date | None = None) -> str:
        """Evening check-in: what got done, what rolls to tomorrow. Stored so
        tomorrow's plan picks the rollover up."""
        today = today or date.today()
        row = self.repo.get_plan(user_id, today.isoformat())
        if not row:
            return "No plan was made today, so nothing to review — fresh start tomorrow."
        context = (
            f"TODAY'S PLAN ({today.isoformat()}):\n{row['plan_text']}\n\n"
            f"CURRENT TASK STATUSES:\n{tasks or 'none'}\n\n"
            f"REMINDERS:\n{reminders or 'none'}"
        )
        try:
            review = self._ask(REVIEW_SYSTEM, context)
        except Exception as e:
            return f"Could not review the day: {type(e).__name__}: {e}"
        try:
            self.repo.set_review(user_id, today.isoformat(), review)
        except Exception as e:
            print(f"[plan] review save failed: {e}")
        return review

    # ---- internals ---------------------------------------------------------

    def _rollover_from_yesterday(self, user_id: str, today: date) -> str | None:
        try:
            row = self.repo.get_plan(user_id, (today - timedelta(days=1)).isoformat())
        except Exception:
            return None
        return (row or {}).get("review_text")

    def _ask(self, system: str, context: str) -> str:
        resp = self.claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=700, system=system,
            messages=[{"role": "user", "content": context}],
        )
        return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


def _plan_context(today: date, calendar: str, reminders: str,
                  tasks: str, rollover: str | None) -> str:
    lines = [f"Planning for {today.strftime('%A, %Y-%m-%d')} (KST).", ""]
    if rollover:
        lines += ["YESTERDAY'S REVIEW / ROLLOVER:", rollover, ""]
    lines += ["FIXED CALENDAR EVENTS:", (calendar or "").strip() or "none", ""]
    lines += ["REMINDERS:", (reminders or "").strip() or "none", ""]
    lines += ["PROJECT/STUDY TASKS (with statuses):", (tasks or "").strip() or "none"]
    return "\n".join(lines)
