from datetime import date, timedelta

from life_service import LifeService

TODAY = date(2026, 8, 13)


class FakeRepo:
    def __init__(self):
        self.inbox = []
        self.deadlines = []

    # inbox
    def add_inbox_item(self, user_id, text, kind):
        row = {"id": str(len(self.inbox)), "user_id": user_id, "text": text,
               "kind": kind, "status": "open"}
        self.inbox.append(row)
        return row

    def list_inbox(self, user_id, limit=30):
        return [r for r in self.inbox if r["status"] == "open"][:limit]

    def close_inbox_item(self, item_id):
        next(r for r in self.inbox if r["id"] == item_id)["status"] = "done"

    # deadlines
    def add_deadline(self, user_id, title, due_date, source="manual"):
        row = {"id": str(len(self.deadlines)), "user_id": user_id, "title": title,
               "due_date": due_date, "status": "open", "last_warned_days": None}
        self.deadlines.append(row)
        return row

    def list_deadlines(self, user_id):
        return sorted((r for r in self.deadlines if r["status"] == "open"),
                      key=lambda r: r["due_date"])

    def close_deadline(self, deadline_id):
        next(r for r in self.deadlines if r["id"] == deadline_id)["status"] = "done"

    def mark_warned(self, deadline_id, days):
        next(r for r in self.deadlines if r["id"] == deadline_id)["last_warned_days"] = days


def _svc():
    return LifeService(FakeRepo())


# ---- inbox -----------------------------------------------------------------

def test_capture_and_list():
    svc = _svc()
    assert "Captured" in svc.capture("u", "try lablab hackathon", "idea")
    assert svc.capture("u", "   ", "note").startswith("Nothing to capture")
    out = svc.show_inbox("u")
    assert "1. 💡 try lablab hackathon" in out


def test_capture_bad_kind_defaults_to_note():
    svc = _svc()
    svc.capture("u", "x", "banana")
    assert svc.repo.inbox[0]["kind"] == "note"


def test_complete_inbox_by_number():
    svc = _svc()
    svc.capture("u", "a")
    svc.capture("u", "b")
    assert "Done: a" in svc.complete_inbox("u", 1)
    assert "b" in svc.show_inbox("u") and "1. 📝 b" in svc.show_inbox("u")
    assert "No inbox item #5" in svc.complete_inbox("u", 5)


# ---- deadlines -------------------------------------------------------------

def test_add_deadline_validates_date():
    svc = _svc()
    assert "Could not parse" in svc.add_deadline("u", "x", "next week")
    assert "Tracking" in svc.add_deadline("u", "OS report", "2027-01-01")


def test_show_and_complete_deadlines():
    svc = _svc()
    svc.add_deadline("u", "apply to junction", (date.today() + timedelta(days=5)).isoformat())
    out = svc.show_deadlines("u")
    assert "apply to junction" in out and "5d left" in out
    assert "Deadline done" in svc.complete_deadline("u", 1)
    assert "No open deadlines" in svc.show_deadlines("u")


# ---- warnings --------------------------------------------------------------

def _deadline_in(svc, days):
    svc.add_deadline("u", f"task-{days}", (TODAY + timedelta(days=days)).isoformat())


def test_warnings_fire_at_thresholds_once():
    svc = _svc()
    _deadline_in(svc, 10)   # too far — no warning
    _deadline_in(svc, 6)    # inside 7-day window
    _deadline_in(svc, 0)    # due today
    w = svc.pending_warnings("u", today=TODAY)
    assert len(w) == 2
    assert any("DUE TODAY" in x for x in w)
    assert any("due in 6 days" in x for x in w)
    # second call: nothing new
    assert svc.pending_warnings("u", today=TODAY) == []


def test_warnings_escalate_as_deadline_approaches():
    svc = _svc()
    _deadline_in(svc, 6)
    assert len(svc.pending_warnings("u", today=TODAY)) == 1          # 7-day warn
    assert len(svc.pending_warnings("u", today=TODAY + timedelta(days=4))) == 1  # 3-day
    assert len(svc.pending_warnings("u", today=TODAY + timedelta(days=5))) == 1  # 1-day
    assert len(svc.pending_warnings("u", today=TODAY + timedelta(days=6))) == 1  # due day
    assert svc.pending_warnings("u", today=TODAY + timedelta(days=6)) == []


def test_overdue_warns_once():
    svc = _svc()
    svc.add_deadline("u", "late thing", (TODAY - timedelta(days=2)).isoformat())
    w = svc.pending_warnings("u", today=TODAY)
    assert len(w) == 1 and "OVERDUE" in w[0]
    assert svc.pending_warnings("u", today=TODAY) == []
