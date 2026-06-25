"""
Read university grades via Korea University's Canvas JSON API, reusing the saved
browser session (uni_auth_state.json). Cleaner and more accurate than scraping
the rendered page — the API distinguishes submitted vs unsubmitted, gives exact
scores, ISO due dates, and both current/final totals.

    python uni_api_grades.py            # full per-assignment report
    python uni_api_grades.py --short    # compact summary

Refresh the session with `python uni_login_test.py` when it expires.
Only academic courses are shown (filtered by UNI_TERM_CODE, default "261R").
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

BASE = os.environ.get("UNI_MYLMS_URL", "https://mylms.korea.ac.kr/").rstrip("/") + "/api/v1"
AUTH_STATE_PATH = Path(__file__).with_name("uni_auth_state.json")
SNAPSHOT_PATH = Path(__file__).with_name("uni_api_grades.json")
REPORT_PATH = Path(__file__).with_name("uni_api_grades_report.txt")
TERM_CODE = os.environ.get("UNI_TERM_CODE", "261R")  # academic courses; "" = all
KST = timezone(timedelta(hours=9))


def _strip(text: str) -> str:
    # Canvas guards JSON with a leading `while(1);` against hijacking.
    text = text.lstrip()
    return text[len("while(1);"):] if text.startswith("while(1);") else text


def _num(x) -> str:
    if x is None:
        return "?"
    return str(int(x)) if float(x).is_integer() else str(x)


def short_course_name(name: str) -> str:
    match = re.search(r"\)([^()]+)\(", name)
    return match.group(1).strip() if match else name[:60]


def fmt_due(due_at: str | None) -> str:
    if not due_at:
        return ""
    try:
        dt = datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(KST)
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return due_at


def fmt_status(a: dict) -> str:
    status = a["status"]
    if a["score"] is not None:
        return f"{_num(a['score'])}/{_num(a['points_possible'])}"
    return {
        "submitted": "submitted (awaiting grade)",
        "unsubmitted": "not submitted",
        "pending_review": "pending review",
        "graded": "graded",
    }.get(status, status or "—")


class Api:
    def __init__(self, req):
        self.req = req

    def get(self, path: str):
        r = self.req.get(f"{BASE}{path}")
        if r.status == 401:
            raise SystemExit("Session expired (401). Run python uni_login_test.py to log in again.")
        if "application/json" not in r.headers.get("content-type", ""):
            raise SystemExit(f"Unexpected non-JSON response for {path} (status {r.status}).")
        return json.loads(_strip(r.text()))


def fetch_grades() -> list[dict]:
    # On a fresh container (Railway wipes disk on deploy) there's no saved
    # session — create one via headless login before fetching.
    if not AUTH_STATE_PATH.exists():
        refresh = Path(__file__).with_name("uni_refresh_session.py")
        subprocess.run([sys.executable, str(refresh)], timeout=180)
    if not AUTH_STATE_PATH.exists():
        raise SystemExit("Missing uni_auth_state.json. Run python uni_login_test.py first.")

    results = []
    with sync_playwright() as p:
        # Same container-safe launch args as uni_refresh_session.py (Railway /dev/shm + sandbox).
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(storage_state=str(AUTH_STATE_PATH))
        api = Api(context.request)

        courses = api.get("/courses?enrollment_state=active&per_page=100")
        for course in courses:
            name = course.get("name") or ""
            cid = course.get("id")
            if not cid or (TERM_CODE and TERM_CODE not in name):
                continue

            enr = api.get(f"/courses/{cid}/enrollments?user_id=self")
            student = next((e for e in enr if e.get("type") == "StudentEnrollment"),
                           enr[0] if enr else {})
            grades = student.get("grades", {})

            subs = api.get(
                f"/courses/{cid}/students/submissions"
                "?student_ids[]=self&include[]=assignment&per_page=100"
            )
            assignments = []
            for s in subs:
                a = s.get("assignment") or {}
                if not a.get("name"):
                    continue
                assignments.append({
                    "name": a["name"],
                    "score": s.get("score"),
                    "points_possible": a.get("points_possible"),
                    "status": s.get("workflow_state"),
                    "due_at": a.get("due_at"),
                })
            assignments.sort(key=lambda x: x["due_at"] or "")

            results.append({
                "id": cid,
                "name": name,
                "short_name": short_course_name(name),
                "current_score": grades.get("current_score"),
                "final_score": grades.get("final_score"),
                "assignments": assignments,
            })
        browser.close()
    return results


def format_report(results: list[dict], short: bool = False) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = ["University grades (via Canvas API)", f"Checked: {now}", ""]

    for c in results:
        cur, fin = c["current_score"], c["final_score"]
        head = f"{c['short_name']}: "
        head += f"{cur}%" if cur is not None else "no grade yet"
        if fin is not None and fin != cur:
            head += f" (final {fin}%)"
        lines.append(head)

        missing = [a for a in c["assignments"] if a["status"] == "unsubmitted"]
        if short:
            if missing:
                names = ", ".join(a["name"] for a in missing[:3])
                lines.append(f"  not submitted ({len(missing)}): {names}")
        else:
            for a in c["assignments"]:
                due = fmt_due(a["due_at"])
                due_str = f"  (due {due})" if due else ""
                lines.append(f"  - {a['name']}: {fmt_status(a)}{due_str}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _due_dt(due_at: str | None):
    if not due_at:
        return None
    try:
        return datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return None


def format_assignments(results: list[dict]) -> str:
    now = datetime.now(KST)
    items = []
    for c in results:
        for a in c["assignments"]:
            items.append({**a, "course": c["short_name"], "_due": _due_dt(a["due_at"])})

    def by_due(a):
        return (a["_due"] is None, a["_due"] or now)

    not_sub = sorted((a for a in items if a["status"] == "unsubmitted"), key=by_due)
    awaiting = sorted((a for a in items if a["status"] == "submitted"), key=by_due)

    lines = ["University assignments (via Canvas API)",
             f"Checked: {now.strftime('%Y-%m-%d %H:%M KST')}", ""]

    lines.append(f"NOT SUBMITTED ({len(not_sub)}):")
    if not not_sub:
        lines.append("  none — you're caught up 🎉")
    for a in not_sub:
        if a["_due"]:
            when = a["_due"].strftime("%Y-%m-%d %H:%M")
            flag = "  ⚠️ OVERDUE" if a["_due"] < now else ""
        else:
            when, flag = "no due date", ""
        lines.append(f"  - [{a['course']}] {a['name']} — due {when}{flag}")

    if awaiting:
        lines += ["", f"SUBMITTED, awaiting grade ({len(awaiting)}):"]
        for a in awaiting:
            when = a["_due"].strftime("%Y-%m-%d") if a["_due"] else "—"
            lines.append(f"  - [{a['course']}] {a['name']} (due {when})")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--short", action="store_true", help="compact grade summary")
    parser.add_argument("--assignments", action="store_true",
                        help="list unsubmitted/awaiting assignments instead of grades")
    args = parser.parse_args()

    results = fetch_grades()
    SNAPSHOT_PATH.write_text(
        json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "courses": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.assignments:
        report = format_assignments(results)
    else:
        report = format_report(results, short=args.short)
        REPORT_PATH.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
