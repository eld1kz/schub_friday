from datetime import date, timedelta

from opportunity_service import OpportunityService, format_report, parse_finds


def _find(**over):
    f = {"title": "AI Hack Seoul", "kind": "hackathon",
         "url": "https://aihack.example.com", "deadline": None,
         "location": "Seoul", "summary": "48h AI hackathon."}
    f.update(over)
    return f


# ---- parse_finds -----------------------------------------------------------

def test_parse_valid_array_with_prose_and_fences():
    text = 'Here you go:\n```json\n[{"title": "AI Hack Seoul", "kind": "hackathon", ' \
           '"url": "https://aihack.example.com", "deadline": "2026-09-01", ' \
           '"location": "Seoul", "summary": "48h AI hackathon."}]\n```'
    finds = parse_finds(text)
    assert len(finds) == 1
    assert finds[0]["title"] == "AI Hack Seoul"
    assert finds[0]["deadline"] == "2026-09-01"


def test_parse_drops_invalid_items():
    text = ('[{"title": "ok", "kind": "hackathon", "url": "https://a.com"},'
            ' {"title": "bad kind", "kind": "party", "url": "https://b.com"},'
            ' {"kind": "internship", "url": "https://no-title.com"},'
            ' "not a dict"]')
    finds = parse_finds(text)
    assert [f["title"] for f in finds] == ["ok"]


def test_parse_bad_deadline_becomes_none():
    finds = parse_finds('[{"title": "x", "kind": "program", "url": "https://c.com", '
                        '"deadline": "next week"}]')
    assert finds[0]["deadline"] is None


def test_parse_no_json_returns_empty():
    assert parse_finds("Sorry, I could not find anything.") == []
    assert parse_finds("") == []


# ---- format_report ---------------------------------------------------------

def test_format_report_includes_title_url_and_deadline_countdown():
    soon = (date.today() + timedelta(days=5)).isoformat()
    out = format_report([_find(deadline=soon)])
    assert "AI Hack Seoul" in out
    assert "https://aihack.example.com" in out
    assert "5d left" in out
    assert "Found 1 new opportunities" in out


def test_format_report_no_header():
    out = format_report([_find()], header=False)
    assert "Found" not in out.splitlines()[0]


# ---- scan (with fakes) -----------------------------------------------------

class FakeRepo:
    def __init__(self, seen=()):
        self._seen = set(seen)
        self.saved = []

    def seen_urls(self, user_id):
        return self._seen

    def save(self, rows):
        self.saved.extend(rows)
        return rows

    def list_recent(self, user_id, limit=20):
        return self.saved[:limit]


class FakeClaude:
    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kw):
        class Block:
            pass
        b = Block()
        b.text = self._text
        class Resp:
            content = [b]
        return Resp()


PAYLOAD = ('[{"title": "New Hack", "kind": "hackathon", "url": "https://new.com"},'
           ' {"title": "Old Hack", "kind": "hackathon", "url": "https://old.com"}]')


def test_scan_dedupes_and_saves_only_new():
    repo = FakeRepo(seen={"https://old.com"})
    svc = OpportunityService(repo, FakeClaude(PAYLOAD))
    out = svc.scan("u1")
    assert "New Hack" in out and "Old Hack" not in out
    assert [r["url"] for r in repo.saved] == ["https://new.com"]
    assert repo.saved[0]["user_id"] == "u1"


def test_scan_all_seen_reports_nothing_new():
    repo = FakeRepo(seen={"https://new.com", "https://old.com"})
    svc = OpportunityService(repo, FakeClaude(PAYLOAD))
    assert svc.scan("u1").startswith("No new opportunities")
    assert repo.saved == []


def test_scan_empty_search_result():
    svc = OpportunityService(FakeRepo(), FakeClaude("no luck, nothing found"))
    assert "came back empty" in svc.scan("u1")
