from __future__ import annotations

import json
from datetime import timedelta

from habit_logging import metric
from habit_repository import iso, utcnow

TASK_STATUSES = ("idea", "researched", "coded", "tested", "reviewed", "done")
DAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_INSIGHTS = 12

STATUS_ICONS = {
    "idea": "💭", "researched": "📖", "coded": "💻",
    "tested": "🧪", "reviewed": "👀", "done": "✅",
}


class StudyPlannerService:
    """Smart Study/Dev Planner: weekly plan generation, pipeline task tracking,
    and a learning loop that turns study logs into insights injected into the
    next plan. Public methods return user-facing strings for run_tool."""

    PLAN_SYSTEM = (
        "You are a study and project planning engine. Convert the planning request "
        "into ONE strict JSON object, no prose, with keys:\n"
        '  "title": string, e.g. "Week of Jun 10 — OS + ML + side project"\n'
        '  "focus": one sentence stating the week\'s single most important outcome\n'
        '  "tasks": array of 5-12 items, each {"course": short label like "OS", '
        '"title": one concrete action sized for one sitting, "day": one of '
        'Mon|Tue|Wed|Thu|Fri|Sat|Sun, "est_minutes": integer, "priority": 1-3 '
        '(1 highest), "hook": one short tip or starting point for this exact task}\n'
        '  "milestones": array of 2-4 checkpoint strings\n'
        '  "tips": array of 1-3 strings of personalized advice drawn from the '
        "profile insights and recent logs\n"
        "Rules:\n"
        "- Profile insights are verified patterns about this user; weight them "
        "heavily (session length, techniques, weak topics, schedule).\n"
        "- Dev/project tasks move through the pipeline idea -> researched -> coded "
        "-> tested -> reviewed -> done; phrase each task as the next pipeline step.\n"
        "- Prefer fewer, deeper work blocks over many shallow ones.\n"
        "- If a current plan is provided, carry over its unfinished high-priority "
        "tasks instead of inventing duplicates.\n"
        "- If web search is available and the request needs fresh resources, search "
        "at most twice, then fold findings into task hooks."
    )

    REFLECT_SYSTEM = (
        "You are a learning-loop analyst. From the study logs and task stats "
        "provided, output ONE strict JSON object, no prose:\n"
        '  "summary": <=120 words to the user in second person: what went well, '
        "what didn't, one concrete adjustment for next week\n"
        '  "insights": array of <=12 items, each {"kind": technique|strength|'
        'weakness|schedule, "insight": one-sentence durable generalization, '
        '"confidence": 0..1}\n'
        "Rules:\n"
        "- Insights must generalize (e.g. 'You retain more with 50-minute deep "
        "work blocks'), never restate a single event.\n"
        "- Merge with the existing insights: keep ones still supported, drop "
        "contradicted ones, refine wording. Your array REPLACES the stored set.\n"
        "- Be conservative with confidence; raise it only when several logs agree."
    )

    LOG_SYSTEM = (
        "Parse one study/dev log message into strict JSON, no prose, keys: "
        '"course" (short label or null), "task_ref" (a few words naming the task '
        'it refers to, or null), "metric" (e.g. "midterm 92%", or null), '
        '"worked" (what helped, or null), "failed" (what hurt, or null), '
        '"mood" (one word or null).'
    )

    def __init__(self, repo, claude_client,
                 plan_model: str = "claude-sonnet-4-6",
                 parse_model: str = "claude-haiku-4-5"):
        self.repo = repo
        self.claude = claude_client
        self.plan_model = plan_model
        self.parse_model = parse_model

    # ---- plan generation ----------------------------------------------------

    def generate_plan(self, user_id: str, request: str,
                      extra_context: str = "", include_resources: bool = False) -> str:
        payload = {
            "request": request,
            "today": utcnow().strftime("%Y-%m-%d (%A)"),
            "profile_insights": [
                f"[{i['kind']}] {i['insight']}" for i in self.repo.list_insights(user_id)
            ],
            "recent_logs": [
                l["raw_text"] for l in
                self.repo.list_logs(user_id, iso(utcnow() - timedelta(days=14)), limit=30)
            ],
        }
        current = self.repo.get_active_plan(user_id)
        if current:
            payload["current_plan"] = {
                "title": current["title"],
                "tasks": [
                    {"course": t["course"], "title": t["title"], "status": t["status"],
                     "priority": t.get("priority", 2)}
                    for t in self.repo.list_tasks(current["id"])
                ],
            }
        if extra_context:
            payload["user_facts"] = extra_context

        tools = [{"type": "web_search_20250305", "name": "web_search"}] if include_resources else []
        resp = self.claude.messages.create(
            model=self.plan_model,
            max_tokens=2000,
            system=self.PLAN_SYSTEM,
            tools=tools,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        self._log_usage(resp)
        data = self._json_from_response(resp)
        if not self._valid_plan(data):
            return "Couldn't generate a valid plan — please rephrase the request."

        self.repo.archive_active_plans(user_id)
        now = iso(utcnow())
        plan = self.repo.save_plan({
            "user_id": user_id,
            "title": data["title"],
            "focus": data.get("focus"),
            "milestones": data.get("milestones", []),
            "tips": data.get("tips", []),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        })
        tasks = self.repo.save_tasks([
            {
                "plan_id": plan["id"], "user_id": user_id,
                "course": str(t["course"]), "title": str(t["title"]),
                "day": t.get("day") if t.get("day") in DAY_ORDER else None,
                "est_minutes": t.get("est_minutes"),
                "priority": int(t.get("priority") or 2),
                "hook": t.get("hook"), "status": "idea",
                "created_at": now, "updated_at": now,
            }
            for t in data["tasks"]
        ])
        return (self._render_plan(plan, tasks, show_hooks=True)
                + "\n\nSaved as your active plan. You can say 'show my plan', "
                  "'mark <task> as coded/done', or 'log: <result>'.")

    def show_current_plan(self, user_id: str) -> str:
        plan = self.repo.get_active_plan(user_id)
        if not plan:
            return "No active study plan. Ask me to plan your week to create one."
        return self._render_plan(plan, self.repo.list_tasks(plan["id"]), show_hooks=False)

    # ---- task tracking -------------------------------------------------------

    def update_task_status(self, user_id: str, ref: str, status: str) -> str:
        if status not in TASK_STATUSES:
            return f"Unknown status '{status}'. Use one of: {', '.join(TASK_STATUSES)}."
        plan = self.repo.get_active_plan(user_id)
        if not plan:
            return "No active study plan."
        needle = ref.lower()
        matches = [
            t for t in self.repo.list_tasks(plan["id"])
            if needle in t["title"].lower() or needle in t["course"].lower()
        ]
        if not matches:
            return f"No task in the current plan matches '{ref}'."
        if len(matches) > 1:
            listing = "\n".join(f"- [{t['course']}] {t['title']}" for t in matches[:5])
            return f"Multiple tasks match '{ref}' — which one?\n{listing}"
        task = matches[0]
        self.repo.update_task(task["id"], status=status)
        return f"{STATUS_ICONS[status]} [{task['course']}] {task['title']} → {status}"

    # ---- learning loop -------------------------------------------------------

    def log_result(self, user_id: str, text: str) -> str:
        parsed = {}
        try:
            resp = self.claude.messages.create(
                model=self.parse_model,
                max_tokens=200,
                system=self.LOG_SYSTEM,
                messages=[{"role": "user", "content": text}],
            )
            self._log_usage(resp)
            parsed = self._json_from_response(resp)
        except Exception as e:
            print(f"[planner] log parse failed, storing raw only: {e}")
        self.repo.save_log({
            "user_id": user_id,
            "course": parsed.get("course"),
            "task_ref": parsed.get("task_ref"),
            "metric": parsed.get("metric"),
            "worked": parsed.get("worked"),
            "failed": parsed.get("failed"),
            "mood": parsed.get("mood"),
            "raw_text": text,
            "logged_at": iso(utcnow()),
        })
        return "Logged. It will shape your next plan and reflections."

    def reflect(self, user_id: str, days: int = 7) -> str:
        logs = self.repo.list_logs(user_id, iso(utcnow() - timedelta(days=days)))
        if not logs:
            return f"No study logs in the last {days} days — nothing to reflect on yet."
        payload = {
            "window_days": days,
            "logs": [
                {k: l.get(k) for k in
                 ("raw_text", "course", "metric", "worked", "failed", "mood", "logged_at")}
                for l in logs
            ],
            "existing_insights": [
                {"kind": i["kind"], "insight": i["insight"], "confidence": i["confidence"]}
                for i in self.repo.list_insights(user_id)
            ],
            "task_stats": self._task_stats(user_id),
        }
        resp = self.claude.messages.create(
            model=self.plan_model,
            max_tokens=1500,
            system=self.REFLECT_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        self._log_usage(resp)
        data = self._json_from_response(resp)
        insights = [i for i in data.get("insights", []) if self._valid_insight(i)][:MAX_INSIGHTS]
        if insights:
            now = iso(utcnow())
            self.repo.deactivate_insights(user_id)
            self.repo.save_insights([
                {"user_id": user_id, "kind": i["kind"], "insight": i["insight"],
                 "confidence": float(i["confidence"]), "is_active": True,
                 "created_at": now, "updated_at": now}
                for i in insights
            ])
        summary = data.get("summary") or "Reflection complete."
        bullets = "\n".join(f"- [{i['kind']}] {i['insight']}" for i in insights)
        return f"🪞 {summary}" + (f"\n\nWhat I'll factor into future plans:\n{bullets}" if bullets else "")

    def get_insights(self, user_id: str) -> str:
        rows = self.repo.list_insights(user_id)
        if not rows:
            return "No learning insights yet. Log a few study results, then ask me to review your week."
        return "🧠 What I know about how you learn:\n" + "\n".join(
            f"- [{r['kind']}] {r['insight']}" for r in rows
        )

    # ---- helpers -------------------------------------------------------------

    def _task_stats(self, user_id: str) -> dict:
        plan = self.repo.get_active_plan(user_id)
        if not plan:
            return {}
        stats: dict[str, dict] = {}
        for t in self.repo.list_tasks(plan["id"]):
            s = stats.setdefault(t["course"], {"done": 0, "total": 0})
            s["total"] += 1
            s["done"] += t["status"] == "done"
        return stats

    def _render_plan(self, plan: dict, tasks: list[dict], show_hooks: bool) -> str:
        lines = [f"📚 {plan['title']}"]
        if plan.get("focus"):
            lines.append(f"🎯 Focus: {plan['focus']}")
        by_day = {d: [] for d in DAY_ORDER}
        unscheduled = []
        for t in tasks:
            (by_day[t["day"]] if t.get("day") in by_day else unscheduled).append(t)
        for day in DAY_ORDER:
            if not by_day[day]:
                continue
            lines.append(f"\n{day}")
            for t in by_day[day]:
                lines.append(self._render_task(t, show_hooks))
        if unscheduled:
            lines.append("\nAnytime")
            lines.extend(self._render_task(t, show_hooks) for t in unscheduled)
        milestones = plan.get("milestones") or []
        if milestones:
            lines.append("\n🏁 " + " · ".join(milestones))
        for tip in plan.get("tips") or []:
            lines.append(f"💡 {tip}")
        return "\n".join(lines)

    def _render_task(self, t: dict, show_hooks: bool) -> str:
        mins = f" ({t['est_minutes']}m)" if t.get("est_minutes") else ""
        line = f"  {STATUS_ICONS.get(t['status'], '•')} [{t['course']}] {t['title']}{mins}"
        if show_hooks and t.get("hook"):
            line += f"\n     ↳ {t['hook']}"
        return line

    def _valid_plan(self, data) -> bool:
        return (
            isinstance(data, dict)
            and isinstance(data.get("title"), str)
            and isinstance(data.get("tasks"), list)
            and len(data["tasks"]) > 0
            and all(isinstance(t, dict) and t.get("course") and t.get("title")
                    for t in data["tasks"])
        )

    def _valid_insight(self, i) -> bool:
        return (
            isinstance(i, dict)
            and i.get("kind") in {"technique", "strength", "weakness", "schedule"}
            and isinstance(i.get("insight"), str)
            and 0 <= float(i.get("confidence") or 0) <= 1
        )

    def _json_from_response(self, resp) -> dict:
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
        return json.loads(raw)

    def _log_usage(self, resp) -> None:
        usage = getattr(resp, "usage", None)
        if usage:
            metric(
                "llm_tokens",
                prompt_tokens=getattr(usage, "input_tokens", 0),
                completion_tokens=getattr(usage, "output_tokens", 0),
            )
