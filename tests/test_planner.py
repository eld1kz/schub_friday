from datetime import date

from planner_service import PlannerService, _plan_context


class FakeRepo:
    def __init__(self, plans=None):
        self.plans = plans or {}          # plan_date -> row
        self.reviews = {}

    def get_plan(self, user_id, plan_date):
        return self.plans.get(plan_date)

    def upsert_plan(self, user_id, plan_date, plan_text):
        row = {"user_id": user_id, "plan_date": plan_date, "plan_text": plan_text}
        self.plans[plan_date] = row
        return row

    def set_review(self, user_id, plan_date, review_text):
        self.reviews[plan_date] = review_text
        if plan_date in self.plans:
            self.plans[plan_date]["review_text"] = review_text


class FakeClaude:
    def __init__(self, text="09:00-10:30 💻 Build Friday"):
        self._text = text
        self.last_context = None
        self.messages = self

    def create(self, **kw):
        self.last_context = kw["messages"][0]["content"]
        class B:
            pass
        b = B()
        b.text = self._text
        class R:
            content = [b]
        return R()


TODAY = date(2026, 8, 13)


def test_plan_today_saves_and_returns():
    repo, claude = FakeRepo(), FakeClaude()
    svc = PlannerService(repo, claude)
    out = svc.plan_today("u1", "10:00 lecture", "- pay rent", "OS: coded", today=TODAY)
    assert out == "09:00-10:30 💻 Build Friday"
    assert repo.plans["2026-08-13"]["plan_text"] == out
    assert "10:00 lecture" in claude.last_context
    assert "pay rent" in claude.last_context


def test_plan_today_includes_yesterday_rollover():
    repo = FakeRepo({"2026-08-12": {"plan_text": "old", "review_text": "➡️ finish tests"}})
    claude = FakeClaude()
    PlannerService(repo, claude).plan_today("u1", "", "", "", today=TODAY)
    assert "finish tests" in claude.last_context
    assert "ROLLOVER" in claude.last_context


def test_get_today_without_plan():
    svc = PlannerService(FakeRepo(), FakeClaude())
    assert "No plan for today" in svc.get_today("u1", today=TODAY)


def test_review_today_saves_review_for_tomorrow():
    repo = FakeRepo({"2026-08-13": {"plan_text": "09:00 work"}})
    svc = PlannerService(repo, FakeClaude("✅ Done\n➡️ Rolls to tomorrow: nothing"))
    out = svc.review_today("u1", "", "OS: done", today=TODAY)
    assert out.startswith("✅ Done")
    assert repo.reviews["2026-08-13"] == out


def test_review_without_plan():
    svc = PlannerService(FakeRepo(), FakeClaude())
    assert "No plan was made today" in svc.review_today("u1", "", "", today=TODAY)


def test_plan_context_handles_empty_sections():
    ctx = _plan_context(TODAY, "", "", "", None)
    assert "none" in ctx and "ROLLOVER" not in ctx
    assert "Thursday, 2026-08-13" in ctx
    assert "READINESS" not in ctx


def test_plan_today_includes_readiness_signal():
    repo, claude = FakeRepo(), FakeClaude()
    PlannerService(repo, claude).plan_today(
        "u1", "", "", "", readiness="Recover — rest or very easy day: short sleep (5.2h).",
        today=TODAY,
    )
    assert "READINESS SIGNAL" in claude.last_context
    assert "Recover" in claude.last_context
