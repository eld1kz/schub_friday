from __future__ import annotations

import json
from types import SimpleNamespace

from study_planner_service import StudyPlannerService
from study_repository import InMemoryStudyRepository


PLAN_JSON = {
    "title": "Week of Jun 10 — OS + ML",
    "focus": "Ship the mutex implementation and finish ML week 3.",
    "tasks": [
        {"course": "OS", "title": "Implement mutex with test harness", "day": "Mon",
         "est_minutes": 50, "priority": 1, "hook": "Start from the lecture 7 pseudocode."},
        {"course": "ML", "title": "Derive backprop for week 3 notes", "day": "Tue",
         "est_minutes": 50, "priority": 2, "hook": "Redo the chain-rule example first."},
    ],
    "milestones": ["Mutex passes tests by Wed"],
    "tips": ["Keep the 50-minute blocks that worked last week."],
}

REFLECT_JSON = {
    "summary": "Strong week: the midterm result confirms deep-work blocks help.",
    "insights": [
        {"kind": "technique", "insight": "You retain more with 50-minute deep work blocks.",
         "confidence": 0.8},
    ],
}

LOG_JSON = {
    "course": "OS", "task_ref": "midterm", "metric": "midterm 92%",
    "worked": "Pomodoro", "failed": None, "mood": "good",
}


class FakeClaude:
    """Returns queued JSON payloads; records every request it receives."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        payload = self._payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(payload))], usage=None
        )


def service(payloads):
    repo = InMemoryStudyRepository()
    claude = FakeClaude(payloads)
    return repo, claude, StudyPlannerService(repo, claude)


def test_generate_plan_saves_plan_and_tasks_and_renders_it():
    repo, claude, svc = service([PLAN_JSON])
    out = svc.generate_plan("1", "plan my week for OS + ML")
    assert "Week of Jun 10" in out and "mutex" in out.lower()
    assert len(repo.plans) == 1 and repo.plans[0]["status"] == "active"
    assert len(repo.tasks) == 2 and all(t["status"] == "idea" for t in repo.tasks)


def test_generate_plan_archives_previous_active_plan():
    repo, claude, svc = service([PLAN_JSON, PLAN_JSON])
    svc.generate_plan("1", "plan my week")
    svc.generate_plan("1", "replan my week")
    statuses = [p["status"] for p in repo.plans]
    assert statuses == ["archived", "active"]


def test_generate_plan_injects_insights_and_recent_logs_into_prompt():
    repo, claude, svc = service([LOG_JSON, PLAN_JSON])
    repo.save_insights([{
        "user_id": "1", "kind": "technique", "confidence": 0.8, "is_active": True,
        "insight": "You retain more with 50-minute deep work blocks.",
    }])
    svc.log_result("1", "log: OS midterm 92%, Pomodoro worked well")
    svc.generate_plan("1", "plan my week")
    sent = claude.requests[-1]["messages"][0]["content"]
    assert "50-minute deep work blocks" in sent
    assert "Pomodoro worked well" in sent


def test_update_task_status_matches_by_substring():
    repo, claude, svc = service([PLAN_JSON])
    svc.generate_plan("1", "plan my week")
    out = svc.update_task_status("1", "mutex", "coded")
    assert "coded" in out
    assert [t["status"] for t in repo.tasks if "mutex" in t["title"].lower()] == ["coded"]


def test_update_task_status_rejects_unknown_status_and_ambiguous_ref():
    repo, claude, svc = service([PLAN_JSON])
    svc.generate_plan("1", "plan my week")
    assert "Unknown status" in svc.update_task_status("1", "mutex", "finished")
    ambiguous = svc.update_task_status("1", "es", "done")  # 'test'/'notes': both titles
    assert "which one" in ambiguous.lower()


def test_log_result_stores_raw_text_even_when_parse_fails():
    repo, claude, svc = service([RuntimeError("api down")])
    svc.log_result("1", "log: OS midterm 92%")
    assert repo.logs[0]["raw_text"] == "log: OS midterm 92%"
    assert repo.logs[0]["course"] is None


def test_reflect_replaces_insight_set_and_returns_summary():
    repo, claude, svc = service([LOG_JSON, REFLECT_JSON])
    repo.save_insights([{
        "user_id": "1", "kind": "schedule", "confidence": 0.4, "is_active": True,
        "insight": "Old insight to be replaced.",
    }])
    svc.log_result("1", "log: OS midterm 92%, Pomodoro worked well")
    out = svc.reflect("1")
    active = repo.list_insights("1")
    assert "Strong week" in out
    assert [i["insight"] for i in active] == ["You retain more with 50-minute deep work blocks."]


def test_reflect_without_logs_returns_friendly_message():
    repo, claude, svc = service([])
    assert "nothing to reflect" in svc.reflect("1").lower()
