"""
Opportunity scout: finds hackathons, internships, and programs via Claude
web search, dedupes against previously shown ones, and formats a Telegram
message. Used both on demand (find_opportunities tool) and by the scheduled
scan in assistant_step4.
"""
from __future__ import annotations

import json
import os
from datetime import date

KINDS = ("hackathon", "internship", "program")

KIND_ICONS = {"hackathon": "🏆", "internship": "💼", "program": "🎓"}

DEFAULT_PROFILE = (
    "A university CS student based in Seoul, South Korea. Builds with Python "
    "and AI/LLMs (Telegram bots, agents, APIs). Speaks English and Russian. "
    "Interested in: hackathons and coding competitions (in Korea, online, or "
    "major international ones), student-friendly internships and junior roles "
    "in AI/software (Korea or remote), and accelerators, fellowships, grants, "
    "and student programs."
)

SCAN_SYSTEM = """You are an opportunity scout. Using web search, find CURRENT, \
still-open opportunities matching the user's profile: hackathons (local, online, \
and major global ones), internships / junior roles, and programs (accelerators, \
fellowships, grants, student programs). Only include opportunities whose \
deadline or event date is in the future; verify with search, don't guess from \
memory. Prefer official pages over aggregator listings for the url.

After searching, output ONLY a JSON array (no prose, no code fences). Each object:
  title    : official name of the opportunity
  kind     : "hackathon" | "internship" | "program"
  url      : link to the official page (or the most direct listing)
  deadline : application/registration deadline as "YYYY-MM-DD", or null if unknown
  location : "online", a city, or "City (global)" for international events
  summary  : 1-2 sentences: what it is and why it fits this user

Aim for 5-10 solid finds. Quality over quantity: skip anything expired, vague,
or clearly out of reach."""


class OpportunityService:
    def __init__(self, repo, claude_client):
        self.repo = repo
        self.claude = claude_client
        self.profile = os.environ.get("OPPORTUNITY_PROFILE", DEFAULT_PROFILE)

    # ---- scan -------------------------------------------------------------

    def scan(self, user_id: str, focus: str | None = None) -> str:
        """Search, dedupe, store, and return a formatted report of NEW finds."""
        try:
            found = self._search(focus)
        except Exception as e:
            return f"Opportunity search failed: {type(e).__name__}: {e}"
        if not found:
            return "The search came back empty — try again later or narrow the ask."

        seen = self.repo.seen_urls(user_id)
        new = [f for f in found if f["url"] not in seen]
        if not new:
            return ("No new opportunities since the last scan "
                    f"({len(found)} found, all already shown).")

        rows = [{**f, "user_id": user_id} for f in new]
        try:
            self.repo.save(rows)
        except Exception as e:
            print(f"[opp] save failed (still reporting): {e}")
        return format_report(new)

    def _search(self, focus: str | None) -> list[dict]:
        """One Claude call with server-side web search -> validated finds."""
        ask = f"Profile: {self.profile}"
        if focus:
            ask += f"\n\nThis scan, focus specifically on: {focus}"
        ask += f"\n\nToday's date: {date.today().isoformat()}"
        resp = self.claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=4000,
            system=SCAN_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
            messages=[{"role": "user", "content": ask}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        return parse_finds(text)

    # ---- listing ----------------------------------------------------------

    def list_saved(self, user_id: str, limit: int = 10) -> str:
        rows = self.repo.list_recent(user_id, limit)
        if not rows:
            return "No opportunities saved yet — ask me to find some."
        return "📌 Recently found opportunities:\n\n" + format_report(rows, header=False)


# ---- pure helpers (unit-tested) -------------------------------------------

def parse_finds(text: str) -> list[dict]:
    """Extract and validate the JSON array from the model's reply."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    finds = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        title, url = item.get("title"), item.get("url")
        if not (title and url) or item.get("kind") not in KINDS:
            continue
        deadline = item.get("deadline")
        try:
            if deadline:
                date.fromisoformat(deadline)
        except ValueError:
            deadline = None
        finds.append({
            "title": str(title).strip(),
            "kind": item["kind"],
            "url": str(url).strip(),
            "deadline": deadline,
            "location": (item.get("location") or "").strip() or None,
            "summary": (item.get("summary") or "").strip() or None,
        })
    return finds


def format_report(finds: list[dict], header: bool = True) -> str:
    lines = [f"🔎 Found {len(finds)} new opportunities:", ""] if header else []
    for f in finds:
        icon = KIND_ICONS.get(f["kind"], "📌")
        bits = [b for b in (f.get("location"), _deadline_str(f.get("deadline"))) if b]
        lines.append(f"{icon} {f['title']}" + (f" — {', '.join(bits)}" if bits else ""))
        if f.get("summary"):
            lines.append(f"   {f['summary']}")
        lines.append(f"   {f['url']}")
        lines.append("")
    lines.append("Say e.g. \"remind me to apply to <name> by its deadline\" and I'll set it up.")
    return "\n".join(lines).strip()


def _deadline_str(deadline: str | None) -> str | None:
    if not deadline:
        return None
    try:
        d = date.fromisoformat(deadline)
    except ValueError:
        return None
    days = (d - date.today()).days
    if days < 0:
        return f"deadline {deadline} (passed)"
    return f"deadline {deadline} ({'today' if days == 0 else f'{days}d left'})"
